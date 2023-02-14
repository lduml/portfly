import sys
import socket
import select
import logging as log
import threading
import argparse
from common import *


log.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s',
                level=log.WARNING)


HB_INTV = 20
class heartbeat():
    """ sent by client """
    def __init__(self):
        self.interval = HB_INTV

    def reset(self):
        self.interval = HB_INTV

    def try_send(self, so):
        self.interval -= 1
        if self.interval == 0:
            log.info('send heartbeat')
            tbsend(so, hb_bmsg, 0)  # sid is 0
            self.interval = HB_INTV


def report_local(taddr, so):
    sodict = {}
    hb = heartbeat()
    while True:
        ss, _, _ = select.select([so]+list(sodict.values()), [], [], 1)
        if len(ss) == 0:
            hb.try_send(so)
            continue
        try:
            ss = List(ss)
            # recv from server
            if so in ss:
                hb.reset()
                sid, bmsg = tbrecv(so)
                # new connection
                if bmsg == mngt_prefix+b'gogogo':
                    try:
                        s = socket.create_connection(taddr, timeout=2)
                        log.info('connect target %s ok', str(taddr))
                        s.setsockopt(socket.IPPROTO_TCP,
                                             socket.TCP_NODELAY,True)
                        sodict[sid] = s
                    except OSError as e:
                        log.error('connect %s failed: %s',str(taddr), str(e))
                        tbsend(so, mngt_prefix+b'sodie', sid)
                # connection die
                elif bmsg == mngt_prefix+b'sodie':
                    log.info('close sid %d by server', sid)
                    close_so(s:=sodict.pop(sid,None))
                    ss.remove(s)
                # heartbeat
                elif bmsg == hb_bmsg:
                    log.info('recv heartbeat')
                # data
                else:
                    try:
                        sodict[sid].sendall(bmsg)
                    except (OSError,KeyError):
                        log.info('stream %d is closed by exception', sid)
                        tbsend(so, mngt_prefix+b'sodie', sid)
                        close_so(s:=sodict.pop(sid,None))
                        ss.remove(s)
                ss.remove(so)
            # recv from connections
            for sdn in ss:
                # find socket
                k = v = None
                for k,v in sodict.items():
                    if v is sdn:
                        break
                else:
                    raise KeyError('no sid found')
                # recv
                try:
                    data = sdn.recv(4096)
                    if data == b'':
                        log.info('stream %d is down (recv 0)', k)
                        hb.reset()
                        tbsend(so, mngt_prefix+b'sodie', sid)
                        close_so(sodict.pop(k,None))
                        continue
                except OSError:
                    log.info('stream %d has been reset', k)
                    hb.reset()
                    tbsend(so, mngt_prefix+b'sodie', sid)
                    close_so(sodict.pop(k,None))
                hb.reset()
                tbsend(so, data, k)
        except Exception as e:
            log.error('Exception %s: %s.', str(taddr), repr(e))
            log.exception(e)
            for s in sodict.values():
                close_so(s)
            break
    # while end
    close_so(so)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-x', action='store_true',
                        help='use simple encryption, default is plain')
    parser.add_argument('settings',
                        help='server_map_port:target_host:target_port')
    parser.add_argument('server', help='server_addr_ip:server_addr_port')
    args = parser.parse_args()

    pub_port, host, port = args.settings.strip().split(':')
    serv_ip, serv_port = args.server.strip().split(':')
    serv_addr = (serv_ip, int(serv_port))

    try:
        so = socket.create_connection(serv_addr)
        so.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
        so.sendall(cx(magic_bmsg) + b'\n')
        so.sendall(cx(pub_port.encode()) + b'\n')
        so.sendall(cx(str(args.x).encode()) + b'\n')
        rf = so.makefile('rb')
        if dx(rf.readline().strip()) == magic_breply:
            log.warning('Connect server %s ok, port %s is ready.',
                                                    serv_ip, pub_port)
        else:
            raise ValueError('magic_breply is not match')
    except Exception as e:
        log.exception(e)
        try:
            so.shutdown(socket.SHUT_RDWR)
            so.close()
        except (NameError,OSError):
            pass
        sys.exit(1)

    target_addr = (host, int(port))
    tbsend = sosr.get_send(args.x)
    tbrecv = sosr.get_recv(args.x)
    th = threading.Thread(target=report_local,
                          args=(target_addr, so), daemon=True)
    th.start()
    th.join()


