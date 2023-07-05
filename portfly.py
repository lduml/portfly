#!/usr/bin/env python3
"""
Remote Port Forwarding in pure Python featured by Non-Blocking socket
and Event IO.

Author:   xinlin-z
Github:   https://github.com/xinlin-z/portfly
Blog:     https://cs.pynote.net
License:  MIT
"""
import socket
import selectors
import logging as log
import argparse
import random
import base64
import multiprocessing as mp
import threading
import time
from typing import Iterator, Generator
from dataclasses import dataclass


# type alias
sk_t = socket.socket


def cx(bmsg: bytes) -> bytes:
    r = random.randint
    m = r(0,255)
    return base64.b64encode(bytes([m]+[i^m for i in bmsg])
                          + bytes([i^r(0,255) for i in range(m)]))


def dx(bmsg: bytes) -> bytes:
    bmsg = base64.b64decode(bmsg)
    return bytes([i^bmsg[0] for i in bmsg[1:len(bmsg)-bmsg[0]]])


def nrclose_socket(sk: sk_t) -> None:
    """ non-raise close socket """
    try:
        sk.shutdown(socket.SHUT_RDWR)
        sk.close()
    except OSError:
        return


SK_IO_CHUNK_LEN = 4096
MAX_STREAM_ID   = 0xFFFFFFFF
HEARTBEAT_INTV  = 30


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

    @dataclass
    class sk_buf:
        sk: sk_t
        buf: bytes = b''

    def send_sk_nonblock_gen(self, sk: sk_t) \
                    -> Generator[int, tuple[bytes|None,int], None]:
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

    def recv_sk_nonblock_gen(self, sk: sk_t) \
                    -> Iterator[tuple[int|None,bytes,bytes]]:
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

    def recv_sk_nonblock(self, sk: sk_t) -> Iterator[bytes]:
        """ socket nonblocking recv generator, one shot """
        while True:
            try:
                data = sk.recv(SK_IO_CHUNK_LEN)
                if len(data) == 0:
                    raise ConnectionError('recv_sk_nonblock recv 0')
                yield data
            except BlockingIOError:
                return

    def send_sk_nonblock(self, sid: int) -> int:
        skb = self.sdict[sid]
        data = skb.buf
        try:
            while True:
                if len(data) == 0:
                    break
                if (i:=skb.sk.send(data[:SK_IO_CHUNK_LEN])) == -1:
                    raise ConnectionError('send_sk_nonblock send -1')
                data = self.sdict[sid].buf = data[i:]
        except BlockingIOError:
            pass
        return len(data)

    def flush(self) -> int:
        """ flush all sending socket, return left bytes number """
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

    def __init__(self, sk: sk_t, role: str, *argv) -> None:
        self.role: str = role  # 's':server or 'c':client

        # set tunnel socket to nonblocking, init generators
        self.sk: sk_t = sk
        self.sk.setblocking(False)
        self.gen_recv = self.recv_sk_nonblock_gen(sk)
        self.gen_send = self.send_sk_nonblock_gen(sk)
        next(self.gen_send)

        # selector
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.sk, selectors.EVENT_READ)

        # retrive argv and set accordingly
        self.port: int = argv[1]
        self.x: bool = argv[2]
        if self.role == 's':
            self.pserv = argv[0]  # no need to set pserv to nonblocking
            self.sid = 1          # sid, stream id
            self.sel.register(self.pserv, selectors.EVENT_READ)
        else:
            self.target = argv[0]
            self.heartbeat_time = time.time()
            self.heartbeat_max = 0

        self.sdict: dict[int, trafix.sk_buf] = {}
        self.kdict: dict[sk_t,int] = {}    # socket --> sid
        self.reg: int = 0
        self.unreg: int = 0

        # event loop
        try:
            self.loop()
        except Exception as e:
            log.error('[%d] exception: %s', self.port, str(e))
            log.exception(e)
            for skb in self.sdict.values():
                nrclose_socket(skb.sk)
        # the end
        nrclose_socket(self.sk)
        if self.role == 's':
            nrclose_socket(self.pserv)
        log.warning('[%d] closed', self.port)

    def try_send_heartbeat(self) -> None:
        if self.heartbeat_max > 10:
            raise ValueError('heartbeat max is reached')
        now = time.time()
        if now - self.heartbeat_time > HEARTBEAT_INTV:
            self.gen_send.send((MSG_HB,0))
            log.info('[%d] send heartbeat', self.port)
            self.heartbeat_time = now
            self.heartbeat_max += 1

    def update_sid(self) -> None:
        # sid 0 is used for heartbeat,
        # sid should be incremented sequentially to avoid conflict.
        while True:
            self.sid = self.sid+1 if self.sid!=MAX_STREAM_ID else 1
            if self.sid not in self.sdict.keys():
                break

    def clean(self, sid: int) -> None:
        """ delete sid from sdict,
            delete sk from kdict,
            close socket,
            unregister sk from selector. """
        assert len(self.sdict) == len(self.kdict)
        _skb = self.sdict.pop(sid)
        sk = _skb.sk
        if sk:
            self.kdict.pop(sk, None)
            nrclose_socket(sk)
            self.sel.unregister(sk)
            self.unreg += 1
            log.debug('[%d] unreg %d', self.port, self.unreg)

    def event_pass(self, events) -> None:
        p = self.port
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
                self.sdict[self.sid] = trafix.sk_buf(s)
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
                                log.debug('[%d] reg %d', p, self.reg)
                                self.sdict[sid] = trafix.sk_buf(s)
                                self.kdict[s] = sid
                            except OSError as e:
                                log.error('[%d] connect %s failed: %s', p, str(self.target), str(e))
                                self.gen_send.send((MSG_CD,sid))
                        # connection down
                        elif t == MSG_CD:
                            if sid in self.sdict.keys():
                                log.info('[%d] close sid %d by peer', p, sid)
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
                                    self.sdict[sid].buf += bmsg
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
                        log.info('[%d] sid %d donw when recv, %s',p,sid,str(e))
                        self.gen_send.send((MSG_CD,sid))
                        self.clean(sid)
                        break
                    except StopIteration:
                        break
                    self.gen_send.send((MSG_ND+data,sid))  # send data

    def loop(self) -> None:
        while True:
            # server is always passive, so it can be blocked forever...
            # but client can not, which needs to send heartbeat.
            bytes_left = self.flush()
            if bytes_left != 0:
                log.info('[%d] flushed, bytes left %d', self.port, bytes_left)
                events = self.sel.select(0)  # just a polling
            else:
                if self.role == 's':
                    events = self.sel.select()
                else:
                    events = self.sel.select(HEARTBEAT_INTV)
            # if no socket ready to be read,
            if len(events) == 0:
                # it might be a chance to send heartbeat.
                if self.role == 'c':
                    self.try_send_heartbeat()
                # it's better to wait a while before next flush.
                if bytes_left != 0:
                    time.sleep(0.1)
                continue
            self.event_pass(events)


