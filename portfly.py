#!/usr/bin/env python3
import sys
import os
import socket
import select
import logging as log
import argparse
import random
import base64
import multiprocessing as mp
import threading


log.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s',
                level=log.INFO)


magic_bmsg = b'ask for REopening a PORT'
magic_breply = b'Done'
mngt_prefix = b'%-[(=!^$#@~+|0|+~@#$^!=)]-%'
hb_bmsg = mngt_prefix + b'hello'


def cx(bmsg: bytes) -> bytes:
    r = random.randint
    m = r(0,255)
    return base64.b64encode(bytes([m]+[i^m for i in bmsg])
                          + bytes([i^r(0,255) for i in range(m)]))


def dx(bmsg: bytes) -> bytes:
    bmsg = base64.b64decode(bmsg)
    return bytes([i^bmsg[0] for i in bmsg[1:len(bmsg)-bmsg[0]]])


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
                return

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
                return

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
                data = sk.recv(SK_IO_CHUNK_LEN)
                if len(data) == 0:
                    raise ConnectionError('recv_sk_nonblock recv 0')
                yield data
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
            return

    def __init__(self, sk, role, *argv):
        # set tunnel socket to nonblocking
        self.sk = sk
        self.sk.setblocking(False)
        self.gen_recv = trafix.recv_sk_nonblock_forever(sk)
        self.gen_send = trafix.send_sk_nonblock_forever(sk)
        next(self.gen_send)
        self.role = role
        if self.role == 's':
            # no need to set pserv to nonblocking
            self.pserv = argv[0]
            self.port = argv[1]
            self.sid = 1   # sid, stream id, also called k
        else:
            self.target = argv[0]
            self.port = argv[1]
        self.sdict = {}    # sid --> socket
        self.kdict = {}    # socket --> sid
        self.sread = []    # sockets ready to be read
        # go
        try:
            self.go(self.port)
        except Exception as e:
            if self.role == 's':
                log.error('exception [%d]: %s', self.port, str(e))
            log.exception(e)
            for s,_ in self.sdict.values():
                trafix.close_socket(s)
        # end
        trafix.close_socket(self.sk)
        if self.role == 's':
            trafix.close_socket(self.pserv)
            log.warning('[%d] closed', self.port)

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
            selist = list(self.kdict.keys()) + \
                     ([self.sk] if self.role=='c' else [self.pserv,self.sk])
            self.sread, _, _ = select.select(selist,[],[],1)
            if len(self.sread) == 0:
                continue
            # new connections in server role
            if self.role=='s' and self.pserv in self.sread:
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
                        # new connection in client role
                        if bmsg == mngt_prefix+b'gogogo':
                            try:
                                conn = socket.create_connection(self.target, timeout=2)
                                log.info('[%d] connect target %s ok, sid %d', port, str(self.target), k)
                                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                                conn.setblocking(False)
                                self.sdict[k] = [conn, b'']
                                self.kdict[conn] = k
                            except OSError as e:
                                log.error('connect %s failed: %s',str(taddr), str(e))
                                self.gen_send.send((mngt_prefix+b'sodie',k))
                        # connection die
                        elif (bmsg == mngt_prefix+b'sodie' and 
                                k in self.sdict.keys()):
                            log.info('[%d] close sid %d by peer', port, k)
                            self.clean(k)
                        # heartbeat
                        elif bmsg == hb_bmsg:
                            if self.role == 's':
                                log.info('[%d] recv & send heartbeat (sid=%d)', port, k)
                                self.gen_send.send((hb_bmsg,k))
                            else:
                                log.info('recv heartbeat')
                        # data
                        else:
                            try:
                                if k in self.sdict.keys():
                                    self.sdict[k][1] += bmsg
                                    self.send_sk_nonblock(k)
                            except OSError:
                                log.info('[%d] sid %d is closed while send', port, k)
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
                        data = next(gen_data)
                    except OSError:
                        log.info('[%d] sid %d is donw while recv', port, k)
                        self.gen_send.send((mngt_prefix+b'sodie',k))
                        self.clean(k, s)
                        break
                    except StopIteration:
                        break
                    self.gen_send.send((data,k))  # send data


def server_main(saddr):
    serv = socket.create_server(saddr)
    log.warning('start portfly server at %s', str(saddr))
    while True:
        sk, faddr = serv.accept()
        log.warning('accept from %s', str(faddr))
        sk.settimeout(2)
        rf = sk.makefile('rb')
        try:
            if dx(rf.readline().strip()) == magic_bmsg:
                # recv port
                port = int(dx(rf.readline().strip()))
                pserv = socket.create_server(('', port))
                log.warning('create server at public port %d', port)
                sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                # recv x
                x = eval((dx(rf.readline().strip())).decode())
                log.warning('encryption %d', x)
                sk.sendall(cx(magic_breply) + b'\n')
                log.warning('good to go...')
                mp.Process(target=trafix, args=(sk,'s',pserv,port)).start()
            else:
                raise ValueError('magic bmsg error')
        except Exception as e:
            log.error('exception %s', str(faddr))
            log.exception(e)
            trafix.close_socket(socket(sk))


def client_main(setting, saddr):
    pub_port, host, port = setting.strip().split(':')
    serv_ip, serv_port = saddr.strip().split(':')
    serv_addr = (serv_ip, int(serv_port))

    try:
        so = socket.create_connection(serv_addr)
        so.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
        so.sendall(cx(magic_bmsg) + b'\n')
        so.sendall(cx(pub_port.encode()) + b'\n')
        #so.sendall(cx(str(args.x).encode()) + b'\n')
        so.sendall(cx(str(0).encode()) + b'\n')
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
    #tbsend = sosr.get_send(args.x)
    #tbrecv = sosr.get_recv(args.x)
    th = threading.Thread(target=trafix,
                          args=(so,'c',target_addr,int(pub_port)), daemon=True)
    th.start()
    th.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    end_type = parser.add_mutually_exclusive_group(required=True)
    end_type.add_argument('-s', '--server', action='store_true',
                          help='server end of tcp tunnel')
    end_type.add_argument('-c', '--client', action='store_true',
                          help='client end of tcp tunnel')
    parser.add_argument('--ip',
                        help='server listen ip, default is all')
    parser.add_argument('-p', '--port', type=int,
                        help='server listen port')
    parser.add_argument('--setting',
                        help='server_port:target_host:target_port')
    parser.add_argument('--serveraddr',
                        help='server_host:server_port')
    args = parser.parse_args()

    if args.server:
        if args.ip is None:
            print('--ip is not provide, 0.0.0.0 is used by default')
        if args.port is None:
            print('--port must be provied in server role')
            sys.exit(1)
        if args.setting:
            print('--setting is ignored in server role')
        if args.serveraddr:
            print('--serveraddr is ignored in server role')
        server_main(('' if args.ip is None else args.ip, int(args.port)))
    else:
        client_main(args.setting, args.serveraddr)


