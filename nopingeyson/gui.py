"""Interface gráfica (janela pywebview com HTML local) + ícone na bandeja."""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import sys
import threading

from . import __version__, netmon, tools
from .booster import TWEAKS, is_admin
from .engine import Engine, relays_of, save_config

UI = os.path.join(os.path.dirname(__file__), "ui", "index.html")

BOOST_PRESETS = {
    "padrao": [],
    "alto": ["power", "throttling", "gamedvr", "gamemode", "bgapps", "nagle"],
}


class Api:
    def __init__(self, cfg: dict):
        self.engine = Engine(cfg)
        self.window = None

    # -- estado e configuração -------------------------------------------------
    def get_state(self):
        snap = self.engine.snapshot()
        snap["admin"] = is_admin()
        snap["version"] = __version__
        return snap

    def get_config(self):
        cfg = {k: v for k, v in self.engine.cfg.items() if not k.startswith("_")}
        servers, _ = relays_of(self.engine.cfg)
        cfg["servers"] = [{k: r.get(k) for k in ("name", "region")} for r in servers]
        cfg["builtin_servers"] = bool(self.engine.cfg.get("_builtin"))
        return cfg

    def save_config(self, new: dict, restart=False):
        cfg = self.engine.cfg
        for k, v in new.items():
            if k == "settings":
                cfg["settings"] = dict(cfg.get("settings") or {}, **v)
            elif not k.startswith("_") and k not in ("servers", "builtin_servers"):
                cfg[k] = v
        save_config(cfg)
        if restart and self.engine.running:
            res = self.engine.restart()
            return "Salvo" if res == "ok" else res
        return "Salvo"

    def set_enabled(self, on):
        self.engine.cfg["enabled"] = bool(on)
        save_config(self.engine.cfg)
        if not on:
            self.engine.stop()
            return "ok"
        if self.engine.cfg["active_games"] and not self.engine.running:
            return self.engine.start()
        return "ok"

    def keygen(self):
        return secrets.token_hex(32)

    # -- biblioteca e conexões ------------------------------------------------------
    def library(self, rescan=False):
        from .games import all_games
        self.engine.library(bool(rescan))
        installed = {g["id"] for g in self.engine.cfg.get("_detected") or []}
        out = []
        for g in all_games(self.engine.cfg):
            out.append({"id": g["id"], "name": g["name"], "category": g.get("category", "Jogos"),
                        "exes": g["exes"], "installed": g["id"] in installed or g.get("custom", False),
                        "source": g.get("source", ""), "custom": g.get("custom", False)})
        return out

    def icons(self, ids):
        """Ícones (data URL) dos jogos pedidos; capas da Steam quando houver."""
        from . import detect
        lib = {g["id"]: g for g in self.engine.cfg.get("_detected") or []}
        out = {}
        for gid in ids:
            g = lib.get(gid)
            if not g:
                continue
            icon = None
            for p in g.get("paths") or []:
                icon = detect.icon_for(p)
                if icon:
                    break
            out[gid] = {"icon": icon, "cover": detect.image_url(g.get("cover")),
                        "hero": detect.image_url(g.get("hero"), 900)}
        return out

    def game_settings(self, game_id):
        return self.engine.game_settings(game_id)

    def apply_routes(self, game_id, settings=None):
        if not self.engine.cfg.get("enabled", True):
            self.engine.cfg["enabled"] = True
        return self.engine.apply_routes(game_id, settings)

    def stop_routes(self, game_id):
        return self.engine.stop_routes(game_id)

    def add_connection(self, game_id):
        """Card "+" da tela Conexões: adiciona o jogo já ligado."""
        return self.apply_routes(game_id, {"connections": 2 if self.engine.cfg["settings"].get("dual_routes") else 1})

    def remove_connection(self, game_id):
        self.engine.stop_routes(game_id)
        conns = self.engine.cfg.setdefault("connections", [])
        if game_id in conns:
            conns.remove(game_id)
            save_config(self.engine.cfg)
        return "ok"

    def launch_game(self, game_id):
        g = next((g for g in self.engine.cfg.get("_detected") or [] if g["id"] == game_id), None)
        if not g or not g.get("paths"):
            return "Não sei como abrir esse jogo"
        if g.get("appid"):
            os.startfile(f"steam://rungameid/{g['appid']}")
        else:
            os.startfile(g["paths"][0])
        return "ok"

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
        games.append({"id": gid, "name": name, "category": "Jogos", "exes": exes})
        self.engine.cfg["custom_games"] = games
        save_config(self.engine.cfg)
        return "ok"

    def remove_custom_game(self, game_id):
        self.engine.stop_routes(game_id)
        self.engine.cfg["custom_games"] = [g for g in self.engine.cfg.get("custom_games", [])
                                           if g["id"] != game_id]
        save_config(self.engine.cfg)
        return "ok"

    # -- PC Boost --------------------------------------------------------------------
    def boost_status(self):
        return self.engine.booster.status()

    def boost_preset(self, name):
        self.engine.booster.restore()
        if name == "padrao":
            return {"preset": name, "result": "restaurado"}
        return {"preset": name, "result": self.engine.booster.apply(BOOST_PRESETS[name])}

    def boost_toggle(self, name, on):
        if on:
            return self.engine.booster.apply([name]).get(name)
        saved = self.engine.booster.backup.pop(name, None)
        if saved is not None:
            getattr(self.engine.booster, f"_restore_{name}")(saved)
            self.engine.booster._save()
        return "ok"

    def clean_ram(self):
        protect = {exe for g in self.engine.cfg.get("_detected") or [] for exe in g["exes"]
                   if g["id"] in self.engine.running_ids}
        return tools.clean_ram(protect)

    def memory(self):
        import psutil
        m = psutil.virtual_memory()
        return {"used": round(m.used / 2**30, 1), "total": round(m.total / 2**30, 1),
                "percent": m.percent}

    def dns_benchmark(self):
        return tools.dns_benchmark()

    def dns_apply(self, primary, secondary):
        return tools.dns_apply(primary, secondary)

    def dns_restore(self):
        return tools.dns_restore()

    def current_dns(self):
        return tools.current_dns()

    # -- Analisador de rede -------------------------------------------------------
    def region_ping(self):
        return tools.region_ping()

    def traceroute(self, host):
        return tools.traceroute(host)

    def flush_dns(self):
        return tools.flush_dns()

    def ping_test(self, hosts):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            return list(ex.map(netmon.ping_test, hosts))

    def speed_test(self):
        return netmon.speed_test()

    def device_monitor(self):
        return netmon.device_monitor()

    def list_interfaces(self):
        return netmon.list_interfaces()

    # -- Limitador / Multi Internet ---------------------------------------------------
    def shaper_save(self, settings):
        self.engine.cfg["shaper"] = settings
        save_config(self.engine.cfg)
        if settings.get("enabled"):
            if not self.engine.running:
                self.engine.start()
            return self.engine.start_shaper()
        self.engine.stop_shaper()
        return "ok"

    # -- configurações do app ---------------------------------------------------------
    def set_startup(self, on):
        res = tools.set_startup(bool(on))
        if res == "ok":
            self.engine.cfg["settings"]["startup"] = bool(on)
            save_config(self.engine.cfg)
        return res

    def relaunch_admin(self):
        args = sys.argv[1:] if getattr(sys, "frozen", False) else ['-m', 'nopingeyson'] + sys.argv[1:]
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(args), os.getcwd(), 1)
        os._exit(0)

    def minimize(self):
        if self.window:
            self.window.minimize()

    def close(self):
        if self.window:
            if (self.engine.cfg.get("settings") or {}).get("tray"):
                self.window.hide()
            else:
                self.quit()

    def quit(self):
        self.engine.stop()
        os._exit(0)