# tunnel init msg
magic_bmsg = b'ask for REopening a PORT'
magic_breply = b'Done'


def server_main(saddr: tuple[str,int]) -> None:
    serv = socket.create_server(saddr)
    log.warning('start portfly server at %s', str(saddr))
    while True:
        sk, faddr = serv.accept()
        log.warning('accept from %s', str(faddr))
        sk.settimeout(2)
        sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
        rf = sk.makefile('rb')
        try:
            if dx(rf.readline().strip()) == magic_bmsg:
                # recv mode
                mode = dx(rf.readline().strip())
                log.warning('mode %s', mode)
                if mode == b'R':  # recv port
                    port = int(dx(rf.readline().strip()))
                    pserv = socket.create_server(('', port))
                    log.warning('create server at public port %d', port)
                else:  # recv thost and tport
                    thost = dx(rf.readline().strip())
                    tport = int(dx(rf.readline().strip()))
                    log.warning('recv target addr %s:%s', thost, tport)
                # recv x
                x = eval((dx(rf.readline().strip())).decode())
                log.warning('encryption %d', x)
                sk.sendall(cx(magic_breply) + b'\n')
                log.warning('launch process...')
                if mode == b'R':
                    args = (sk, 's', pserv, port, x)
                else:
                    args = (sk, 'c', (thost,tport), -1, x)
                mp.Process(target=trafix, args=args).start()
            else:
                raise ValueError('magic bmsg error')
        except Exception as e:
            log.error('exception %s', str(faddr))
            log.exception(e)
            nrclose_socket(sk)


def client_main(mode: str,
                setting: str,
                saddr: tuple[str,int], x: bool) -> None:
    pub_port, thost, tport = setting.strip().split(':')

    while True:
        try:
            if mode == b'L':
                pserv = socket.create_server(('', int(pub_port)))
                log.warning('create server at port %s', pub_port)
            sk = socket.create_connection(saddr)
            sk.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
            sk.sendall(cx(magic_bmsg) + b'\n')
            sk.sendall(cx(mode) + b'\n')
            if mode == b'R':
                sk.sendall(cx(pub_port.encode()) + b'\n')
            else:
                sk.sendall(cx(thost.encode()) + b'\n')
                sk.sendall(cx(tport.encode()) + b'\n')
            sk.sendall(cx(str(int(x)).encode()) + b'\n')
            rf = sk.makefile('rb')
            if dx(rf.readline().strip()) == magic_breply:
                log.warning('connect server %s ok, port %s is ready.',
                                                        str(saddr), pub_port)
            else:
                raise ValueError('magic_breply is not match')
            # start
            if mode == b'R':
                args = (sk, 'c', (thost,int(tport)), -1, x)
            else:
                args = (sk, 's', pserv, int(pub_port), x)
            th = threading.Thread(target=trafix, args=args, daemon=True)
            th.start()
            th.join()
        except Exception as e:
            log.error('exception %s', str(e))
            log.exception(e)
        finally:
            nrclose_socket(sk)
            time.sleep(4)


_VER = 'portfly V0.21 by xinlin-z '\
       'https://github.com/xinlin-z/portfly'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-V', '--version', action='version', version=_VER)
    parser.add_argument('--log', choices=('INFO','DEBUG'), default='WARNING')
    parser.add_argument('-x', action='store_true',
                        help='apply simple encryption to traffic')
    end_type = parser.add_mutually_exclusive_group(required=True)
    end_type.add_argument('-s', '--server', action='store_true')
    end_type.add_argument('-c', '--client', action='store_true')
    parser.add_argument('-L', action='store_true',
                        help='local port forwarding')
    parser.add_argument('settings')
    args = parser.parse_args()

    log.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s',
                    level=eval('log.'+args.log))

    # python portfly.py -s server_listen_ip:port
    if args.server:
        if args.x:
            print('-x is ignored in server side')
        if args.L:
            print('-L is ignored in server side')
        ip, port = args.settings.split(':')
        server_main((ip,int(port)))
    # python portfly.py -c mapping_port:target_ip:port+server_ip:port
    else:
        mapping, saddr = args.settings.split('+')
        addr, port = saddr.split(':')
        client_main(b'L' if args.L else b'R',
                    mapping, (addr,int(port)), args.x)


