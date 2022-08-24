import sys
import os
import socket
import threading
import select
from time import sleep
from time import monotonic as time
import logging as log
from common import *


log.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s',
                level=log.WARNING)


def report_tserv(so, tserv, port):
    sid = 1  # stream id
    sodict = {} 
    while True:
        ss, _, _ = select.select([tserv,so]+list(sodict.values()), [], [], 1)
        if len(ss) == 0:
            continue
        try:
            ss = List(ss)
            # new connections
            if tserv in ss:
                sk, addr = tserv.accept()
                log.info('[%d] accept %s, sid %d', port, str(addr), sid)
                sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                sodict[sid] = sk
                tbsend(so, mngt_prefix+b'gogogo', sid)
                sid += 1
                if sid == 0xFFFFFFFF:
                    sid = 1
                ss.remove(tserv)
            # recv from client
            if so in ss:
                _sid, bmsg = tbrecv(so)
                # connection die
                if bmsg == mngt_prefix+b'sodie':
                    log.info('[%d] close sid %d by client', port, _sid)
                    close_so(s:=sodict.pop(_sid,None))
                    ss.remove(s)
                # heartbeat
                elif bmsg == hb_bmsg:
                    log.info('[%d] recv & send heartbeat (sid=%d)', port, _sid)
                    tbsend(so, hb_bmsg, _sid)
                # data
                else:
                    try:
                        sodict[_sid].sendall(bmsg)
                    except (OSError,KeyError):
                        log.info('[%d] stream %d is closed by exception',
                                                                port, _sid)
                        tbsend(so, mngt_prefix+b'sodie', _sid)
                        close_so(sodict.pop(_sid,None))
                ss.remove(so)
            # recv from connections
            for sup in ss:
                # find the socket in sodict
                k = v = None
                for k,v in sodict.items():
                    if v is sup:
                        break
                else:
                    raise KeyError('no sid found')
                # recv
                try:
                    data = sup.recv(4096)
                    if data == b'':
                        log.info('[%d] stream %d is down (recv 0)', port, k)
                        tbsend(so, mngt_prefix+b'sodie', k)
                        close_so(sodict.pop(k,None))
                        continue
                except OSError:
                    log.info('[%d] stream %d has been reset', port, k)
                    tbsend(so, mngt_prefix+b'sodie', k)
                    close_so(sodict.pop(k,None))
                # send to client
                tbsend(so, data, k)
        except Exception as e:
            log.error('Exception [%d]: %s', port, str(e))
            log.exception(e)
            [close_so(s) for s in sodict.values()]
            break
    # while end
    close_so(tserv)
    close_so(so)
    log.info('[%d] closed', port)


# usage: python3 report_serv.py <listen_port>
if __name__ == '__main__':
    addr = ('', int(sys.argv[1].strip()))
    serv = socket.create_server(('', int(sys.argv[1].strip())))
    log.info('Start report_serv at addr %s.', str(addr))
    while True:
        so, addr = serv.accept()
        log.warning('Accept from %s.', str(addr))
        so.settimeout(2)
        rf = so.makefile('rb')
        try:
            if dx(rf.readline().strip()) == magic_bmsg:
                # recv port
                port = int(dx(rf.readline().strip()))
                log.warning('Get the pub_port %d.', port)
                tserv = socket.create_server(('', port))
                so.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                # recv x
                x = eval((dx(rf.readline().strip())).decode())
                log.warning('Encryption %d.', x)
                tbsend = sosr.get_send(x)
                tbrecv = sosr.get_recv(x)
                # good to go
                so.sendall(cx(magic_breply) + b'\n')
                th = threading.Thread(target=report_tserv,
                                      args=(so,tserv,port), daemon=True)
                th.start()
            else:
                raise ValueError('magic bmsg error')
        except Exception as e:
            log.error('Exception %s', str(addr))
            log.exception(e)
            so.shutdown(socket.SHUT_RDWR)
            so.close()


