#!/usr/bin/env python3
"""
Remote Port Forwarding in pure Python featured by Non-Blocking socket
and Event IO.

Author:   xinlin-z
Github:   https://github.com/xinlin-z/portfly
Blog:     https://cs.pynote.net
License:  MIT
"""
import sys
import socket
import selectors
import logging as log
import argparse
import random
import base64
import multiprocessing as mp
import threading
import time


def cx(bmsg: bytes) -> bytes:
    r = random.randint
    m = r(0,255)
    return base64.b64encode(bytes([m]+[i^m for i in bmsg])
                          + bytes([i^r(0,255) for i in range(m)]))


def dx(bmsg: bytes) -> bytes:
    bmsg = base64.b64decode(bmsg)
    return bytes([i^bmsg[0] for i in bmsg[1:len(bmsg)-bmsg[0]]])


def nrclose_socket(sk):
    """ non-raise close socket """
    if sk:
        try:
            sk.shutdown(socket.SHUT_RDWR)
            sk.close()
        except OSError:
            return


SK_IO_CHUNK_LEN = 4096
MAX_STREAM_ID   = 0xFFFFFFFF


"""
Message Format:

* 4 bytes, total length, little endian
* 4 bytes, stream id, big endian
* 1 byte, type:
    0x01, Heart Beat (zero payload)
    0x02, Normal Data
    0x03, New Connection (zero payload)
    0x04, Connection Down (zero payload)
* variable length payload
"""
MSG_HB = b'\x01'
MSG_ND = b'\x02'
MSG_NC = b'\x03'
MSG_CD = b'\x04'


