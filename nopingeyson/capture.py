"""Captura de tráfego por aplicativo no Windows usando WinDivert (via pydivert).

Fluxo:
  1. WinDivert entrega cada datagrama UDP de saída (exceto o do próprio túnel
     e o que vai para os relays).
  2. Descobrimos qual processo é dono da porta local (tabela UDP do Windows).
  3. Se for um jogo da lista, o datagrama NÃO segue: o payload vai para o
     túnel. Senão, é reinjetado intacto.
  4. Respostas que chegam pelo túnel são montadas como um pacote IPv4/UDP
     "vindo" do servidor do jogo e injetadas como entrada na mesma interface,
     então o jogo não percebe nada.

Precisa rodar como Administrador (o driver WinDivert exige).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import threading
import time

from .tunnel import DIRECT, Tunnel

log = logging.getLogger("nopingeyson.capture")


class ProcessMap:
    """(protocolo, porta local) -> nome do executável (minúsculo)."""

    def __init__(self, ttl: float = 30.0, kind: str = "udp4", min_refresh: float = 0.0):
        import psutil
        self.psutil = psutil
        self.ttl = ttl
        self.kind = kind
        self.min_refresh = min_refresh     # evita reler a tabela a cada porta nova
        self.last_refresh = 0.0
        self.cache: dict[tuple[str, int], tuple[str, int, float]] = {}
        self.names: dict[int, str] = {}
        self.lock = threading.Lock()

    def lookup(self, port: int, proto: str = "udp") -> tuple[str, int]:
        now = time.monotonic()
        key = (proto, port)
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[2] < self.ttl:
                return hit[0], hit[1]
            if now - self.last_refresh >= self.min_refresh:
                self._refresh(now)
            hit = self.cache.get(key)
            if not hit:
                self.cache[key] = ("", 0, now)
                return "", 0
            return hit[0], hit[1]

    def _refresh(self, now: float):
        self.last_refresh = now
        try:
            conns = self.psutil.net_connections(kind=self.kind)
        except Exception as e:  # sem permissão etc.
            log.warning("não consegui ler a tabela de conexões: %s", e)
            return
        for c in conns:
            if not c.laddr or not c.pid:
                continue
            name = self.names.get(c.pid)
            if name is None:
                try:
                    name = self.psutil.Process(c.pid).name().lower()
                except Exception:
                    name = ""
                self.names[c.pid] = name
            proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
            self.cache[(proto, c.laddr.port)] = (name, c.pid, now)
        if len(self.names) > 2000:
            self.names.clear()


def build_udp_ipv4(src: tuple[str, int], dst: tuple[str, int], payload: bytes) -> bytes:
    """Monta IPv4+UDP cru. Os checksums são recalculados pelo WinDivert ao enviar."""
    udp_len = 8 + len(payload)
    total = 20 + udp_len
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, int.from_bytes(os.urandom(2), "big"),
                     0, 64, socket.IPPROTO_UDP, 0,
                     socket.inet_aton(src[0]), socket.inet_aton(dst[0]))
    udp = struct.pack("!HHHH", src[1], dst[1], udp_len, 0)
    return ip + udp + payload


class Capture:
    def __init__(self, tunnel: Tunnel, loop: asyncio.AbstractEventLoop, games: list[str],
                 exclude_ports: list[int] | None = None, on_game_seen=None, profile_for=None):
        try:
            import pydivert
        except ImportError as e:
            raise RuntimeError("pydivert não está instalado. Rode: pip install -r requirements.txt") from e
        self.pydivert = pydivert
        self.tunnel = tunnel
        self.loop = loop
        self.games = {g.lower() for g in games}
        self.exclude_ports = exclude_ports or []
        self.on_game_seen = on_game_seen or (lambda name, pid: None)
        self.profile_for = profile_for or (lambda name: None)
        self.procs = ProcessMap()
        self.local: dict[int, tuple[str, tuple[int, int]]] = {}   # lport -> (ip local, interface)
        self.handle = None
        # handle só para injetar respostas: o handle de captura fica bloqueado no
        # recv() e, num handle síncrono, o send() de outra thread esperaria o
        # próximo pacote de saída para andar
        self.inject_handle = None
        self.thread: threading.Thread | None = None
        self.running = False
        self.stats = {"captured": 0, "passed": 0, "injected": 0}
        tunnel.on_downstream = self.inject
        tunnel.on_direct = self._reinject

    def _filter(self) -> str:
        # o WinDivert 1.3 do pydivert não tem o campo "loopback": exclui 127/8 na mão
        parts = ["outbound", "ip", "udp", "(ip.DstAddr < 127.0.0.0 or ip.DstAddr > 127.255.255.255)"]
        for port in self.tunnel.local_ports():
            parts.append(f"udp.SrcPort != {port}")
        for ip in sorted(self.tunnel.relay_ips()):
            parts.append(f"ip.DstAddr != {ip}")
        for port in self.exclude_ports:
            parts.append(f"udp.DstPort != {int(port)}")
        return " and ".join(parts)

    def start(self):
        flt = self._filter()
        log.info("filtro WinDivert: %s", flt)
        self.handle = self.pydivert.WinDivert(flt)
        self.handle.open()
        self.inject_handle = self.pydivert.WinDivert("false")
        self.inject_handle.open()
        self.running = True
        self.thread = threading.Thread(target=self._run, name="nopingeyson-captura", daemon=True)
        self.thread.start()

    def set_games(self, games):
        """Troca a lista de jogos otimizados sem parar a captura."""
        self.games = {g.lower() for g in games}

    def stop(self):
        self.running = False
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
        if self.inject_handle:
            try:
                self.inject_handle.close()
            except Exception:
                pass
            self.inject_handle = None

    def _run(self):
        while self.running:
            try:
                packet = self.handle.recv()
            except Exception as e:
                if self.running:
                    log.error("captura parou: %s", e)
                break
            try:
                name, pid = self.procs.lookup(packet.src_port)
                if name not in self.games:
                    self.handle.send(packet)
                    self.stats["passed"] += 1
                    continue
                self.stats["captured"] += 1
                self.local[packet.src_port] = (packet.src_addr, packet.interface)
                self.loop.call_soon_threadsafe(self._to_tunnel, packet, name, pid)
            except Exception as e:
                log.debug("pacote ignorado: %s", e)
                try:
                    self.handle.send(packet)
                except Exception:
                    pass

    # roda no loop asyncio
    def _to_tunnel(self, packet, name: str, pid: int):
        self.on_game_seen(name, pid)
        res = self.tunnel.send(packet.src_port, (packet.dst_addr, packet.dst_port),
                               bytes(packet.payload), orig=packet,
                               profile=self.profile_for(name))
        if res == DIRECT:
            self._reinject(packet)

    def _reinject(self, packet):
        if packet is not None and self.inject_handle:
            try:
                self.inject_handle.send(packet)
            except Exception as e:
                log.debug("falha ao reinjetar: %s", e)

    def inject(self, lport: int, target: tuple[str, int], payload: bytes):
        info = self.local.get(lport)
        if not info or not self.inject_handle:
            return
        local_ip, iface = info
        raw = build_udp_ipv4(target, (local_ip, lport), payload)
        pkt = self.pydivert.Packet(raw, iface, self.pydivert.Direction.INBOUND)
        try:
            self.inject_handle.send(pkt)
            self.stats["injected"] += 1
        except Exception as e:
            log.debug("falha ao injetar: %s", e)
