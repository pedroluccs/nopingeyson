"""Teste de ponta a ponta em loopback: 3 relays, um "servidor de jogo" de eco,
perda simulada, multipath e queda de relay (failover).

    python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nopingeyson import protocol as P            # noqa: E402
from nopingeyson import relay as R                # noqa: E402
from nopingeyson import tunnel as T               # noqa: E402

KEY = bytes(range(32))


class Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(b"eco:" + data, addr)


class ProtocolTests(unittest.TestCase):
    def test_roundtrip_and_mac(self):
        pkt = P.Packet(P.T_DATA, b"12345678", 3, 99, [("1.2.3.4", 7000)],
                       P.data_body(5, ("8.8.8.8", 53), b"oi"))
        raw = pkt.encode(KEY)
        back = P.Packet.decode(raw, KEY)
        self.assertEqual(back.hops, [("1.2.3.4", 7000)])
        self.assertEqual(P.parse_data(back.body), (5, ("8.8.8.8", 53), b"oi"))
        with self.assertRaises(P.BadPacket):
            P.Packet.decode(raw, b"x" * 32)
        tampered = bytearray(raw)
        tampered[20] ^= 1
        with self.assertRaises(P.BadPacket):
            P.Packet.decode(bytes(tampered), KEY)

    def test_seq_window(self):
        w = P.SeqWindow(size=16)
        self.assertTrue(w.accept(1))
        self.assertFalse(w.accept(1))
        self.assertTrue(w.accept(3))
        self.assertTrue(w.accept(2))
        for s in range(4, 100):
            self.assertTrue(w.accept(s))
        self.assertFalse(w.accept(10))            # velho demais


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        loop.set_debug(False)   # o modo debug do unittest deixa o loop lento demais
        self.echo_t, _ = await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
        self.echo_addr = self.echo_t.get_extra_info("sockname")
        self.relays, self.infos = [], []
        for i, drop in enumerate([0.0, 0.25, 0.25]):
            relay, jan = await R.serve("127.0.0.1", 0, KEY, allow_private=True, drop_rate=drop)
            port = relay.transport.get_extra_info("sockname")[1]
            self.relays.append((relay, jan))
            self.infos.append(T.RelayInfo(f"R{i}", "127.0.0.1", port, "127.0.0.1"))
        self.received = []
        # saída fixa em R0 (sem perda) para medir o multipath; liberada no teste de failover
        self.tunnel = T.Tunnel(self.infos, KEY, multipath=3, prefer_direct=False, pinned_exit="R0",
                               on_downstream=lambda lp, tg, pl: self.received.append(pl))
        await self.tunnel.start()

    async def asyncTearDown(self):
        self.tunnel.close()
        for relay, jan in self.relays:
            jan.cancel()
            relay.transport.close()
        self.echo_t.close()

    async def test_route_multipath_failover(self):
        await asyncio.sleep(1.2)   # sondas medem os caminhos
        # primeiro pacote fica retido enquanto mede, depois sai pelo túnel
        res = self.tunnel.send(5555, self.echo_addr, b"p0")
        self.assertEqual(res, T.QUEUED)
        await asyncio.sleep(T.HOLD_MAX + 0.6)
        ts = self.tunnel.targets["127.0.0.1"]
        self.assertEqual(ts.mode, T.TUNNELED)
        self.assertEqual(ts.exit.name, "R0")
        self.assertIn(b"eco:p0", self.received)

        # multipath: com 25% de perda em 2 relays, quase nada se perde
        for i in range(1, 201):
            self.assertEqual(self.tunnel.send(5555, self.echo_addr, b"p%d" % i), T.TUNNELED)
            await asyncio.sleep(0.003)
        await asyncio.sleep(1.5)
        got = {p for p in self.received}
        lost = sum(1 for i in range(1, 201) if b"eco:p%d" % i not in got)
        self.assertLessEqual(lost, 2, f"perdidos {lost}")
        self.assertEqual(len(self.received), len(got), "duplicata entregou ao jogo")

        # failover: derruba a saída atual e confere que o tráfego continua
        self.tunnel.pinned_exit = None
        exit_name = ts.exit.name
        idx = int(exit_name[1])
        relay, jan = self.relays[idx]
        relay.transport.close()
        jan.cancel()
        await asyncio.sleep(T.EXIT_DEAD + 1.0)
        self.assertNotEqual(ts.exit.name, exit_name)
        before = len(self.received)
        for i in range(10):
            self.tunnel.send(5555, self.echo_addr, b"f%d" % i)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        # a nova saída perde 25% por conta própria (simulado), o multipath não cobre isso
        self.assertGreaterEqual(len(self.received) - before, 5)
        print("\neventos:", *[m for _, m in ts.events], sep="\n  ")
        print("recuperados pelo multipath:", ts.recovered,
              "duplicatas descartadas:", self.tunnel.counters["dup_dropped"])


@unittest.skipUnless(sys.platform == "win32", "WinDivert só existe no Windows")
class FilterTests(unittest.TestCase):
    """Os filtros precisam ser aceitos pelo WinDivert que vem com o pydivert."""

    def test_filters_are_valid(self):
        import pydivert
        from nopingeyson.capture import Capture
        from nopingeyson.shaper import TrafficShaper

        class FakeTunnel:
            on_downstream = on_direct = None
            def local_ports(self): return [50000, 50001]
            def relay_ips(self): return {"203.0.113.10"}

        cap = Capture(FakeTunnel(), None, ["cs2.exe"], [53, 123])
        sh = TrafficShaper(1000, 500, [], set(), {"203.0.113.10"}, [50000])
        for flt in (cap._filter(), sh._filter()):
            ok, pos, msg = pydivert.WinDivert.check_filter(flt)
            self.assertTrue(ok, f"{msg} na posição {pos}: {flt}")


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    """Região, modo manual, nº de conexões e Multi Internet na escolha de caminhos."""

    async def test_region_manual_and_connections(self):
        asyncio.get_running_loop().set_debug(False)
        loop = asyncio.get_running_loop()
        echo_t, _ = await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
        echo = echo_t.get_extra_info("sockname")
        relays, infos = [], []
        for i, region in enumerate(["SP", "SP", "MIA"]):
            relay, jan = await R.serve("127.0.0.1", 0, KEY, allow_private=True)
            relays.append((relay, jan))
            infos.append(T.RelayInfo(f"R{i}", "127.0.0.1",
                                     relay.transport.get_extra_info("sockname")[1], "127.0.0.1", region))
        # duas "conexões de internet" (ambas loopback aqui) para testar o Multi Internet
        ifaces = [T.Iface("A", "127.0.0.1"), T.Iface("B", "127.0.0.1")]
        tun = T.Tunnel(infos, KEY, prefer_direct=False, ifaces=ifaces)
        await tun.start()
        try:
            await asyncio.sleep(1.2)
            tun.send(1, echo, b"x", profile={"region": "MIA", "connections": 2})
            tun.send(2, ("127.0.0.2", echo[1]), b"y",
                     profile={"mode": "manual", "exit": "R1", "connections": 4})
            await asyncio.sleep(T.HOLD_MAX + 0.6)
            a, b = tun.targets["127.0.0.1"], tun.targets["127.0.0.2"]
            self.assertEqual(a.exit.name, "R2")                      # só a região MIA
            self.assertEqual(len(a.paths), 2)
            self.assertEqual({p.iface for p in a.paths}, {0, 1})     # uma por conexão
            self.assertEqual(b.exit.name, "R1")                      # saída manual
            self.assertEqual(len(b.paths), 4)
        finally:
            tun.close()
            for relay, jan in relays:
                jan.cancel()
                relay.transport.close()
            echo_t.close()


if __name__ == "__main__":
    unittest.main()
