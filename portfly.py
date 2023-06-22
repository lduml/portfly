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


SK_IO_CHUNK_LEN = 4096
MAX_STREAM_ID   = 0xFFFFFFFF


class trafix():
    """ traffic exchanging class """

    @staticmethod
    def close_socket(sk):
        """ socket no-raise-close interface """
        if sk:
            try:
                sk.shutdown(socket.SHUT_RDWR)
                sk.close()
            except OSError:
                pass

    def clean(self, k, s=None):
        assert len(self.sdict) == len(self.kdict)
        _s, _ = self.sdict.pop(k, (None,None))
        if s:
            assert s is _s
        else:
            s = _s
        if s:
            self.kdict.pop(s, None)
            trafix.close_socket(s)
            try:
                self.sread.remove(s)
            except ValueError:
                pass

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    def recv_sk_nonblock(sk):
        """ socket nonblocking recv generator, one shot """
        while True:
            try:
                yield sk.recv(SK_IO_CHUNK_LEN)
            except BlockingIOError:
                return
        
    def send_sk_nonblock(self, k):
        sk, data = self.sdict[k]
        try:
            while True:
                if len(data) == 0:
                    break
                if (i:=sk.send(data[:SK_IO_CHUNK_LEN])) == -1:
                    raise ConnectionError('send_sk_nonblock send -1')
                data = self.sdict[k][1] = data[i:]
        except BlockingIOError:
            pass

    def __init__(self, sk, pserv, port):
        # no need to set pserv to nonblocking
        self.pserv = pserv
        # set tunnel socket to nonblocking
        self.sk = sk
        self.sk.setblocking(False)
        self.gen_recv = trafix.recv_sk_nonblock_forever(sk)
        self.gen_send = trafix.send_sk_nonblock_forever(sk)
        next(self.gen_send)
        self.sid = 1       # sid, stream id, also called k
        self.sdict = {}    # sid --> socket
        self.kdict = {}    # socket --> sid
        self.sread = []    # sockets ready to be read
        self.port = port
        # go
        try:
            self.go(port)
        except Exception as e:
            log.error('exception [%d]: %s', port, str(e))
            log.exception(e)
            for s,_ in self.sdict.values():
                trafix.close_socket(s)
        # end
        trafix.close_socket(self.pserv)
        trafix.close_socket(self.sk)
        log.warning('[%d] closed', port)

    def flush(self):
        self.gen_send.send((None,0))
        try:
            for k in self.sdict.keys():
                self.send_sk_nonblock(k)
        except OSError:
            log.info('[%d] sid %d is closed while flush', self.port, k)
            self.gen_send.send((mngt_prefix+b'sodie',k))
            self.clean(k)

    def go(self, port):
        while True:
            self.flush()
            selist = [self.pserv, self.sk] + list(self.kdict.keys())
            self.sread, _, _ = select.select(selist,[],[],1)
            if len(self.sread) == 0:
                continue
            # new connections
            if self.pserv in self.sread:
                conn, addr = self.pserv.accept()
                self.gen_send.send((mngt_prefix+b'gogogo',self.sid))
                log.info('[%d] accept %s, sid %d', port, str(addr), self.sid)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                conn.setblocking(False)             # set nonblocking
                self.sdict[self.sid] = [conn, b'']  # sid -> [socket,buffer]
                self.kdict[conn] = self.sid
                self.sid = self.sid+1 if self.sid!=MAX_STREAM_ID else 1
                self.sread.remove(self.pserv)
            # recv from tunnel
            if self.sk in self.sread:
                while True:
                    k, bmsg = next(self.gen_recv)
                    if k:
                        # connection die
                        if (bmsg == mngt_prefix+b'sodie' and 
                                k in self.sdict.keys()):
                            log.info('[%d] close sid %d by client', port, k)
                            self.clean(k)
                        # heartbeat
                        elif bmsg == hb_bmsg:
                            log.info('[%d] recv & send heartbeat (sid=%d)', port, k)
                            self.gen_send.send((hb_bmsg,k))
                        # data
                        else:
                            try:
                                if k in self.sdict.keys():
                                    self.sdict[k][1] += bmsg
                                    self.send_sk_nonblock(k)
                            except OSError:
                                log.info('[%d] sid %d is closed while send',
                                                                     port, k)
                                self.gen_send.send((mngt_prefix+b'sodie',k))
                                self.clean(k)
                    else:
                        break
                self.sread.remove(self.sk)
            self.flush()
            # recv from connections,
            # self.clean would remove s in self.sread list,
            # so here should make a copy.
            for s in self.sread[:]:
                k = self.kdict[s]
                gen_data = trafix.recv_sk_nonblock(s)
                while True:
                    try:
                        if (data:=next(gen_data)) == b'':
                            raise OSError
                    except OSError:
                        log.info('[%d] sid %d is donw while recv', port, k)
                        self.gen_send.send((mngt_prefix+b'sodie',k))
                        self.clean(k, s)
                        break
                    except StopIteration:
                        break
                    self.gen_send.send((data,k))  # send data


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
                threading.Thread(target=trafix,
                                 args=(so,tserv,port),
                                 daemon=True).start()
            else:
                raise ValueError('magic bmsg error')
        except Exception as e:
            log.error('Exception %s', str(addr))
            log.exception(e)
            so.shutdown(socket.SHUT_RDWR)
            so.close()


