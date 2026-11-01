"""Núcleo do cliente: medição de rotas, escolha da melhor, multipath e failover.

Não depende de WinDivert: recebe payloads do jogo por `send()` e entrega as
respostas pelo callback `on_downstream(lport, target, payload)`. Isso permite
testar tudo em loopback (ver tests/).

Modelo de rotas
  * Cada relay configurado pode ser SAÍDA (quem fala com o servidor do jogo).
  * Um caminho é [saída] (direto) ou [entrada, saída] (dois saltos).
  * Para cada servidor de jogo escolhemos UMA saída e até N caminhos que
    terminam nela. Cada pacote vai por todos os N caminhos e a saída descarta
    as cópias (multipath). As respostas voltam duplicadas e o cliente descarta.
  * Nota de rota = RTT cliente->saída (medido pelo caminho) + RTT saída->jogo
    (ping feito pelo relay) + penalidades por jitter e perda.
  * A SAÍDA só muda se todos os caminhos até ela caírem: trocar de saída muda
    o IP que o servidor do jogo vê, o que derruba a partida na maioria dos
    jogos. Já os caminhos até a saída trocam livremente (failover rápido).
  * Quando aparece um servidor novo, os primeiros pacotes ficam retidos por
    até HOLD_MAX segundos enquanto medimos; daí decidimos entre relay e
    conexão direta (se direta for melhor, os pacotes são liberados intactos).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import socket
import time
from collections import deque
from dataclasses import dataclass, field

from . import protocol as P

log = logging.getLogger("nopingeyson.tunnel")

PROBE_INTERVAL = 0.5
PROBE_TIMEOUT = 2.0
DOWN_AFTER = 1.5          # sem resposta de sonda por 1,5 s => caminho caído
EXIT_DEAD = 3.0           # saída inteira sem resposta por 3 s => troca de saída
TPING_INTERVAL = 15.0
SELECT_INTERVAL = 0.25
LOSS_WINDOW = 40
HOLD_MAX = 1.5            # retenção máxima do início de um fluxo novo
HOLD_PACKETS = 128

TUNNELED, QUEUED, DIRECT = "tunneled", "queued", "direct"


@dataclass
class RelayInfo:
    name: str
    host: str
    port: int
    ip: str = ""
    region: str = ""

    @property
    def addr(self) -> P.Addr:
        return (self.ip, self.port)


@dataclass
class Iface:
    """Uma conexão de internet local (Multi Internet). index=0 => rota padrão."""
    name: str = ""
    ip: str = "0.0.0.0"
    index: int = 0


class PathStats:
    def __init__(self):
        self.srtt: float | None = None
        self.jitter = 0.0
        self.results: deque[bool] = deque(maxlen=LOSS_WINDOW)
        self.last_ack = 0.0
        self.history: deque[float] = deque(maxlen=120)

    def on_rtt(self, rtt_ms: float):
        if self.srtt is None:
            self.srtt = rtt_ms
        else:
            self.jitter += (abs(rtt_ms - self.srtt) - self.jitter) / 8
            self.srtt += (rtt_ms - self.srtt) / 8
        self.results.append(True)
        self.last_ack = time.monotonic()
        self.history.append(round(rtt_ms, 1))

    def on_timeout(self):
        self.results.append(False)

    @property
    def loss(self) -> float:
        if not self.results:
            return 0.0
        return 1 - sum(self.results) / len(self.results)

    def alive(self, now: float) -> bool:
        return self.srtt is not None and now - self.last_ack < DOWN_AFTER

    def score(self) -> float | None:
        if self.srtt is None:
            return None
        return self.srtt + 2 * self.jitter + 300 * self.loss


@dataclass
class Path:
    id: int
    hops: list[RelayInfo]
    iface: int = 0
    iface_name: str = ""
    stats: PathStats = field(default_factory=PathStats)

    @property
    def exit(self) -> RelayInfo:
        return self.hops[-1]

    @property
    def label(self) -> str:
        route = " → ".join(h.name for h in self.hops)
        return f"[{self.iface_name}] {route}" if self.iface_name else route


@dataclass
class TargetState:
    ip: str
    created: float
    mode: str = QUEUED                   # QUEUED (medindo), TUNNELED ou DIRECT
    exit: RelayInfo | None = None
    paths: list[Path] = field(default_factory=list)
    score: float | None = None
    exit_rtt: dict[str, float] = field(default_factory=dict)  # relay -> rtt até o jogo
    direct_rtt: float | None = None
    direct_done: bool = False
    held: list = field(default_factory=list)
    last_used: float = 0.0
    up: int = 0
    down: int = 0
    recovered: int = 0        # respostas cuja 1ª cópia veio por caminho secundário
    events: deque = field(default_factory=lambda: deque(maxlen=50))
    # perfil do jogo: {"game", "region" ("auto" ou nome), "mode" ("auto"/"manual"),
    #                  "exit" (relay no modo manual), "connections" (1-4)}
    profile: dict = field(default_factory=dict)

    def log(self, msg: str):
        self.events.append((time.time(), msg))
        log.info("[%s] %s", self.ip, msg)


class Tunnel(asyncio.DatagramProtocol):
    def __init__(self, relays: list[RelayInfo], key: bytes, multipath: int = 2,
                 two_hop: bool = True, min_gain_ms: float = 3.0,
                 pinned_exit: str | None = None, prefer_direct: bool = True,
                 on_downstream=None, on_direct=None, ifaces: list[Iface] | None = None):
        self.relays = relays
        self.ifaces = ifaces or [Iface()]
        self.key = key
        self.multipath = max(1, multipath)
        self.min_gain_ms = min_gain_ms
        self.pinned_exit = pinned_exit
        self.prefer_direct = prefer_direct
        # on_downstream(lport, target, payload): entrega resposta ao jogo
        self.on_downstream = on_downstream or (lambda lport, target, payload: None)
        # on_direct(orig): devolve um pacote retido para seguir pela rota normal
        self.on_direct = on_direct or (lambda orig: None)
        self.session = os.urandom(8)
        self.transports: list[asyncio.DatagramTransport | None] = [None] * len(self.ifaces)
        self.paths = self._build_paths(two_hop)
        self.seq = 0
        self.down_windows: dict[str, P.SeqWindow] = {}   # uma por relay de saída
        self.flows: dict[tuple[int, P.Addr], int] = {}
        self.flow_info: dict[int, tuple[int, P.Addr]] = {}
        self._next_flow = 0
        self.targets: dict[str, TargetState] = {}
        self.pending_probes: dict[int, tuple[int, float]] = {}
        self._probe_ids = itertools.count(1)
        self.tasks: list[asyncio.Task] = []
        self.counters = {"up": 0, "down": 0, "dup_dropped": 0}

    # -- montagem ------------------------------------------------------------
    def _build_paths(self, two_hop: bool) -> list[Path]:
        paths: list[Path] = []
        multi = len(self.ifaces) > 1
        routes = [[r] for r in self.relays]
        if two_hop:
            routes += [[a, b] for a in self.relays for b in self.relays if a is not b]
        # caminhos diretos de todas as interfaces primeiro (usados no ping ao alvo)
        routes.sort(key=len)
        for hops in routes:
            for i, iface in enumerate(self.ifaces):
                if len(paths) < 255:
                    paths.append(Path(len(paths), hops, i, iface.name if multi else ""))
        return paths

    @property
    def direct_paths(self) -> list[Path]:
        return [p for p in self.paths if len(p.hops) == 1]

    async def start(self, local_port: int = 0):
        loop = asyncio.get_running_loop()
        for r in self.relays:
            if not r.ip:
                infos = await loop.getaddrinfo(r.host, r.port, family=socket.AF_INET,
                                               type=socket.SOCK_DGRAM)
                r.ip = infos[0][4][0]
        for i, iface in enumerate(self.ifaces):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if iface.index:
                _bind_to_interface(sock, iface.index)
            sock.bind((iface.ip, local_port if i == 0 else 0))
            await loop.create_datagram_endpoint(lambda i=i: _IfaceSocket(self, i), sock=sock)
        self.tasks = [asyncio.ensure_future(c) for c in
                      (self._probe_loop(), self._tping_loop(), self._select_loop())]

    @property
    def transport(self):
        return self.transports[0]

    @property
    def local_port(self) -> int:
        return self.transport.get_extra_info("sockname")[1] if self.transport else 0

    def local_ports(self) -> list[int]:
        return [t.get_extra_info("sockname")[1] for t in self.transports if t]

    def relay_ips(self) -> set[str]:
        return {r.ip for r in self.relays if r.ip}

    def close(self):
        for t in self.tasks:
            t.cancel()
        for t in self.transports:
            if t:
                t.close()

    def _send(self, pkt: P.Packet, path: Path):
        transport = self.transports[path.iface]
        if transport:
            pkt.hops = [h.addr for h in path.hops[1:]]
            pkt.path_id = path.id
            transport.sendto(pkt.encode(self.key), path.hops[0].addr)

    # -- tráfego do jogo -----------------------------------------------------
    def send(self, lport: int, target: P.Addr, payload: bytes, orig=None,
             profile: dict | None = None) -> str:
        """Encaminha um datagrama do jogo.

        Retorna TUNNELED (foi pelo túnel), QUEUED (retido enquanto mede; será
        enviado ou devolvido via on_direct depois) ou DIRECT (o chamador deve
        deixar o pacote seguir normalmente). `profile` são as opções do jogo
        (região, modo, conexões) e vale para servidores novos desse jogo.
        """
        ts = self.targets.get(target[0])
        if ts is None:
            ts = self._new_target(target[0])
            ts.profile = dict(profile or {})
        ts.last_used = time.monotonic()
        if ts.mode == DIRECT:
            return DIRECT
        if ts.mode == QUEUED:
            if len(ts.held) >= HOLD_PACKETS:
                return DIRECT
            ts.held.append((lport, target, payload, orig))
            return QUEUED
        if not ts.paths:
            return DIRECT
        self._tunnel(ts, lport, target, payload)
        return TUNNELED

    def _tunnel(self, ts: TargetState, lport: int, target: P.Addr, payload: bytes):
        key = (lport, target)
        flow_id = self.flows.get(key)
        if flow_id is None:
            self._next_flow = self._next_flow % 0xFFFF + 1
            flow_id = self._next_flow
            old = self.flow_info.pop(flow_id, None)
            if old:
                self.flows.pop(old, None)
            self.flows[key] = flow_id
            self.flow_info[flow_id] = key
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        body = P.data_body(flow_id, target, payload)
        for path in ts.paths:
            self._send(P.Packet(P.T_DATA, self.session, 0, self.seq, [], body), path)
        ts.up += 1
        self.counters["up"] += 1

    def datagram_received(self, data, addr):
        try:
            pkt = P.Packet.decode(data, self.key)
        except Exception:
            return
        if pkt.session != self.session or pkt.path_id >= len(self.paths):
            return
        path = self.paths[pkt.path_id]
        if pkt.type == P.T_DATA_BACK:
            win = self.down_windows.setdefault(path.exit.name, P.SeqWindow())
            if not win.accept(pkt.seq):
                self.counters["dup_dropped"] += 1
                return
            try:
                flow_id, payload = P.parse_data_back(pkt.body)
            except P.BadPacket:
                return
            info = self.flow_info.get(flow_id)
            if not info:
                return
            lport, target = info
            ts = self.targets.get(target[0])
            if ts:
                ts.down += 1
                if len(ts.paths) > 1 and path is not ts.paths[0]:
                    ts.recovered += 1
            self.counters["down"] += 1
            self.on_downstream(lport, target, payload)
        elif pkt.type == P.T_PROBE_ACK:
            probe_id, t0 = P.parse_probe(pkt.body)
            if self.pending_probes.pop(probe_id, None) is not None:
                path.stats.on_rtt((time.perf_counter() - t0) * 1000)
        elif pkt.type == P.T_TPING_ACK:
            ip, rtt = P.parse_tping_ack(pkt.body)
            ts = self.targets.get(ip)
            if ts:
                ts.exit_rtt[path.exit.name] = round(rtt, 1)

    # -- medições ------------------------------------------------------------
    def _new_target(self, ip: str) -> TargetState:
        ts = self.targets[ip] = TargetState(ip, time.monotonic())
        self._request_tping(ip)
        asyncio.ensure_future(self._direct_ping(ts))
        return ts

    def _request_tping(self, ip: str):
        for path in self.direct_paths:
            self._send(P.Packet(P.T_TPING, self.session, 0, 0, [], P.tping_body(ip)), path)

    async def _direct_ping(self, ts: TargetState):
        from .relay import icmp_ping
        rtt = await icmp_ping(ts.ip, count=2)
        ts.direct_rtt = rtt if rtt >= 0 else None
        ts.direct_done = True

    async def _probe_loop(self):
        while True:
            now = time.perf_counter()
            for pid, (path_id, t0) in list(self.pending_probes.items()):
                if now - t0 > PROBE_TIMEOUT:
                    del self.pending_probes[pid]
                    self.paths[path_id].stats.on_timeout()
            for path in self.paths:
                probe_id = next(self._probe_ids) & 0xFFFFFFFF
                t = time.perf_counter()
                self.pending_probes[probe_id] = (path.id, t)
                self._send(P.Packet(P.T_PROBE, self.session, 0, 0, [],
                                    P.probe_body(probe_id, t)), path)
            await asyncio.sleep(PROBE_INTERVAL)

    async def _tping_loop(self):
        while True:
            await asyncio.sleep(TPING_INTERVAL)
            for ip in self._active_targets():
                self._request_tping(ip)

    def _active_targets(self) -> list[str]:
        now = time.monotonic()
        return [ip for ip, ts in self.targets.items() if now - ts.last_used < 60]

    # -- escolha de rota -------------------------------------------------------
    def _exit_options(self, ts: TargetState) -> dict[str, tuple[float, RelayInfo, list[Path]]]:
        """Para cada saída possível: (nota total, relay, melhores caminhos até ela)."""
        now = time.monotonic()
        prof = ts.profile
        manual_exit = prof.get("exit") if prof.get("mode") == "manual" else None
        region = prof.get("region") or "auto"
        n = int(prof.get("connections") or self.multipath)
        out = {}
        for relay in self.relays:
            if self.pinned_exit and relay.name != self.pinned_exit:
                continue
            if manual_exit and relay.name != manual_exit:
                continue
            if region != "auto" and relay.region != region:
                continue
            cands = sorted(((p.stats.score(), p) for p in self.paths
                            if p.exit is relay and p.stats.alive(now)),
                           key=lambda c: c[0])
            if not cands:
                continue
            er = ts.exit_rtt.get(relay.name)
            if er is not None and er < 0:
                er = None   # servidor não responde a ping: só a parte até o relay conta
            total = cands[0][0] + (er or 0)
            out[relay.name] = (total, relay, self._diverse(cands, n))
        return out

    def _diverse(self, cands: list[tuple[float, Path]], n: int) -> list[Path]:
        """Os N melhores caminhos, espalhados: primeiro uma conexão de internet
        diferente (Multi Internet), depois entradas diferentes, depois o resto."""
        chosen: list[Path] = []
        keys = (lambda p: p.iface, lambda p: (p.iface, p.hops[0].name), lambda p: p.id)
        for key in keys:
            seen = {key(p) for p in chosen}
            for _, p in cands:
                if len(chosen) >= n:
                    return chosen
                if p not in chosen and key(p) not in seen:
                    chosen.append(p)
                    seen.add(key(p))
        return chosen

    def _measured(self, ts: TargetState) -> bool:
        alive = {p.exit.name for p in self.paths if p.stats.alive(time.monotonic())}
        return ts.direct_done and all(n in ts.exit_rtt for n in alive)

    def _decide(self, ts: TargetState):
        """Primeira decisão para um alvo novo: relay ou conexão direta."""
        opts = self._exit_options(ts)
        best = min(opts.values(), key=lambda o: o[0]) if opts else None
        held, ts.held = ts.held, []
        use_relay = best is not None
        manual = ts.profile.get("mode") == "manual"
        if use_relay and self.prefer_direct and not manual and ts.direct_rtt is not None \
                and best[1].name in ts.exit_rtt and ts.exit_rtt[best[1].name] >= 0:
            use_relay = best[0] + self.min_gain_ms < ts.direct_rtt
        if use_relay:
            total, relay, paths = best
            ts.mode, ts.exit, ts.paths, ts.score = TUNNELED, relay, paths, total
            direct = f"{ts.direct_rtt:.0f} ms" if ts.direct_rtt else "?"
            ts.log(f"Rota escolhida: saída {relay.name} via "
                   f"{', '.join(p.label for p in paths)} (~{total:.0f} ms; direto {direct})")
            for lport, target, payload, _ in held:
                self._tunnel(ts, lport, target, payload)
        else:
            ts.mode = DIRECT
            ts.log("Conexão direta é melhor ou nenhum relay disponível: tráfego segue normal")
            for *_, orig in held:
                self.on_direct(orig)

    def _maintain(self, ts: TargetState):
        """Failover e ajuste de caminhos de um alvo já roteado."""
        now = time.monotonic()
        opts = self._exit_options(ts)
        cur = opts.get(ts.exit.name) if ts.exit else None
        if cur is None:
            # Nenhum caminho até a saída respondeu há pouco. Até EXIT_DEAD seguimos
            # mandando pelos caminhos atuais (pode ser só uma rajada de perda);
            # depois disso trocamos de saída (último recurso, muda o IP visto pelo jogo).
            last = max((p.stats.last_ack for p in self.paths if p.exit is ts.exit), default=0)
            if opts and now - last > EXIT_DEAD:
                total, relay, paths = min(opts.values(), key=lambda o: o[0])
                ts.log(f"Saída {ts.exit.name if ts.exit else '?'} caiu; failover para "
                       f"{relay.name} (o jogo pode reconectar)")
                ts.exit, ts.paths, ts.score = relay, paths, total
            return
        total, _, paths = cur
        old = [p.id for p in ts.paths]
        if [p.id for p in paths] != old:
            if ts.paths and not ts.paths[0].stats.alive(now):
                ts.log(f"Caminho {ts.paths[0].label} caiu; usando {paths[0].label}")
            ts.paths = paths
        ts.score = total

    async def _select_loop(self):
        while True:
            now = time.monotonic()
            for ip, ts in list(self.targets.items()):
                if ts.mode == QUEUED:
                    if self._measured(ts) or now - ts.created > HOLD_MAX:
                        self._decide(ts)
                elif ts.mode == TUNNELED and now - ts.last_used < 60:
                    self._maintain(ts)
                elif now - ts.last_used > 300:
                    del self.targets[ip]       # alvo esquecido; nova decisão se voltar
            await asyncio.sleep(SELECT_INTERVAL)

    # -- estado para a interface ---------------------------------------------
    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            "session": self.session.hex(),
            "counters": dict(self.counters),
            "paths": [{
                "id": p.id, "label": p.label,
                "rtt": round(p.stats.srtt, 1) if p.stats.srtt is not None else None,
                "jitter": round(p.stats.jitter, 1),
                "loss": round(p.stats.loss * 100, 1),
                "alive": p.stats.alive(now),
                "history": list(p.stats.history)[-60:],
            } for p in self.paths],
            "targets": [{
                "ip": ts.ip,
                "game": ts.profile.get("game"),
                "mode": ts.mode,
                "active": now - ts.last_used < 60,
                "exit": ts.exit.name if ts.exit else None,
                "paths": [p.label for p in ts.paths],
                "score": round(ts.score, 1) if ts.score else None,
                "direct": ts.direct_rtt,
                "exit_rtt": ts.exit_rtt,
                "up": ts.up, "down": ts.down, "recovered": ts.recovered,
                "events": [{"t": t, "msg": m} for t, m in list(ts.events)[-10:]],
            } for ts in self.targets.values()],
        }


class _IfaceSocket(asyncio.DatagramProtocol):
    """Um socket por conexão de internet; tudo cai no mesmo Tunnel."""

    def __init__(self, tunnel: Tunnel, index: int):
        self.tunnel, self.index = tunnel, index

    def connection_made(self, transport):
        P.tune_socket(transport)
        self.tunnel.transports[self.index] = transport

    def datagram_received(self, data, addr):
        self.tunnel.datagram_received(data, addr)

    def error_received(self, exc):
        log.debug("erro de socket (interface %d): %s", self.index, exc)


IP_UNICAST_IF = 31


def _bind_to_interface(sock: socket.socket, index: int):
    """Força a saída por uma placa de rede específica (Windows: IP_UNICAST_IF)."""
    import struct
    try:
        sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", index))
    except OSError as e:
        log.warning("não consegui prender o socket à interface %d: %s", index, e)