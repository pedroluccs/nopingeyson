"""Servidor relay do NoPingeyson. Roda numa VPS (Linux ou Windows), sem dependências.

    python3 -m nopingeyson.relay --port 7000 --key <chave-hex>

Um mesmo relay pode ser "entrada" (repassa para outro relay) ou "saída"
(fala com o servidor do jogo). Ele:
  * só aceita pacotes assinados com a chave compartilhada (não é proxy aberto);
  * repassa pacotes de ida para o próximo hop e lembra quem enviou, para
    devolver as respostas pelo mesmo caminho;
  * na saída, descarta cópias duplicadas (multipath) e encaminha ao jogo;
  * duplica as respostas do jogo por todos os caminhos ativos da sessão;
  * mede a latência dele até o servidor do jogo (ping ICMP) sob pedido.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import sys
import time

from . import protocol as P

log = logging.getLogger("nopingeyson.relay")

PATH_TTL = 30.0        # caminho sem tráfego por 30 s é esquecido
FLOW_TTL = 120.0       # fluxo sem tráfego por 2 min é fechado
ACTIVE_PATH = 3.0      # caminhos vistos nos últimos 3 s recebem cópias de volta
TPING_CACHE = 20.0


class Session:
    def __init__(self):
        self.prev_hop: dict[int, tuple[P.Addr, float]] = {}   # path_id -> (addr, visto)
        self.exit_paths: dict[int, float] = {}   # caminhos que trouxeram DATA até aqui (saída)
        self.up_window = P.SeqWindow()
        self.down_seq = 0
        self.flows: dict[int, "ExitFlow"] = {}
        self.last_seen = time.monotonic()


class ExitFlow(asyncio.DatagramProtocol):
    """Socket do relay de saída para um servidor de jogo específico."""

    def __init__(self, relay: "Relay", sid: bytes, flow_id: int, target: P.Addr):
        self.relay, self.sid, self.flow_id, self.target = relay, sid, flow_id, target
        self.transport: asyncio.DatagramTransport | None = None
        self.last_used = time.monotonic()
        self.pending: list[bytes] = []     # chegou antes do socket abrir

    def connection_made(self, transport):
        self.transport = transport
        P.tune_socket(transport)
        for payload in self.pending:
            transport.sendto(payload, self.target)
        self.pending.clear()

    def send(self, payload: bytes):
        self.last_used = time.monotonic()
        if self.transport:
            self.transport.sendto(payload, self.target)
        elif len(self.pending) < 64:
            self.pending.append(payload)

    def datagram_received(self, data, addr):
        if (addr[0], addr[1]) != self.target:
            return  # só aceita respostas do próprio servidor do jogo
        self.last_used = time.monotonic()
        self.relay.send_back_data(self.sid, self.flow_id, data)

    def error_received(self, exc):
        log.debug("erro no fluxo %s: %s", self.target, exc)


class Relay(asyncio.DatagramProtocol):
    def __init__(self, key: bytes, allow_private: bool = False, drop_rate: float = 0.0):
        self.key = key
        self.allow_private = allow_private
        self.drop_rate = drop_rate          # só para testes: simula perda
        self.sessions: dict[bytes, Session] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self.tping_cache: dict[str, tuple[float, float]] = {}
        self.tping_running: dict[str, asyncio.Task] = {}
        self.stats = {"in": 0, "out": 0, "bad": 0, "dup": 0}

    # -- asyncio -----------------------------------------------------------
    def connection_made(self, transport):
        self.transport = transport
        P.tune_socket(transport, 4 << 20)

    def datagram_received(self, data, addr):
        self.stats["in"] += 1
        if self.drop_rate and random.random() < self.drop_rate:
            return
        try:
            pkt = P.Packet.decode(data, self.key)
        except (P.BadPacket, Exception):
            self.stats["bad"] += 1
            return
        sess = self.sessions.get(pkt.session)
        if sess is None:
            if pkt.type not in P.FORWARD_TYPES:
                return
            sess = self.sessions[pkt.session] = Session()
        now = time.monotonic()
        sess.last_seen = now
        if pkt.type in P.FORWARD_TYPES:
            sess.prev_hop[pkt.path_id] = ((addr[0], addr[1]), now)
            if pkt.hops:
                nxt = pkt.hops.pop(0)
                self._send(pkt, nxt)
            else:
                self._handle_exit(sess, pkt)
        else:
            self._send_back(sess, pkt)

    # -- envio -------------------------------------------------------------
    def _send(self, pkt: P.Packet, addr: P.Addr):
        if self.transport:
            self.stats["out"] += 1
            self.transport.sendto(pkt.encode(self.key), addr)

    def _send_back(self, sess: Session, pkt: P.Packet):
        entry = sess.prev_hop.get(pkt.path_id)
        if entry:
            self._send(pkt, entry[0])

    def send_back_data(self, sid: bytes, flow_id: int, payload: bytes):
        sess = self.sessions.get(sid)
        if not sess:
            return
        sess.down_seq = (sess.down_seq + 1) & 0xFFFFFFFF
        now = time.monotonic()
        body = P.data_back_body(flow_id, payload)
        for path_id, seen in list(sess.exit_paths.items()):
            hop = sess.prev_hop.get(path_id)
            if hop and now - seen <= ACTIVE_PATH:
                self._send(P.Packet(P.T_DATA_BACK, sid, path_id, sess.down_seq, [], body), hop[0])

    # -- papel de saída ----------------------------------------------------
    def _handle_exit(self, sess: Session, pkt: P.Packet):
        if pkt.type == P.T_PROBE:
            self._send_back(sess, P.Packet(P.T_PROBE_ACK, pkt.session, pkt.path_id,
                                           pkt.seq, [], pkt.body))
        elif pkt.type == P.T_DATA:
            sess.exit_paths[pkt.path_id] = time.monotonic()
            if not sess.up_window.accept(pkt.seq):
                self.stats["dup"] += 1
                return
            try:
                flow_id, target, payload = P.parse_data(pkt.body)
            except P.BadPacket:
                return
            if not self.allow_private and not P.is_public_ipv4(target[0]):
                return
            flow = sess.flows.get(flow_id)
            if flow is None or flow.target != target:
                if flow and flow.transport:
                    flow.transport.close()
                flow = ExitFlow(self, pkt.session, flow_id, target)
                sess.flows[flow_id] = flow
                asyncio.ensure_future(self._open_flow(flow))
            flow.send(payload)
        elif pkt.type == P.T_TPING:
            try:
                ip = P.parse_tping(pkt.body)
            except Exception:
                return
            if not self.allow_private and not P.is_public_ipv4(ip):
                return
            asyncio.ensure_future(self._answer_tping(pkt, ip))

    async def _open_flow(self, flow: ExitFlow):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: flow, local_addr=("0.0.0.0", 0))

    async def _answer_tping(self, pkt: P.Packet, ip: str):
        cached = self.tping_cache.get(ip)
        if cached and time.monotonic() - cached[1] < TPING_CACHE:
            rtt = cached[0]
        else:
            task = self.tping_running.get(ip)
            if task is None:
                task = asyncio.ensure_future(icmp_ping(ip))
                self.tping_running[ip] = task
            try:
                rtt = await task
            finally:
                self.tping_running.pop(ip, None)
            self.tping_cache[ip] = (rtt, time.monotonic())
        sess = self.sessions.get(pkt.session)
        if sess:
            self._send_back(sess, P.Packet(P.T_TPING_ACK, pkt.session, pkt.path_id,
                                           pkt.seq, [], P.tping_ack_body(ip, rtt)))

    # -- limpeza -----------------------------------------------------------
    async def janitor(self):
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for sid, sess in list(self.sessions.items()):
                for pid, (_, seen) in list(sess.prev_hop.items()):
                    if now - seen > PATH_TTL:
                        del sess.prev_hop[pid]
                        sess.exit_paths.pop(pid, None)
                for fid, flow in list(sess.flows.items()):
                    if now - flow.last_used > FLOW_TTL:
                        if flow.transport:
                            flow.transport.close()
                        del sess.flows[fid]
                if not sess.prev_hop and not sess.flows:
                    del self.sessions[sid]


_RTT_RE = re.compile(r"(?:time|tempo)[=<]\s*([\d.,]+)\s*ms", re.I)


async def icmp_ping(ip: str, count: int = 3) -> float:
    """Ping ICMP usando o comando do sistema (não precisa de root). Retorna ms ou -1."""
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", str(count), "-w", "1000", ip]
    else:
        cmd = ["ping", "-n", "-c", str(count), "-i", "0.2", "-W", "1", ip]
    try:
        extra = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}  # sem janela
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **extra)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=count * 1.5 + 2)
    except (OSError, asyncio.TimeoutError):
        return -1.0
    times = [float(m.replace(",", ".")) for m in _RTT_RE.findall(out.decode(errors="ignore"))]
    return min(times) if times else -1.0


async def serve(host: str, port: int, key: bytes, allow_private=False, drop_rate=0.0):
    loop = asyncio.get_running_loop()
    relay = Relay(key, allow_private, drop_rate)
    await loop.create_datagram_endpoint(lambda: relay, local_addr=(host, port))
    log.info("relay ouvindo em %s:%d/udp", host, port)
    jan = asyncio.ensure_future(relay.janitor())
    return relay, jan


def main(argv=None):
    ap = argparse.ArgumentParser(description="Relay UDP do NoPingeyson")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7000)
    ap.add_argument("--key", default=os.environ.get("NOPINGEYSON_KEY"),
                    help="chave compartilhada (hex). Também pode vir de NOPINGEYSON_KEY")
    ap.add_argument("--allow-private", action="store_true",
                    help="permite destinos de rede privada (só para testes)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.key:
        ap.error("informe --key ou a variável NOPINGEYSON_KEY")
    key = P.load_key(args.key)

    async def run():
        relay, _ = await serve(args.host, args.port, key, args.allow_private)
        while True:
            await asyncio.sleep(60)
            log.info("sessões=%d in=%d out=%d dup=%d inválidos=%d",
                     len(relay.sessions), relay.stats["in"], relay.stats["out"],
                     relay.stats["dup"], relay.stats["bad"])

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
