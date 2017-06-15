#!/usr/bin/env python
# -*- coding: utf-8 -*-
import copy
import inspect
import logging
import os
import time
import uuid
import zipfile
from io import BytesIO

import gevent.monkey
from gevent.pool import Pool

from microservice import micro_server
from service_manage import get_service_group, update_service_group

gevent.monkey.patch_all()


def Command(func):
    def warp(cls, *args, **kwargs):
        return func(cls, *args, **kwargs)
    
    return warp


class WORK_FRAME(micro_server):
    command_fun = {}
    
    def __init__(self, name=None, service_group_conf=None, app=None, channel="center", lock=False, auri=None):
        # 指定name最优先，service_group_conf中的service_group次之
        if not name:
            if service_group_conf:
                name = get_service_group(service_group_conf).get('service_group')
            if not name:
                raise EnvironmentError('Neither name given nor service_group_conf name given')
        super(WORK_FRAME, self).__init__(name, app=app, channel=channel, auri=auri, lock=lock)
        self.command_q = "{0}-{1}".format(self.name, self.id)
        # frame停止运行,没有任何consumer消费20s后自动删除command_q
        self.create_queue(self.command_q, ttl=15, args={'x-expires': 20000})
        self.command_prefix = "skyeye-rpc-{0}.".format(self.name)
        self.join(self.command_q, "{0}*".format(self.command_prefix))
        self.init_command()
        self.command_pool = Pool(100)
        self.service_group_conf = service_group_conf
    
    def init_service(self):
        if not self.service_group_conf:
            raise ImportError('No Config of Service Group: service_group_conf')
        self.loaded_services = get_service_group(self.service_group_conf)
        for service_pkg, value in self.loaded_services['service_pkg'].iteritems():
            for service_name, func in value['services'].iteritems():
                self.services.update({service_name: func})
    
    def init_crontabs(self):
        """
        frame_start时调用, 调用rpc检查是否已有相同名称的定时任务启动
        1 检查其他机器所有cron状态
        2 对比自身载入的所有crontab。
        3 存在则设置状态False； 不存在则设置状态True ,并设置微服务层定时任务

        最后self.loaded_crontab的结构：
        {
            'print':{
                'path':'/data/project/mq-service/services_default/task1_crontab',
                'version': 1.0,
                'crontabs':[ {'time_str': '', 'command': ''}, {} ]
                'status': True
            }
            'sql':{
                'path':'/data/project/mq-service/services_default/sql_crontab',
                'version': 1.1,
                'crontabs':[ {'time_str': '', 'command': ''}, {} ]
                'status': False
            }
        }
        """
        self.loaded_crontab = get_service_group(self.service_group_conf)['crontab_pkg']
        all_servers_crontab_status = self.get_all_crontab_status()
        for crontab_pkg, value in self.loaded_crontab.iteritems():
            need_cron_start = True
            for server, cron_dicts in all_servers_crontab_status.iteritems():
                if cron_dicts.get(crontab_pkg, {})['status']:
                    need_cron_start = False
                    break
                    
            # 所有服务器上没有已启动的<crontab_pkg>
            if need_cron_start:
                value['status'] = True
                self.set_crontabs(cron_name=crontab_pkg, jobs=value['crontabs'])
            else:
                value['status'] = False
    
    def frame_start(self, process_num=2, daemon=True):
        """框架启动"""
        print 'WORK FRAME START'
        print self.command_q, '\n', 30 * '-'
        
        self.init_service()
        self.init_crontabs()
        
        self.start(process_num, daemon=daemon)
        channel = self.connection.channel()
        channel.basic_consume(self.process_command,
                              queue=self.command_q, no_ack=False)
        try:
            channel.start_consuming()
        except Exception:
            self.connection = self.connect()
            channel = self.connection.channel()
            channel.start_consuming()
    
    def process_command(self, ch, method, props, body):
        """server中的命令执行函数"""
        ch.basic_ack(delivery_tag=method.delivery_tag)
        body = self.decode_body(body)
        args, kwargs = body
        rtk = method.routing_key.replace(self.command_prefix, "")
        buf = rtk.split("@")
        rtk = buf[0]
        if len(buf) > 1:
            id = buf[1]
            if self.command_q != id:
                return
        fn = self.command_fun.get(rtk)
        if fn:
            result = fn(*args, **kwargs)
            rbody = result
            self.push_msg(qid=self.command_q, topic="", msg=rbody, reply_id=props.correlation_id, session=ch,
                          to=props.reply_to)
    
    def command(self, name=None, *args, **kwargs):
        """work frame客户端命令调用函数"""
        id = kwargs.get("id")
        try:
            kwargs.pop("id")
        except:
            pass
        if name and name in self.command_fun:
            topic = "{0}{1}".format(self.command_prefix, name)
            if id:
                topic = "{0}{1}@{2}".format(self.command_prefix, name, id)
            qid = "command_{0}.{1}.{2}".format(self.command_q, name, uuid.uuid4())
            self.create_queue(qid, exclusive=True, auto_delete=True, )
            self.push_msg(qid, topic=topic, msg=(args, kwargs), ttl=15)
            return qid
    
    def get_response(self, qid, timeout=0):
        """work frame 客户端结果获取函数"""
        if timeout:
            time.sleep(timeout)
        try:
            ch = self.connection.channel()
        except:
            self.connection = self.connect()
            ch = self.connection.channel()
        ctx = self.pull_msg(qid=qid, session=ch)
        return {i[1].reply_to: i[-1] for i in ctx}
    
    @classmethod
    def methodsWithDecorator(cls, decoratorName):
        sourcelines = inspect.getsourcelines(cls)[0]
        for i, line in enumerate(sourcelines):
            line = line.strip()
            if line.split('(')[0].strip() == '@' + decoratorName:  # leaving a bit out
                nextLine = sourcelines[i + 1]
                name = nextLine.split('def')[1].split('(')[0].strip()
                yield (name)
    
    def init_command(self):
        l = self.methodsWithDecorator("Command")
        for func in l:
            fun = getattr(self, func)
            self.command_fun.setdefault(func, fun)
    
    def get_last_version(self, service=None, timeout=5):
        r = self.command("get_service_version", service=service)
        data = self.get_response(r, timeout=timeout, )
        last_dict = {}
        for id in data:
            for service in data[id]:
                t = last_dict.get(service)
                if t and data[id][service]["version"] > t[0]:
                    last_dict[service] = [data[id][service]["version"], data[id][service]["path"], id]
                else:
                    last_dict.setdefault(service, [data[id][service]["version"], data[id][service]["path"], id])
        return last_dict
    
    def get_all_crontab_status(self, crontab=None, timeout=5):
        r = self.command('get_crontab_status', crontab)
        data = self.get_response(r, timeout=timeout)
        return data
    
    @Command
    def system(self, cmd):
        output = os.popen(cmd)
        data = output.read()
        output.close()
        return data
    
    @Command
    def restart_service(self, process_num=2, daemon=True):
        self.init_service()
        self.restart(n=process_num, daemon=daemon)
        return 'restart ok'
    
    @Command
    def get_service_version(self, service=None):
        rdata = {}
        if not service:
            for i in self.loaded_services['service_pkg']:
                data = copy.deepcopy(self.loaded_services['service_pkg'][i])
                data.pop("services")
                rdata.setdefault(i, data)
        else:
            if service not in self.loaded_services['service_pkg']:
                rdata = {}
            else:
                data = copy.deepcopy(self.loaded_services['service_pkg'][service])
                data.pop("services")
                rdata = {service: data}
        return rdata
    
    @Command
    def get_crontab_status(self, crontab=None):
        if crontab:
            content = self.loaded_crontab.get(crontab)
            if content:
                return {crontab: content}
            return {}
        return self.loaded_crontab
    
    @Command
    def zip_pkg(self, service_pkg):
        pkg_path = self.loaded_services['service_pkg'][service_pkg]['path']
        tmp = BytesIO()
        cwd = os.getcwd()
        os.chdir(pkg_path)
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk('.'):
                for f in files:
                    if f.split('.')[-1] != 'pyc':
                        z.write(os.path.join(root, f), compress_type=zipfile.ZIP_DEFLATED)
        
        res = tmp.getvalue()
        tmp.close()
        os.chdir(cwd)
        return res
    
    def _update_and_install_pkg(self, fid_version, service_pkg, install_path=None, timeout=5):
        """
        被更新服务端发起, 更新服务不需要install_path， 安装服务需要install_path
        update service -->  install_path=None, use existed path
        install service --> install_path is not None, use install_path

        :param fid_version: source code provider server id & code version
        :param service_pkg: the service name which needed update or install
        :param install_path: if service not exist in server, need install_path to deploy
        :param timeout:
        :return: execution message
        """
        from_server_id, from_server_version = fid_version['fid'], fid_version['version']
        old_version = self.loaded_services.get('service_pkg').get(service_pkg, {}).get('version')
        if from_server_id == self.command_q:
            return 'I am the source code'
        
        if from_server_version == old_version:
            return 'Service <{}> Version <{}> Already on the server'.format(service_pkg, from_server_version)
        
        r = self.command('zip_pkg', service_pkg, id=from_server_id)
        data = self.get_response(r, timeout=timeout)
        if not data:
            return 'ERR: No Zip Content from get_response'
        
        content = data[from_server_id]
        if not content:
            return 'ERR: No Zip Content. From {}, To {}'.format(from_server_id, self.command_q)
        
        # update service -->  install_path=None, use existed path
        # install service --> install_path is not None, use install_path
        self_server_path = self.loaded_services.get('service_pkg').get(service_pkg, {}).get('path')
        if not self_server_path and not install_path:
            return 'ERR: No Service AND No install_path. Cannot update or install on {}:'.format(self.command_q)
        
        if install_path:
            self_server_path = install_path
        
        cwd = os.getcwd()
        os.chdir(self_server_path)
        with BytesIO() as tmp:
            tmp.write(content)
            with zipfile.ZipFile(tmp, 'r', zipfile.ZIP_DEFLATED) as z:
                try:
                    z.extractall()
                except Exception as e:
                    logging.exception(e)
                    return 'ERR: extract failed, {}'.format(e.message)
        os.chdir(cwd)
        return 'Update OK. Version from <{}> to <{}>'.format(old_version, from_server_version)
    
    @Command
    def update_pkg(self, fid_version, service_pkg, timeout=5):
        return self._update_and_install_pkg(fid_version, service_pkg, timeout=timeout)
    
    @Command
    def install_pkg(self, fid_version, service_pkg, install_path, timeout=5):
        # check whether service is installed
        if service_pkg in self.loaded_services.get('service_pkg', {}):
            if fid_version['version'] == self.loaded_services.get('service_pkg').get(service_pkg, {}).get('version'):
                return 'Service <{}> Version <{}> Already on this server'.format(service_pkg, fid_version['version'])
            else:
                return 'Service <{}> Version <{}> Already on this server. Please use update command to version <{}>'. \
                    format(service_pkg, self.loaded_services.get('service_pkg').get(service_pkg, {}).get('version'),
                           fid_version['version'])
        
        # install_path为相对路径时，更改为绝对路径
        if install_path.split(os.path.sep)[0] in ['.', '..'] and type(self.service_group_conf) in (str, unicode):
            service_group_dir = os.path.dirname(os.path.realpath(self.service_group_conf))
            install_path = os.path.realpath(os.path.join(service_group_dir, install_path))
        if not os.path.exists(install_path):
            try:
                os.makedirs(install_path)
            except Exception as e:
                if not os.path.exists(install_path):
                    logging.exception(e)
                    return 'ERR: install fail, cannot make dir {}. {}'.format(install_path, e.message)
        
        res = self._update_and_install_pkg(fid_version, service_pkg, install_path, timeout=timeout)
        if res != 'update ok':
            return res
        # install service: 需将service group载入的文件更新(只针对配置文件启动)
        update_service_group(self.service_group_conf, install_path)
        return res
    
    # 上层控制函数
    def _get_source_service_server_id(self, service_pkg, version=None, timeout=5):
        fid_version = {}
        if not version:
            v = self.get_last_version(service_pkg, ).get(service_pkg)
            if v:
                fid_version = {'version': v[0], 'fid': v[2]}
        else:
            r = self.command("get_service_version", service=service_pkg)
            data = self.get_response(r, timeout=timeout, )
            for server_id, service in data.iteritems():
                if service_pkg not in service:
                    break
                if version == service[service_pkg]["version"]:
                    fid_version = {'version': version, 'fid': server_id}
                    break
        return fid_version
    
    def update_service(self, service_pkg, version=None, id=None, timeout=5):
        print '--- Update Service <{}> to Version <{}> ---'.format(service_pkg, version if version else 'latest')
        fid_version = self._get_source_service_server_id(service_pkg, version=version, timeout=timeout)
        if fid_version:
            print '--- From Source server <{}> Version <{}> ---'.format(fid_version['fid'], fid_version['version'])
            r = self.command("update_pkg", fid_version, service_pkg, id=id, timeout=timeout)
            data = self.get_response(r, timeout=timeout)
            data.update(self.get_response(r, timeout=timeout))
            return data
        print '--- No Source Server and Version Found ---'
    
    def install_service(self, service_pkg, service_install_path, version=None, id=None, timeout=5):
        print '--- Update Service <{}> to Version <{}> ---'.format(service_pkg, version if version else 'latest')
        fid_version = self._get_source_service_server_id(service_pkg, version=version, timeout=timeout)
        if fid_version:
            print '--- From Source server <{}> Version <{}> ---'.format(fid_version['fid'], fid_version['version'])
            r = self.command("install_pkg", fid_version, service_pkg, install_path=service_install_path, id=id,
                             timeout=timeout)
            data = self.get_response(r, timeout=timeout)
            data.update(self.get_response(r, timeout=timeout))
            return data
        print '--- No Source Server and Version Found ---'


def main():
    pass


if __name__ == '__main__':
    main()