def _tray(api: Api):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return
    logo = os.path.join(os.path.dirname(__file__), "ui", "logo.png")
    image = Image.open(logo) if os.path.exists(logo) else Image.new("RGB", (64, 64), "#7c5cff")
    menu = pystray.Menu(
        pystray.MenuItem("Abrir NoPingeyson", lambda: api.window.show(), default=True),
        pystray.MenuItem("Sair", lambda icon: (icon.stop(), api.quit())),
    )
    pystray.Icon("NoPingeyson", image, "NoPingeyson", menu).run()


def run_gui(cfg: dict, minimized: bool = False):
    try:
        import webview
    except ImportError:
        print("pywebview não está instalado. Rode: pip install -r requirements.txt")
        sys.exit(1)
    api = Api(cfg)
    api.window = webview.create_window("NoPingeyson", UI, js_api=api, width=1220, height=780,
                                       min_size=(1040, 660), background_color="#0a0b10",
                                       frameless=True, easy_drag=False, hidden=minimized)

    def on_closing():
        if (api.engine.cfg.get("settings") or {}).get("tray"):
            api.window.hide()
            return False
        return True
    api.window.events.closing += on_closing
    threading.Thread(target=_tray, args=(api,), daemon=True).start()
    if cfg.get("active_games") and cfg.get("enabled", True):
        threading.Thread(target=api.engine.start, daemon=True).start()
    try:
        webview.start()
    finally:
        api.engine.stop()
