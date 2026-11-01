"""Protocolo de túnel do NoPingeyson (formato próprio, simples e documentado).

Todo pacote UDP trocado entre cliente e relays tem este formato:

    magic   2 bytes  b"NP"
    versão  1 byte
    tipo    1 byte   (ver constantes T_*)
    sessão  8 bytes  identificador aleatório do cliente
    caminho 1 byte   id do caminho (path) usado, para o retorno seguir a mesma rota
    seq     4 bytes  número de sequência (usado para descartar duplicatas)
    nhops   1 byte   quantos saltos ainda faltam
    hops    6*n      (ipv4 4 bytes + porta 2 bytes) dos próximos relays
    corpo   variável
    mac     16 bytes HMAC-SHA256 truncado sobre tudo o que vem antes

Pacotes "de ida" (PROBE, DATA, TPING) andam pela lista de hops; cada relay
remove o primeiro hop e repassa. O último relay (saída) responde. Pacotes "de
volta" (PROBE_ACK, DATA_BACK, TPING_ACK) seguem o caminho inverso usando a
tabela (sessão, caminho) -> salto anterior que cada relay mantém.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import struct
import sys
from dataclasses import dataclass, field

MAGIC = b"NP"
VERSION = 1
MAC_LEN = 16

T_PROBE = 1
T_PROBE_ACK = 2
T_DATA = 3
T_DATA_BACK = 4
T_TPING = 5
T_TPING_ACK = 6

FORWARD_TYPES = {T_PROBE, T_DATA, T_TPING}
BACKWARD_TYPES = {T_PROBE_ACK, T_DATA_BACK, T_TPING_ACK}

_HDR = struct.Struct("!2sBB8sBIB")  # 18 bytes
_HOP = struct.Struct("!4sH")
HEADER_LEN = _HDR.size

Addr = tuple[str, int]


class BadPacket(Exception):
    pass


@dataclass
class Packet:
    type: int
    session: bytes
    path_id: int
    seq: int
    hops: list[Addr] = field(default_factory=list)
    body: bytes = b""

    def encode(self, key: bytes) -> bytes:
        if len(self.session) != 8:
            raise ValueError("sessão deve ter 8 bytes")
        parts = [
            _HDR.pack(MAGIC, VERSION, self.type, self.session, self.path_id,
                      self.seq & 0xFFFFFFFF, len(self.hops))
        ]
        for ip, port in self.hops:
            parts.append(_HOP.pack(socket.inet_aton(ip), port))
        parts.append(self.body)
        raw = b"".join(parts)
        return raw + _mac(key, raw)

    @classmethod
    def decode(cls, data: bytes, key: bytes) -> "Packet":
        if len(data) < HEADER_LEN + MAC_LEN:
            raise BadPacket("curto demais")
        raw, mac = data[:-MAC_LEN], data[-MAC_LEN:]
        if not hmac.compare_digest(mac, _mac(key, raw)):
            raise BadPacket("mac inválido")
        magic, ver, typ, session, path_id, seq, nhops = _HDR.unpack_from(raw, 0)
        if magic != MAGIC or ver != VERSION:
            raise BadPacket("versão/magic desconhecidos")
        off = HEADER_LEN
        hops = []
        for _ in range(nhops):
            if off + _HOP.size > len(raw):
                raise BadPacket("hops truncados")
            ip, port = _HOP.unpack_from(raw, off)
            hops.append((socket.inet_ntoa(ip), port))
            off += _HOP.size
        return cls(typ, session, path_id, seq, hops, raw[off:])


def _mac(key: bytes, raw: bytes) -> bytes:
    return hmac.new(key, raw, hashlib.sha256).digest()[:MAC_LEN]


# ---- corpos ---------------------------------------------------------------

_PROBE = struct.Struct("!Id")          # probe_id, horário de envio (cliente)
_DATA = struct.Struct("!H4sH")          # flow_id, ip destino, porta destino
_DATA_BACK = struct.Struct("!H")        # flow_id
_TPING = struct.Struct("!4s")           # ip alvo
_TPING_ACK = struct.Struct("!4sf")      # ip alvo, rtt em ms (-1 = sem resposta)


def probe_body(probe_id: int, t: float) -> bytes:
    return _PROBE.pack(probe_id, t)


def parse_probe(body: bytes) -> tuple[int, float]:
    return _PROBE.unpack_from(body, 0)


def data_body(flow_id: int, target: Addr, payload: bytes) -> bytes:
    return _DATA.pack(flow_id, socket.inet_aton(target[0]), target[1]) + payload


def parse_data(body: bytes) -> tuple[int, Addr, bytes]:
    if len(body) < _DATA.size:
        raise BadPacket("DATA curto")
    flow_id, ip, port = _DATA.unpack_from(body, 0)
    return flow_id, (socket.inet_ntoa(ip), port), body[_DATA.size:]


def data_back_body(flow_id: int, payload: bytes) -> bytes:
    return _DATA_BACK.pack(flow_id) + payload


def parse_data_back(body: bytes) -> tuple[int, bytes]:
    if len(body) < _DATA_BACK.size:
        raise BadPacket("DATA_BACK curto")
    (flow_id,) = _DATA_BACK.unpack_from(body, 0)
    return flow_id, body[_DATA_BACK.size:]


def tping_body(ip: str) -> bytes:
    return _TPING.pack(socket.inet_aton(ip))


def parse_tping(body: bytes) -> str:
    return socket.inet_ntoa(_TPING.unpack_from(body, 0)[0])


def tping_ack_body(ip: str, rtt_ms: float) -> bytes:
    return _TPING_ACK.pack(socket.inet_aton(ip), rtt_ms)


def parse_tping_ack(body: bytes) -> tuple[str, float]:
    ip, rtt = _TPING_ACK.unpack_from(body, 0)
    return socket.inet_ntoa(ip), rtt


def is_public_ipv4(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_global
    except ValueError:
        return False


class SeqWindow:
    """Janela deslizante para descartar duplicatas (cópias vindas do multipath)."""

    def __init__(self, size: int = 4096):
        self.size = size
        self.highest = -1
        self.seen: set[int] = set()

    def accept(self, seq: int) -> bool:
        if self.highest >= 0:
            # trata a virada do contador de 32 bits
            delta = (seq - self.highest) & 0xFFFFFFFF
            if delta >= 0x80000000:  # seq é mais antigo que o maior visto
                if (self.highest - seq) & 0xFFFFFFFF >= self.size:
                    return False
        if seq in self.seen:
            return False
        self.seen.add(seq)
        if self.highest < 0 or ((seq - self.highest) & 0xFFFFFFFF) < 0x80000000:
            self.highest = seq
        if len(self.seen) > self.size * 2:
            low = (self.highest - self.size) & 0xFFFFFFFF
            self.seen = {s for s in self.seen
                         if ((s - low) & 0xFFFFFFFF) < 0x80000000}
        return True


def tune_socket(transport, size: int = 1 << 20):
    """Aumenta os buffers do socket UDP (o padrão do Windows perde pacotes em rajadas)."""
    sock = transport.get_extra_info("socket")
    for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, size)
        except (OSError, AttributeError):
            pass
    if sys.platform == "win32":
        _disable_udp_connreset(sock)


def _disable_udp_connreset(sock):
    """No Windows, um ICMP "porta inalcançável" (ex.: um relay caiu) gera
    WSAECONNRESET no socket UDP e o asyncio para de ler dele: o túnel inteiro
    ficaria surdo. SIO_UDP_CONNRESET = FALSE desliga esse comportamento."""
    import ctypes
    from ctypes import wintypes
    SIO_UDP_CONNRESET = 0x9800000C
    flag = wintypes.BOOL(False)
    returned = wintypes.DWORD()
    ws2 = ctypes.WinDLL("ws2_32")
    ws2.WSAIoctl(ctypes.c_size_t(sock.fileno()), SIO_UDP_CONNRESET, ctypes.byref(flag),
                 ctypes.sizeof(flag), None, 0, ctypes.byref(returned), None, None)


def load_key(text: str) -> bytes:
    text = text.strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        key = text.encode()
    if len(key) < 16:
        raise ValueError("chave curta demais (use `python -m nopingeyson keygen`)")
    return key
