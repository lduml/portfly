#!/usr/bin/env python3
import sys
import os
import socket
import selectors
import logging as log
import argparse
import random
import base64
import multiprocessing as mp
import threading
import time


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


    @staticmethod
    def send_sk_nonblock_gen(sk):
        """ socket nonblocking send generator """
        data = b''
        while True:
            bmsg, sid = yield len(data)
            if bmsg is not None:
                data += (len(bmsg)+8).to_bytes(4,'little') \
                                + sid.to_bytes(4,'big') \
                                + bmsg
            try:
                while True:
                    if len(data) == 0:
                        break
                    if (i:=sk.send(data[:SK_IO_CHUNK_LEN])) == -1:
                        raise ConnectionError('send_sk_nonblock_gen send -1')
                    data = data[i:]
            except BlockingIOError:
                continue


    @staticmethod
    def recv_sk_nonblock_gen(sk):
        """ socket nonblocking recv generator """
        data = b''
        while True:
            try:
                _d = sk.recv(SK_IO_CHUNK_LEN)
                if len(_d) == 0:
                    raise ConnectionError('recv_sk_nonblock_gen recv 0')
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
        

    def send_sk_nonblock(self, sid):
        sk, data = self.sdict[sid]
        try:
            while True:
                if len(data) == 0:
                    break
                if (i:=sk.send(data[:SK_IO_CHUNK_LEN])) == -1:
                    raise ConnectionError('send_sk_nonblock send -1')
                data = self.sdict[sid][1] = data[i:]
        except BlockingIOError:
            pass
        return len(data)


    def __init__(self, sk, role, *argv):
        self.role = role  # 's':server or 'c':client
        
        # set tunnel socket to nonblocking, init generators
        self.sk = sk
        self.sk.setblocking(False)
        self.gen_recv = trafix.recv_sk_nonblock_gen(sk)
        self.gen_send = trafix.send_sk_nonblock_gen(sk)
        next(self.gen_send)

        # epoll in Linux, select in Windows
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sk, selectors.EVENT_READ)

        # retrive argv and set accordingly
        if self.role == 's':
            self.pserv = argv[0]  # no need to set pserv to nonblocking
            self.port = argv[1]
            self.sid = 1          # sid, stream id
            self.sel.register(self.pserv, selectors.EVENT_READ)
        else:
            self.target = argv[0]
            self.port = argv[1]
            self.heartbeat_time = time.time()

        self.sdict = {}    # sid --> socket
        self.kdict = {}    # socket --> sid

        # go
        try:
            self.go(self.port)
        except Exception as e:
            log.error('exception [%d]: %s', self.port, str(e))
            log.exception(e)
            for s,_ in self.sdict.values():
                trafix.close_socket(s)
        # the end
        trafix.close_socket(self.sk)
        if self.role == 's':
            trafix.close_socket(self.pserv)
        log.warning('[%d] closed', self.port)


    def flush(self):
        tunnel_left = self.gen_send.send((None,0))
        try:
            sk_left = 0
            for k in self.sdict.keys():
                sk_left += self.send_sk_nonblock(k)
        except OSError:
            log.info('[%d] sid %d is closed while flush', self.port, k)
            tunnel_left = self.gen_send.send((mngt_prefix+b'sodie',k))
            self.clean(k)
        return tunnel_left + sk_left


    def try_send_heartbeat(self):
        now = time.time()
        if now - self.heartbeat_time > 30:  # minimal heartbeat interval
            self.gen_send.send((hb_bmsg,0))
            log.info('[%d] send heartbeat', self.port)
            self.heartbeat_time = now


    def update_sid(self):
        # sid 0 is used for heartbeat,
        # sid should be incremented sequentially to avoid conflict.
        while True:
            self.sid = self.sid+1 if self.sid!=MAX_STREAM_ID else 1
            if self.sid not in self.sdict.keys():
                break


    def clean(self, sid, sk=None):
        """ delete sid from sdict,
            delete sk from kdict,
            unregister sk from selector. """
        assert len(self.sdict) == len(self.kdict)
        _sk, _ = self.sdict.pop(sid, (None,None))
        if sk:
            assert sk is _sk
        else:
            sk = _sk
        if sk:
            self.kdict.pop(sk, None)
            trafix.close_socket(sk)
            self.sel.unregister(sk)


    def go(self, port):
        while True:
            # server is always passive, so it can be blocked forever...
            # but client can not, which needs to send heartbeat.
            bytes_left = self.flush()
            if bytes_left != 0:
                log.info('[%d] flushed, bytes left %d', port, bytes_left)
                events = self.sel.select(0)  # just a polling
            else:
                if self.role == 's':
                    events = self.sel.select()
                else:
                    events = self.sel.select(9)

            # when no socket ready to be read,
            if len(events) == 0:
                # it might be a chance to send heartbeat.
                if self.role == 'c':
                    self.try_send_heartbeat()
                # it's better to wait a while before next flush.
                if bytes_left != 0:
                    time.sleep(0.1)
                continue

            # event loop
            for fd,_ in events:
                # new connections in server role
                if self.role=='s' and fd.fileobj==self.pserv:
                    conn, addr = self.pserv.accept()
                    self.gen_send.send((mngt_prefix+b'gogogo',self.sid))
                    log.info('[%d] accept %s, sid %d', port, str(addr), self.sid)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                    conn.setblocking(False)             # set nonblocking
                    self.sel.register(conn, selectors.EVENT_READ)
                    self.sdict[self.sid] = [conn, b'']  # sid -> [socket,buffer]
                    self.kdict[conn] = self.sid
                    self.update_sid()

                # recv from tunnel
                elif fd.fileobj == self.sk:
                    while True:
                        sid, bmsg = next(self.gen_recv)
                        if sid is not None:
                            # new connection in client role
                            if bmsg == mngt_prefix+b'gogogo':
                                try:
                                    conn = socket.create_connection(self.target, timeout=2)
                                    log.info('[%d] connect target %s ok, sid %d', port, str(self.target), sid)
                                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                                    conn.setblocking(False)
                                    self.sel.register(conn, selectors.EVENT_READ)
                                    self.sdict[sid] = [conn, b'']
                                    self.kdict[conn] = sid
                                except OSError as e:
                                    log.error('connect %s failed: %s',str(taddr), str(e))
                                    self.gen_send.send((mngt_prefix+b'sodie',sid))
                            # connection die
                            elif (bmsg == mngt_prefix+b'sodie' and 
                                    sid in self.sdict.keys()):
                                log.info('[%d] close sid %d by peer', port, sid)
                                self.clean(sid)
                            # heartbeat
                            elif bmsg == hb_bmsg:
                                if self.role == 's':
                                    log.info('[%d] recv and send heartbeat, sid %d', port, sid)
                                    self.gen_send.send((hb_bmsg,sid))
                                else:
                                    log.info('[%d] recv heartbeat', port)
                            # data
                            else:
                                try:
                                    if sid in self.sdict.keys():
                                        self.sdict[sid][1] += bmsg
                                        self.send_sk_nonblock(sid)
                                except OSError:
                                    log.info('[%d] sid %d is closed while send', port, sid)
                                    self.gen_send.send((mngt_prefix+b'sodie',sid))
                                    self.clean(sid)
                        else:
                            break

                # recv from connections
                else:
                    try:
                        sid = self.kdict[fd.fileobj]
                    except KeyError:
                        continue
                    gen_data = trafix.recv_sk_nonblock(fd.fileobj)
                    while True:
                        try:
                            data = next(gen_data)
                        except OSError:
                            log.info('[%d] sid %d is donw while recv', port, sid)
                            self.gen_send.send((mngt_prefix+b'sodie',sid))
                            self.clean(sid, fd.fileobj)
                            break
                        except StopIteration:
                            break
                        self.gen_send.send((data,sid))  # send data


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
            trafix.close_socket(sk)


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


