"""Liga as peças: configuração, túnel, captura, Traffic Shaper e Boost, num
loop asyncio em segundo plano. A interface gráfica e a linha de comando usam
esta classe.

Fluxo igual ao de um otimizador de jogos: o usuário escolhe um jogo na
biblioteca, ajusta região / modo / conexões e clica em "Aplicar rotas". O
túnel sobe na primeira vez e a captura passa a desviar os executáveis de todos
os jogos com rotas aplicadas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections import deque

from . import protocol as P
from .booster import Booster, app_dir
from .games import all_games, by_exe
from .tunnel import Iface, RelayInfo, Tunnel

log = logging.getLogger("nopingeyson")

DEFAULT_GAME = {"region": "auto", "mode": "auto", "exit": None, "connections": 2}

DEFAULT_CONFIG = {
    "key": "",
    "relays": [],                 # {"name", "host", "port", "region"}
    "game_settings": {},          # id do jogo -> DEFAULT_GAME
    "active_games": [],           # jogos com "Aplicar rotas" ligado
    "custom_games": [],           # jogos pedidos pelo usuário
    "favorites": [],
    "two_hop": True,
    "prefer_direct": True,
    "min_gain_ms": 3,
    "exclude_ports": [53, 67, 68, 123, 137, 138, 1900, 3702, 5353, 5355],
    "auto_boost": [],
    "multi_internet": {"enabled": False, "interfaces": []},
    "shaper": {"enabled": False, "download_kbps": 0, "upload_kbps": 0, "exceptions": []},
}


def config_path() -> str:
    local = os.path.join(os.getcwd(), "config.json")
    return local if os.path.exists(local) else os.path.join(app_dir(), "config.json")


def load_config(path: str | None = None) -> dict:
    path = path or config_path()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    cfg["_path"] = path
    return cfg


def save_config(cfg: dict):
    path = cfg.get("_path") or config_path()
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: deque = deque(maxlen=200)

    def emit(self, record):
        self.lines.append({"t": record.created, "level": record.levelname,
                           "msg": record.getMessage()})


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="nopingeyson-loop",
                                       daemon=True)
        self.thread.start()
        self.tunnel: Tunnel | None = None
        self.capture = None
        self.shaper = None
        self.booster = Booster()
        self.running = False
        self.measure_only = False
        self.started_at = 0.0
        self.error = ""
        self.games_seen: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.logs = LogBuffer()
        root = logging.getLogger("nopingeyson")
        root.addHandler(self.logs)
        root.setLevel(logging.INFO)

    def _call(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # -- jogos ---------------------------------------------------------------
    def game_settings(self, game_id: str) -> dict:
        return dict(DEFAULT_GAME, **(self.cfg["game_settings"].get(game_id) or {}))

    def _active_exes(self) -> list[str]:
        games = {g["id"]: g for g in all_games(self.cfg)}
        return [exe for gid in self.cfg["active_games"] if gid in games
                for exe in games[gid]["exes"]]

    def profile_for(self, exe: str) -> dict | None:
        game = by_exe(self.cfg).get(exe.lower())
        if not game:
            return None
        return dict(self.game_settings(game["id"]), game=game["name"])

    def apply_routes(self, game_id: str, settings: dict | None = None) -> str:
        """Botão "Aplicar rotas" de um jogo."""
        with self.lock:
            if settings:
                self.cfg["game_settings"][game_id] = dict(self.game_settings(game_id), **settings)
            if game_id not in self.cfg["active_games"]:
                self.cfg["active_games"].append(game_id)
            save_config(self.cfg)
            if self.running and self.measure_only:
                self.stop()
            if not self.running:
                res = self.start()
                if res != "ok":
                    self.cfg["active_games"].remove(game_id)
                    save_config(self.cfg)
                    return res
            else:
                self._refresh_games()
                # servidores já conhecidos deste jogo usam as opções novas no próximo contato
                self._forget_targets(game_id)
            log.info("Rotas aplicadas: %s", game_id)
            return "ok"

    def stop_routes(self, game_id: str) -> str:
        with self.lock:
            if game_id in self.cfg["active_games"]:
                self.cfg["active_games"].remove(game_id)
                save_config(self.cfg)
            if not self.cfg["active_games"]:
                self.stop()
            else:
                self._refresh_games()
            return "ok"

    def _refresh_games(self):
        if self.capture:
            self.capture.set_games(self._active_exes())
        if self.shaper:
            self.shaper.protected = set(self._active_exes())

    def _forget_targets(self, game_id: str):
        game = next((g for g in all_games(self.cfg) if g["id"] == game_id), None)
        tunnel = self.tunnel
        if not game or not tunnel:
            return

        async def forget():
            for ip, ts in list(tunnel.targets.items()):
                if ts.profile.get("game") == game["name"] and ts.mode == "direct":
                    del tunnel.targets[ip]
        self._call(forget())

    # -- ciclo de vida -------------------------------------------------------
    def _ifaces(self) -> list[Iface] | None:
        mi = self.cfg.get("multi_internet") or {}
        if not mi.get("enabled"):
            return None
        from .netmon import list_interfaces
        chosen = set(mi.get("interfaces") or [])
        out = [Iface(i["name"], i["ip"], int(i["index"])) for i in list_interfaces()
               if i["name"] in chosen]
        if len(out) < 2:
            log.warning("Multi Internet precisa de 2 conexões ativas; usando só a padrão")
            return None
        log.info("Multi Internet: %s", ", ".join(i.name for i in out))
        return out

    def start(self, measure_only: bool = False) -> str:
        if self.running:
            return "já está rodando"
        self.error = ""
        cfg = self.cfg
        if not cfg.get("key") or not cfg.get("relays"):
            self.error = "Configure a chave e pelo menos um servidor (relay) em Configurações."
            return self.error
        try:
            key = P.load_key(cfg["key"])
            relays = [RelayInfo(r["name"], r["host"], int(r.get("port", 7000)),
                                region=r.get("region", "")) for r in cfg["relays"]]
            self.tunnel = Tunnel(relays, key, multipath=2,
                                 two_hop=bool(cfg["two_hop"]),
                                 min_gain_ms=float(cfg["min_gain_ms"]),
                                 prefer_direct=bool(cfg["prefer_direct"]),
                                 ifaces=self._ifaces())
            self._call(self.tunnel.start())
            if not measure_only:
                if sys.platform != "win32":
                    raise RuntimeError("a captura por aplicativo só existe no Windows")
                from .capture import Capture
                self.capture = Capture(self.tunnel, self.loop, self._active_exes(),
                                       cfg.get("exclude_ports"), on_game_seen=self._game_seen,
                                       profile_for=self.profile_for)
                self.capture.start()
                if (cfg.get("shaper") or {}).get("enabled"):
                    self.start_shaper()
                if cfg.get("auto_boost"):
                    self.booster.apply(cfg["auto_boost"])
        except Exception as e:
            self.error = str(e)
            if getattr(e, "winerror", None) == 5:
                self.error = "Acesso negado: abra o NoPingeyson como Administrador para aplicar rotas."
            log.error("falha ao iniciar: %s", self.error)
            self.stop()
            return self.error
        self.running = True
        self.measure_only = measure_only
        self.started_at = time.time()
        log.info("NoPingeyson iniciado (%s)", "só medição" if measure_only else "otimizando jogos")
        return "ok"

    def stop(self):
        self.stop_shaper()
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.tunnel:
            self.loop.call_soon_threadsafe(self.tunnel.close)
            self.tunnel = None
        if self.booster.timer_on or self.booster.boosted_pids:
            self.booster._restore_timer(None)
            self.booster._restore_priority(None)
        if self.running:
            log.info("NoPingeyson parado")
        self.running = False

    def restart(self) -> str:
        """Reinicia para aplicar mudanças de servidores ou Multi Internet."""
        if not self.running:
            return "ok"
        measure = self.measure_only
        self.stop()
        return self.start(measure_only=measure)

    # -- Traffic Shaper ----------------------------------------------------------
    def start_shaper(self) -> str:
        sh = self.cfg.get("shaper") or {}
        if self.shaper:
            self.shaper.update(sh.get("download_kbps", 0), sh.get("upload_kbps", 0),
                               sh.get("exceptions", []))
            return "ok"
        if not self.tunnel or self.measure_only:
            return "O Traffic Shaper liga junto com as rotas de um jogo."
        from .shaper import TrafficShaper
        try:
            self.shaper = TrafficShaper(sh.get("download_kbps", 0), sh.get("upload_kbps", 0),
                                        sh.get("exceptions", []), set(self._active_exes()),
                                        self.tunnel.relay_ips(), self.tunnel.local_ports())
            self.shaper.start()
        except Exception as e:
            self.shaper = None
            log.error("Traffic Shaper não iniciou: %s", e)
            return str(e)
        return "ok"

    def stop_shaper(self):
        if self.shaper:
            self.shaper.stop()
            self.shaper = None

    def _game_seen(self, name: str, pid: int):
        g = self.games_seen.setdefault(name, {"pid": pid, "since": time.time()})
        g["pid"] = pid
        if "priority" in (self.cfg.get("auto_boost") or []):
            self.booster.boost_process(pid)

    # -- estado ------------------------------------------------------------------
    def snapshot(self) -> dict:
        snap = {
            "running": self.running,
            "measure_only": self.measure_only,
            "active_games": list(self.cfg["active_games"]),
            "error": self.error,
            "uptime": time.time() - self.started_at if self.running else 0,
            "games_seen": self.games_seen,
            "capture": self.capture.stats if self.capture else None,
            "shaper": self.shaper.stats if self.shaper else None,
            "boost": self.booster.status(),
            "logs": list(self.logs.lines)[-40:],
        }
        tunnel = self.tunnel
        if tunnel:
            async def grab():   # lê o estado dentro do loop, sem disputar com ele
                return tunnel.snapshot()
            try:
                snap.update(self._call(grab(), timeout=2))
            except Exception:
                pass
        return snap
