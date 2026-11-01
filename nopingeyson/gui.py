"""Interface gráfica (janela pywebview com HTML local)."""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import sys

from . import netmon
from .booster import is_admin
from .engine import Engine, save_config
from .games import all_games

UI = os.path.join(os.path.dirname(__file__), "ui", "index.html")


class Api:
    def __init__(self, cfg: dict):
        self.engine = Engine(cfg)

    # -- estado e configuração -------------------------------------------------
    def get_state(self):
        snap = self.engine.snapshot()
        snap["admin"] = is_admin()
        return snap

    def get_config(self):
        return {k: v for k, v in self.engine.cfg.items() if not k.startswith("_")}

    def save_config(self, new: dict, restart=False):
        cfg = self.engine.cfg
        cfg.update({k: v for k, v in new.items() if not k.startswith("_")})
        save_config(cfg)
        if restart and self.engine.running:
            res = self.engine.restart()
            return "Salvo e reiniciado" if res == "ok" else res
        return "Configuração salva"

    def keygen(self):
        return secrets.token_hex(32)

    # -- jogos e rotas ---------------------------------------------------------------
    def list_games(self):
        return all_games(self.engine.cfg)

    def game_settings(self, game_id):
        return self.engine.game_settings(game_id)

    def apply_routes(self, game_id, settings):
        return self.engine.apply_routes(game_id, settings)

    def stop_routes(self, game_id):
        return self.engine.stop_routes(game_id)

    def measure(self):
        return self.engine.start(measure_only=True)

    def stop_all(self):
        self.engine.cfg["active_games"] = []
        save_config(self.engine.cfg)
        self.engine.stop()
        return "ok"

    def toggle_favorite(self, game_id):
        favs = self.engine.cfg.setdefault("favorites", [])
        favs.remove(game_id) if game_id in favs else favs.append(game_id)
        save_config(self.engine.cfg)
        return favs

    # -- pedido de jogo novo --------------------------------------------------------
    def network_activity(self):
        return netmon.network_activity()

    def add_custom_game(self, name, exes):
        name = str(name).strip()
        exes = [e.strip().lower() for e in exes if e.strip()]
        if not name or not exes:
            return "Informe o nome e pelo menos um executável"
        gid = "custom-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or secrets.token_hex(3))
        games = [g for g in self.engine.cfg.setdefault("custom_games", []) if g["id"] != gid]
        games.append({"id": gid, "name": name, "genre": "Personalizado", "exes": exes})
        self.engine.cfg["custom_games"] = games
        save_config(self.engine.cfg)
        return "ok"

    def remove_custom_game(self, game_id):
        self.stop_routes(game_id)
        self.engine.cfg["custom_games"] = [g for g in self.engine.cfg.get("custom_games", [])
                                           if g["id"] != game_id]
        save_config(self.engine.cfg)
        return "ok"

    # -- ferramentas -------------------------------------------------------------------
    def boost_apply(self, names):
        return self.engine.booster.apply(list(names))

    def boost_restore(self):
        return self.engine.booster.restore()

    def shaper_save(self, settings):
        self.engine.cfg["shaper"] = settings
        save_config(self.engine.cfg)
        if settings.get("enabled"):
            return self.engine.start_shaper()
        self.engine.stop_shaper()
        return "ok"

    def list_interfaces(self):
        return netmon.list_interfaces()

    def ping_test(self, hosts):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            return list(ex.map(netmon.ping_test, hosts))

    def speed_test(self):
        return netmon.speed_test()

    def device_monitor(self):
        return netmon.device_monitor()

    def relaunch_admin(self):
        args = sys.argv[1:] if getattr(sys, "frozen", False) else ['-m', 'nopingeyson'] + sys.argv[1:]
        params = " ".join(args)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
        os._exit(0)


def run_gui(cfg: dict):
    try:
        import webview
    except ImportError:
        print("pywebview não está instalado. Rode: pip install -r requirements.txt")
        sys.exit(1)
    api = Api(cfg)
    webview.create_window("NoPingeyson", UI, js_api=api, width=1180, height=760,
                          min_size=(980, 640), background_color="#0b0d14")
    try:
        webview.start()
    finally:
        api.engine.stop()
