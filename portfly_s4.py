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
                level=log.INFO)


def close_socket(s):
    """ socket no-raise-close interface """
    if s:
        try:
            s.shutdown(socket.SHUT_RDWR)
            s.close()
        except OSError:
            pass


def remove_list_ele(lst, ele):
    """ remove element from list with no raise """
    try:
        lst.remove(ele)
    except ValueError:
        pass


SK_IO_CHUNK_LEN = 4096
MAX_STREAM_ID   = 0xFFFFFFFF


def recv_sk_nonblock_forever(sk):
    """ socket nonblocking recv generator, last forever """
    data = b''
    while True:
        try:
            _d = sk.recv(SK_IO_CHUNK_LEN)
            if len(_d) == 0:
                raise ConnectionError('recv_sk_nonblock_forever recv 0')
            data += _d
            while (dlen:=len(data)) > 4:
                mlen = int.from_bytes(data[:4], 'little')
                if dlen >= mlen:
                    yield int.from_bytes(data[4:8],'big'), data[8:mlen]
                    data = data[mlen:]
                else:
                    break
        except BlockingIOError:
            yield None, b''


def recv_sk_nonblock(sk):
    """ socket nonblocking recv generator, one shot """
    while True:
        try:
            yield sk.recv(SK_IO_CHUNK_LEN)
        except BlockingIOError:
            return


def send_sk_nonblock_forever(sk):
    """ socket nonblocking send generator, last forever """
    data = b''
    while True:
        bmsg, sid = yield
        if bmsg is not None:
            data += (len(bmsg)+8).to_bytes(4,'little') \
                            + sid.to_bytes(4,'big') \
                            + bmsg
        try:
            while True:
                if len(data) == 0:
                    break
                if (i:=sk.send(data[:SK_IO_CHUNK_LEN])) == -1:
                    raise ConnectionError('send_sk_nonblock_forever send -1')
                data = data[i:]
        except BlockingIOError:
            continue


def send_sk_nonblock(sk_data):
    sk, data = sk_data
    try:
        while True:
            if (i:=sk.send(data[:SK_IO_CHUNK_LEN])) == -1:
                raise ConnectionError('send_sk_nonblock send -1')
            data = data[i:]
            sk_data[1] = data
            if len(data) == 0:
                break
    except BlockingIOError:
        pass


def portfly_pserv(sk, pserv, port):
    # set socket nonblocking, but pserv is not needed
    sk.setblocking(False)
    gen_recv = recv_sk_nonblock_forever(sk)
    gen_send = send_sk_nonblock_forever(sk)
    next(gen_send)

    sid = 1       # sid, stream id, also called k
    sdict = {}   # sid --> socket
    kdict = {}    # socket --> sid
    while True:
        assert len(sdict) == len(kdict)
        rs, _, _ = select.select([pserv,sk]+list(kdict.keys()),[],[],1)
        try:
            gen_send.send((None,0))
            if len(rs) == 0:
                continue
            # new connections
            if pserv in rs:
                conn, addr = pserv.accept()
                gen_send.send((mngt_prefix+b'gogogo',sid))
                log.info('[%d] accept %s, sid %d', port, str(addr), sid)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                conn.setblocking(False)   # set nonblocking
                sdict[sid] = [conn, b'']  # [socket, sending buffer]
                kdict[conn] = sid
                sid = sid+1 if sid!=MAX_STREAM_ID else 1
                rs.remove(pserv)
            # recv from client
            if sk in rs:
                while True:
                    k, bmsg = next(gen_recv)
                    if k:
                        # connection die
                        if (bmsg == mngt_prefix+b'sodie' and 
                                k in sdict.keys()):
                            log.info('[%d] close sid %d by client', port, k)
                            #close_socket(s:=sdict.pop(k,None))
                            s, _ = sdict.pop(k, (None,None))
                            close_socket(s)
                            remove_list_ele(rs, s)
                            if s: kdict.pop(s, None)
                        # heartbeat
                        elif bmsg == hb_bmsg:
                            log.info('[%d] recv & send heartbeat (sid=%d)', port, k)
                            gen_send.send((hb_bmsg,k))
                        # data
                        else:
                            try:
                                if k in sdict.keys():
                                    #sdict[k][0].setblocking(True)
                                    #sdict[k][0].sendall(bmsg)
                                    #sdict[k][0].setblocking(False)
                                    sdict[k][1] += bmsg
                                    send_sk_nonblock(sdict[k])
                            except OSError:
                                log.info('[%d] sid %d is closed by exception',
                                                                        port, k)
                                gen_send.send((mngt_prefix+b'sodie',k))
                                #close_socket(s:=sdict.pop(k,None))
                                s, _ = sdict.pop(k, (None,None))
                                close_socket(s)
                                remove_list_ele(rs, s)
                                if s: kdict.pop(s, None)
                    else:
                        break
                rs.remove(sk)
            # recv from connections
            for s in rs:
                k = kdict[s]
                gen_data = recv_sk_nonblock(s)
                while True:
                    try:
                        if (data:=next(gen_data)) == b'':
                            raise OSError
                    except OSError:
                        log.info('[%d] sid %d is donw while recv', port, k)
                        gen_send.send((mngt_prefix+b'sodie',k))
                        #close_socket(sdict.pop(k,None))
                        sdict.pop(k, (None,None))
                        close_socket(s)
                        kdict.pop(s, None)
                        break
                    except StopIteration:
                        break
                    gen_send.send((data,k))  # send data
        except Exception as e:
            log.error('exception [%d]: %s', port, str(e))
            log.exception(e)
            for s,_ in sdict.values():
                close_socket(s)
            break
    # while end
    close_socket(pserv)
    close_socket(sk)
    log.warning('[%d] closed', port)


# usage: python3 report_serv.py <listen_port>
if __name__ == '__main__':
    addr = ('', int(sys.argv[1].strip()))
    serv = socket.create_server(('', int(sys.argv[1].strip())))
    log.warning('Start report server at address %s.', str(addr))
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
                th = threading.Thread(target=portfly_pserv,
                                      args=(so,tserv,port), daemon=True)
                th.start()
            else:
                raise ValueError('magic bmsg error')
        except Exception as e:
            log.error('Exception %s', str(addr))
            log.exception(e)
            so.shutdown(socket.SHUT_RDWR)
            so.close()


