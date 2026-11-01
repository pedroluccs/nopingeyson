"""Traffic Shaper: limita a banda dos OUTROS programas para sobrar para o jogo.

Usa um segundo handle WinDivert que vê o tráfego TCP/UDP de entrada e saída.
Pacotes de jogos e de programas na lista de exceções passam livres. O resto
divide um "balde de fichas" por direção (download e upload): se o balde
esvaziou, o pacote é descartado e o TCP do outro programa desacelera sozinho.

Custa CPU (todo pacote passa pelo Python), por isso vem desligado.
"""

from __future__ import annotations

import logging
import threading
import time

from .capture import ProcessMap

log = logging.getLogger("nopingeyson.shaper")


class TokenBucket:
    def __init__(self, rate_kbps: float):
        self.set_rate(rate_kbps)
        self.tokens = self.capacity
        self.last = time.monotonic()

    def set_rate(self, rate_kbps: float):
        self.rate = max(0.0, rate_kbps) * 1000 / 8          # bytes por segundo
        self.capacity = max(self.rate * 0.25, 16_000)       # rajada de 250 ms

    def take(self, n: int) -> bool:
        if self.rate <= 0:
            return True     # sem limite
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class TrafficShaper:
    def __init__(self, download_kbps: float, upload_kbps: float, exceptions: list[str],
                 protected: set[str], exclude_ips: set[str], exclude_ports: list[int]):
        import pydivert
        self.pydivert = pydivert
        self.down = TokenBucket(download_kbps)
        self.up = TokenBucket(upload_kbps)
        self.exceptions = {e.lower() for e in exceptions}
        self.protected = protected          # jogos otimizados: nunca limitados
        self.exclude_ips = exclude_ips
        self.exclude_ports = exclude_ports  # portas UDP do próprio túnel
        self.procs = ProcessMap(ttl=20, kind="inet4", min_refresh=0.5)
        self.handle = None
        self.running = False
        self.stats = {"passed": 0, "limited": 0, "dropped": 0}

    def update(self, download_kbps: float, upload_kbps: float, exceptions: list[str]):
        self.down.set_rate(download_kbps)
        self.up.set_rate(upload_kbps)
        self.exceptions = {e.lower() for e in exceptions}

    def _filter(self) -> str:
        # IPv4 e IPv6 (muitos downloads vêm por IPv6). Campos ip.* são falsos em
        # pacotes IPv6 e vice-versa, por isso cada condição traz o "ipv6 or"/"ip or".
        parts = ["(tcp or udp)",
                 "((ip and (ip.DstAddr < 127.0.0.0 or ip.DstAddr > 127.255.255.255)"
                 " and (ip.SrcAddr < 127.0.0.0 or ip.SrcAddr > 127.255.255.255))"
                 " or (ipv6 and ipv6.DstAddr != ::1 and ipv6.SrcAddr != ::1))"]
        for ip in sorted(self.exclude_ips):
            parts.append(f"(ipv6 or (ip.DstAddr != {ip} and ip.SrcAddr != {ip}))")
        for port in self.exclude_ports:
            # campos udp.* são falsos em pacotes TCP, por isso o "tcp or"
            parts.append(f"(tcp or (udp.SrcPort != {port} and udp.DstPort != {port}))")
        return " and ".join(parts)

    def start(self):
        # prioridade baixa: roda depois da captura dos jogos
        self.handle = self.pydivert.WinDivert(self._filter(), priority=10)
        self.handle.open()
        self.running = True
        threading.Thread(target=self._run, name="nopingeyson-shaper", daemon=True).start()
        log.info("Traffic Shaper ligado (download %.0f kbps, upload %.0f kbps)",
                 self.down.rate * 8 / 1000, self.up.rate * 8 / 1000)

    def stop(self):
        self.running = False
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None

    def _run(self):
        while self.running:
            try:
                packet = self.handle.recv()
            except Exception as e:
                if self.running:
                    log.error("Traffic Shaper parou: %s", e)
                break
            try:
                proto = "tcp" if packet.tcp else "udp"
                lport = packet.src_port if packet.is_outbound else packet.dst_port
                name, _ = self.procs.lookup(lport, proto)
                if name in self.protected or name in self.exceptions:
                    self.handle.send(packet)
                    self.stats["passed"] += 1
                    continue
                bucket = self.up if packet.is_outbound else self.down
                if bucket.take(len(packet.raw)):
                    self.handle.send(packet)
                    self.stats["limited"] += 1
                else:
                    self.stats["dropped"] += 1     # descartado: o TCP do programa desacelera
            except Exception:
                try:
                    self.handle.send(packet)
                except Exception:
                    pass
