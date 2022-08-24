import random
import base64
import socket


magic_bmsg = b'ask for REopening a PORT'
magic_breply = b'Done'
mngt_prefix = b'%-[(=!^$#@~+|0|+~@#$^!=)]-%'
hb_bmsg = mngt_prefix + b'hello'


class List(list):
    """ no-raise-remove list """
    def remove(self, x):
        try:
            super().remove(x)
        except ValueError:
            pass


def close_so(so):
    """ socket no-raise-close interface """
    if so:
        try:
            so.shutdown(socket.SHUT_RDWR)
            so.close()
        except OSError:
            pass


def cx(bmsg: bytes) -> bytes:
    r = random.randint
    m = r(0,255)
    return base64.b64encode(bytes([m]+[i^m for i in bmsg])
                          + bytes([i^r(0,255) for i in range(m)]))


def dx(bmsg: bytes) -> bytes:
    bmsg = base64.b64decode(bmsg)
    return bytes([i^bmsg[0] for i in bmsg[1:len(bmsg)-bmsg[0]]])


class sosr():
    """ socket send and recv """

    @staticmethod
    def get_send(x):
        if x is True:
            return sosr._tbsendx
        else:
            return sosr._tbsend

    @staticmethod
    def get_recv(x):
        if x is True:
            return sosr._tbrecvx
        else:
            return sosr._tbrecv

    @staticmethod
    def _tbsend(so, bmsg, sid):
        """8 bytes prefix,
           4 for length, little endian,
           4 for streamid, big endian.
        """
        bmsg = (len(bmsg)+8).to_bytes(4, 'little') \
                       + sid.to_bytes(4, 'big') \
                       + bmsg
        so.sendall(bmsg)  # if error, sendall would raise

    @staticmethod
    def _tbsend2(so, bmsg, sid):
        bmsg = (len(bmsg)+8).to_bytes(4, 'little') \
                       + sid.to_bytes(4, 'big') \
                       + bmsg
        msglen = len(bmsg)
        i = j = 0
        while i < msglen:
            j = so.send(bmsg[i:])
            if j == 0:
                raise ConnectionError('socket broken (tsend_all) %d'%i)
            i += j

    @staticmethod
    def _tbsendx(so, bmsg, sid):
        _bm = cx(sid.to_bytes(4,'big')+bmsg)
        bmsg = (len(_bm)+4).to_bytes(4, 'little') + _bm
        so.sendall(bmsg)  # if error, sendall would raise

    @staticmethod
    def _tbsendx2(so, bmsg, sid):
        _bm = cx(sid.to_bytes(4,'big')+bmsg)
        bmsg = (len(_bm)+4).to_bytes(4, 'little') + _bm
        msglen = len(bmsg)
        i = j = 0
        while i < msglen:
            j = so.send(bmsg[i:])
            if j == 0:
                raise ConnectionError('socket broken (tsend_all) %d'%i)
            i += j

    @staticmethod
    def _tbrecv(so):
        """recv a msg sent by tbsend"""
        bmsg = b''
        while len(bmsg) < 4:
            chunk = so.recv(4-len(bmsg))
            if chunk == b'':
                raise ConnectionError('socket broken (tbrecv recv 0) E1')
            bmsg += chunk
        msglen = int.from_bytes(bmsg, 'little')
        while len(bmsg) < msglen:
            chunk = so.recv(msglen-len(bmsg))
            if chunk == b'':
                raise ConnectionError('socket broken (tbrecv recv 0) E2')
            bmsg += chunk
        return int.from_bytes(bmsg[4:8],'big'), bmsg[8:]  # sid, bmsg

    @staticmethod
    def _tbrecvx(so):
        bmsg = b''
        while len(bmsg) < 4:
            chunk = so.recv(4-len(bmsg))
            if chunk == b'':
                raise ConnectionError('socket broken (tbrecv recv 0) E1')
            bmsg += chunk
        msglen = int.from_bytes(bmsg, 'little')
        while len(bmsg) < msglen:
            chunk = so.recv(msglen-len(bmsg))
            if chunk == b'':
                raise ConnectionError('socket broken (tbrecv recv 0) E2')
            bmsg += chunk
        _bm = dx(bmsg[4:])
        return int.from_bytes(_bm[:4],'big'), _bm[4:]  # sid, bmsg