class trafix():
    """ traffic exchanging class """

    def send_sk_nonblock_gen(self, sk):
        """ socket nonblocking send generator """
        data = b''
        while True:
            bmsg, sid = yield len(data)
            if bmsg is not None:
                if self.x: bmsg = cx(bmsg)
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

    def recv_sk_nonblock_gen(self, sk):
        """ socket nonblocking recv generator,
            yield sid,type,msg """
        data = b''
        while True:
            try:
                d = sk.recv(SK_IO_CHUNK_LEN)
                if len(d) == 0:
                    raise ConnectionError('recv_sk_nonblock_gen recv 0')
                data += d
                while (dlen:=len(data)) > 4:
                    mlen = int.from_bytes(data[:4], 'little')
                    if dlen >= mlen:
                        sid = int.from_bytes(data[4:8], 'big')
                        msg = dx(data[8:mlen]) if self.x else data[8:mlen]
                        yield sid, msg[:1], msg[1:]
                        data = data[mlen:]
                    else:
                        break
            except BlockingIOError:
                yield None, b'\x00', b''

    def recv_sk_nonblock(self, sk):
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

    def flush(self):
        tunnel_left = self.gen_send.send((None,0))
        sk_left = 0
        for sid in self.sdict.keys():
            try:
                sk_left += self.send_sk_nonblock(sid)
            except OSError as e:
                log.info('[%d] sid %d down while flush',self.port,sid,str(e))
                tunnel_left = self.gen_send.send((MSG_CD,sid))
                self.clean(sid)
        return tunnel_left + sk_left

    def __init__(self, sk, role, *argv):
        self.role = role  # 's':server or 'c':client

        # set tunnel socket to nonblocking, init generators
        self.sk = sk
        self.sk.setblocking(False)
        self.gen_recv = self.recv_sk_nonblock_gen(sk)
        self.gen_send = self.send_sk_nonblock_gen(sk)
        next(self.gen_send)

        # selector
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sk, selectors.EVENT_READ)

        # retrive argv and set accordingly
        self.port = argv[1]
        self.x = argv[2]
        if self.role == 's':
            self.pserv = argv[0]  # no need to set pserv to nonblocking
            self.sid = 1          # sid, stream id
            self.sel.register(self.pserv, selectors.EVENT_READ)
        else:
            self.target = argv[0]
            self.heartbeat_time = time.time()
            self.heartbeat_max = 0

        self.sdict = {}    # sid --> socket
        self.kdict = {}    # socket --> sid
        self.reg = 0
        self.unreg = 0

        # event loop
        try:
            self.loop()
        except Exception as e:
            log.error('exception [%d]: %s', self.port, str(e))
            log.exception(e)
            for s,_ in self.sdict.values():
                nrclose_socket(s)
        # the end
        nrclose_socket(self.sk)
        if self.role == 's':
            nrclose_socket(self.pserv)
        log.warning('[%d] closed', self.port)

    def try_send_heartbeat(self):
        if self.heartbeat_max > 6:
            raise ValueError('heartbeat max is reached')
        now = time.time()
        if now - self.heartbeat_time > 30:  # minimal heartbeat interval
            self.gen_send.send((MSG_HB,0))
            log.info('[%d] send heartbeat', self.port)
            self.heartbeat_time = now
            self.heartbeat_max += 1

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
            close socket,
            unregister sk from selector. """
        assert len(self.sdict) == len(self.kdict)
        _sk, _ = self.sdict.pop(sid, (None,None))
        if sk:
            assert sk is _sk
        else:
            sk = _sk
        if sk:
            self.kdict.pop(sk, None)
            nrclose_socket(sk)
            self.sel.unregister(sk)
            self.unreg += 1
            log.debug('[%d] unreg %d', self.port, self.unreg)

    def loop(self):
        p = self.port
        while True:
            # server is always passive, so it can be blocked forever...
            # but client can not, which needs to send heartbeat.
            bytes_left = self.flush()
            if bytes_left != 0:
                log.info('[%d] flushed, bytes left %d', p, bytes_left)
                events = self.sel.select(0)  # just a polling
            else:
                if self.role == 's':
                    events = self.sel.select()
                else:
                    events = self.sel.select(9)

            # if no socket ready to be read,
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
                    s, addr = self.pserv.accept()
                    self.gen_send.send((MSG_NC,self.sid))
                    log.info('[%d] accept %s, sid %d', p, str(addr), self.sid)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                    s.setblocking(False)             # set nonblocking
                    self.sel.register(s, selectors.EVENT_READ)
                    self.reg += 1
                    log.debug('[%d] reg %d', self.port, self.reg)
                    self.sdict[self.sid] = [s, b'']  # sid -> [sk,buffer]
                    self.kdict[s] = self.sid
                    self.update_sid()

                # recv from tunnel
                elif fd.fileobj == self.sk:
                    while True:
                        sid, t, bmsg = next(self.gen_recv)
                        if sid is not None:
                            # new connection in client role
                            if t == MSG_NC:
                                try:
                                    s = socket.create_connection(self.target, timeout=2)
                                    log.info('[%d] connect target %s ok, sid %d', p, str(self.target), sid)
                                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
                                    s.setblocking(False)
                                    self.sel.register(s, selectors.EVENT_READ)
                                    self.reg += 1
                                    log.debug('[%d] reg %d',self.port,self.reg)
                                    self.sdict[sid] = [s, b'']
                                    self.kdict[s] = sid
                                except OSError as e:
                                    log.error('connect %s failed: %s', str(self.target), str(e))
                                    self.gen_send.send((MSG_CD,sid))
                            # connection down
                            elif t == MSG_CD:
                                if sid in self.sdict.keys():
                                    log.info('[%d] close sid %d by peer',p,sid)
                                    self.clean(sid)
                            # heartbeat
                            elif t == MSG_HB:
                                if self.role == 's':
                                    log.info('[%d] recv and send heartbeat, sid %d', p, sid)
                                    self.gen_send.send((MSG_HB,sid))
                                else:
                                    log.info('[%d] recv heartbeat', p)
                                    self.heartbeat_max = 0
                            # data
                            else:
                                assert t == MSG_ND
                                try:
                                    if sid in self.sdict.keys():
                                        self.sdict[sid][1] += bmsg
                                        self.send_sk_nonblock(sid)
                                except OSError:
                                    log.info('[%d] sid %d is closed while send', p, sid)
                                    self.gen_send.send((MSG_CD,sid))
                                    self.clean(sid)
                        else:
                            break

                # recv from connections
                else:
                    try:
                        sid = self.kdict[fd.fileobj]
                    except KeyError:
                        continue
                    gen_data = self.recv_sk_nonblock(fd.fileobj)
                    while True:
                        try:
                            data = next(gen_data)
                        except OSError as e:
                            log.info('[%d] sid %d donw while recv, %s',p,sid,str(e))
                            self.gen_send.send((MSG_CD,sid))
                            self.clean(sid, fd.fileobj)
                            break
                        except StopIteration:
                            break
                        self.gen_send.send((MSG_ND+data,sid))  # send data


# tunnel init msg
magic_bmsg = b'ask for REopening a PORT'
magic_breply = b'Done'


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
                log.warning('launch process...')
                mp.Process(target=trafix,
                           args=(sk,'s',pserv,port,x)).start()
            else:
                raise ValueError('magic bmsg error')
        except Exception as e:
            log.error('exception %s', str(faddr))
            log.exception(e)
            nrclose_socket(sk)


def client_main(setting, saddr, x):
    pub_port, host, port = setting.strip().split(':')
    serv_ip, serv_port = saddr.strip().split(':')
    serv_addr = (serv_ip, int(serv_port))

    try:
        sk = socket.create_connection(serv_addr)
        sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
        sk.sendall(cx(magic_bmsg) + b'\n')
        sk.sendall(cx(pub_port.encode()) + b'\n')
        sk.sendall(cx(str(int(x)).encode()) + b'\n')
        rf = sk.makefile('rb')
        if dx(rf.readline().strip()) == magic_breply:
            log.warning('Connect server %s ok, port %s is ready.',
                                                    serv_ip, pub_port)
        else:
            raise ValueError('magic_breply is not match')
    except Exception as e:
        log.exception(e)
        nrclose_socket(sk)
        sys.exit(1)

    target_addr = (host, int(port))
    th = threading.Thread(target=trafix,
                          args=(sk,'c',target_addr,int(pub_port),x),
                          daemon=True)
    th.start()
    th.join()


_VER = 'portfly V0.20 by xinlin-z '\
       'https://github.com/xinlin-z/portfly'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-V', '--version', action='version', version=_VER)
    parser.add_argument('--log', choices=('INFO','DEBUG'))
    parser.add_argument('-x', action='store_true',
                        help='apply simple encryption to traffic')
    end_type = parser.add_mutually_exclusive_group(required=True)
    end_type.add_argument('-s', '--server', action='store_true')
    end_type.add_argument('-c', '--client', action='store_true')
    parser.add_argument('settings')
    args = parser.parse_args()

    log_level = 'log.' + (args.log if args.log else 'WARNING')
    log.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s',
                    level=eval(log_level))

    # python portfly.py -s server_listen_ip:port
    if args.server:
        if args.x:
            print('-x is ignored in server side')
        ip, port = args.settings.split(':')
        server_main((ip,int(port)))
    # python portfly.py -c mapping_port:target_ip:port+server_ip:port
    else:
        mapping, saddr = args.settings.split('+')
        client_main(mapping, saddr, args.x)


