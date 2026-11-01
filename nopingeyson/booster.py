"""Boost: ajustes de sistema para jogos, todos reversíveis.

Cada ajuste guarda o valor original em %APPDATA%\\NoPingeyson\\backup.json antes de
mudar, e `restore()` devolve tudo como estava. Nada é aplicado sozinho: só
quando o usuário pede (botão na interface ou `python -m nopingeyson boost apply`).

Ajustes disponíveis
  power      plano de energia "Alto desempenho"
  timer      resolução do timer do Windows em 1 ms enquanto o NoPingeyson roda
  priority   prioridade "Alta" para os processos de jogo detectados
  throttling desliga o NetworkThrottlingIndex e reserva menos CPU para fundo
  gamedvr    desliga a gravação em segundo plano do Xbox Game Bar
  nagle      TcpAckFrequency=1 e TCPNoDelay=1 nas interfaces de rede (TCP de jogos)
  gamemode   liga o Modo de Jogo do Windows
  visualfx   efeitos visuais do Windows em "melhor desempenho"
  bgapps     impede apps da loja de rodarem em segundo plano
  mouse      desliga a aceleração do mouse ("aumentar precisão do ponteiro")
  hags       agendamento de GPU acelerado por hardware (exige reiniciar)
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import sys

log = logging.getLogger("nopingeyson.booster")

HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
MM_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
IFACES_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"

TWEAKS = {
    "power": "Plano de energia Alto desempenho",
    "timer": "Timer do Windows em 1 ms (enquanto o NoPingeyson estiver aberto)",
    "priority": "Prioridade alta para o jogo",
    "throttling": "Remover limitação de rede multimídia",
    "gamedvr": "Desligar gravação em segundo plano (Game DVR)",
    "nagle": "Desligar algoritmo de Nagle (TCP)",
    "gamemode": "Ligar o Modo de Jogo do Windows",
    "visualfx": "Efeitos visuais do Windows em modo desempenho",
    "bgapps": "Bloquear apps em segundo plano",
    "mouse": "Desligar aceleração do mouse",
    "hags": "Agendamento de GPU por hardware",
}
NEEDS_REBOOT = {"nagle", "throttling", "hags", "visualfx"}


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "NoPingeyson")
    os.makedirs(path, exist_ok=True)
    return path


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Booster:
    def __init__(self):
        self.backup_path = os.path.join(app_dir(), "backup.json")
        self.backup: dict = self._load()
        self.timer_on = False
        self.boosted_pids: dict[int, int] = {}

    # -- backup ------------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self.backup_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(self.backup, f, indent=2)

    def status(self) -> dict:
        applied = set(self.backup)
        if self.timer_on:
            applied.add("timer")
        if self.boosted_pids:
            applied.add("priority")
        return {k: {"label": v, "applied": k in applied, "reboot": k in NEEDS_REBOOT}
                for k, v in TWEAKS.items()}

    # -- aplicar / desfazer ----------------------------------------------------
    def apply(self, names: list[str]) -> dict[str, str]:
        if sys.platform != "win32":
            return {n: "só funciona no Windows" for n in names}
        out = {}
        for name in names:
            try:
                getattr(self, f"_apply_{name}")()
                out[name] = "ok"
            except Exception as e:
                out[name] = f"erro: {e}"
                log.warning("boost %s falhou: %s", name, e)
        self._save()
        return out

    def restore(self) -> dict[str, str]:
        out = {}
        for name in list(self.backup):
            try:
                getattr(self, f"_restore_{name}")(self.backup[name])
                del self.backup[name]
                out[name] = "restaurado"
            except Exception as e:
                out[name] = f"erro: {e}"
        if self.timer_on:
            self._restore_timer(None)
            out["timer"] = "restaurado"
        if self.boosted_pids:
            self._restore_priority(None)
            out["priority"] = "restaurado"
        self._save()
        return out

    # power ------------------------------------------------------------------
    def _apply_power(self):
        if "power" not in self.backup:
            cur = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                                 text=True, check=True).stdout
            m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})", cur, re.I)
            self.backup["power"] = m.group(1) if m else None
        subprocess.run(["powercfg", "/setactive", HIGH_PERF], check=True, capture_output=True)

    def _restore_power(self, guid):
        if guid:
            subprocess.run(["powercfg", "/setactive", guid], check=True, capture_output=True)

    # timer -----------------------------------------------------------------
    def _apply_timer(self):
        if not self.timer_on:
            ctypes.windll.winmm.timeBeginPeriod(1)
            self.timer_on = True

    def _restore_timer(self, _):
        if self.timer_on:
            ctypes.windll.winmm.timeEndPeriod(1)
            self.timer_on = False

    # prioridade ----------------------------------------------------------------
    def _apply_priority(self):
        pass   # aplicada em boost_process() quando um jogo é detectado

    def boost_process(self, pid: int):
        import psutil
        if pid in self.boosted_pids:
            return
        try:
            p = psutil.Process(pid)
            self.boosted_pids[pid] = p.nice()
            p.nice(psutil.HIGH_PRIORITY_CLASS)
            log.info("prioridade alta para %s (pid %d)", p.name(), pid)
        except Exception as e:
            log.debug("não consegui mudar prioridade de %d: %s", pid, e)

    def _restore_priority(self, _):
        import psutil
        for pid, old in list(self.boosted_pids.items()):
            try:
                psutil.Process(pid).nice(old)
            except Exception:
                pass
        self.boosted_pids.clear()

    # registro ----------------------------------------------------------------
    @staticmethod
    def _reg_get(root, path, name):
        import winreg
        try:
            with winreg.OpenKey(root, path) as k:
                return list(winreg.QueryValueEx(k, name))
        except FileNotFoundError:
            return None

    @staticmethod
    def _reg_set(root, path, name, value, kind=None):
        import winreg
        with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, kind or winreg.REG_DWORD, value)

    @staticmethod
    def _reg_del(root, path, name):
        import winreg
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    def _set_many(self, tweak: str, items: list[tuple]):
        """items: (raiz, caminho, nome, valor). Faz backup dos originais uma vez."""
        import winreg
        roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
        if tweak not in self.backup:
            self.backup[tweak] = [[r, p, n, self._reg_get(roots[r], p, n)] for r, p, n, *_ in items]
        for r, p, n, v, *kind in items:
            self._reg_set(roots[r], p, n, v, kind[0] if kind else None)

    def _restore_many(self, saved):
        import winreg
        roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
        for r, p, n, old in saved:
            if old is None:
                self._reg_del(roots[r], p, n)
            else:
                self._reg_set(roots[r], p, n, old[0], old[1])

    def _apply_throttling(self):
        self._set_many("throttling", [("HKLM", MM_KEY, "NetworkThrottlingIndex", 0xFFFFFFFF),
                                      ("HKLM", MM_KEY, "SystemResponsiveness", 10)])

    _restore_throttling = _restore_many

    def _apply_gamedvr(self):
        self._set_many("gamedvr", [
            ("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0),
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0),
        ])

    _restore_gamedvr = _restore_many

    def _apply_nagle(self):
        import winreg
        items = []
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, IFACES_KEY) as k:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                path = IFACES_KEY + "\\" + guid
                # só interfaces com IP configurado
                if self._reg_get(winreg.HKEY_LOCAL_MACHINE, path, "DhcpIPAddress") or \
                        self._reg_get(winreg.HKEY_LOCAL_MACHINE, path, "IPAddress"):
                    items += [("HKLM", path, "TcpAckFrequency", 1),
                              ("HKLM", path, "TCPNoDelay", 1)]
        if not items:
            raise RuntimeError("nenhuma interface de rede encontrada")
        self._set_many("nagle", items)

    _restore_nagle = _restore_many

    def _apply_gamemode(self):
        self._set_many("gamemode", [
            ("HKCU", r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),
            ("HKCU", r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1),
        ])

    _restore_gamemode = _restore_many

    def _apply_visualfx(self):
        self._set_many("visualfx", [
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
             "VisualFXSetting", 2),
        ])

    _restore_visualfx = _restore_many

    def _apply_bgapps(self):
        self._set_many("bgapps", [
            ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
             "GlobalUserDisabled", 1),
        ])

    _restore_bgapps = _restore_many

    def _apply_mouse(self):
        import winreg
        key = r"Control Panel\Mouse"
        self._set_many("mouse", [
            ("HKCU", key, "MouseSpeed", "0", winreg.REG_SZ),
            ("HKCU", key, "MouseThreshold1", "0", winreg.REG_SZ),
            ("HKCU", key, "MouseThreshold2", "0", winreg.REG_SZ),
        ])
        # avisa o Windows para reler a configuração do mouse agora
        SPI_SETMOUSE, SPIF_SENDCHANGE = 0x0004, 0x02
        params = (ctypes.c_int * 3)(0, 0, 0)
        ctypes.windll.user32.SystemParametersInfoW(SPI_SETMOUSE, 0, params, SPIF_SENDCHANGE)

    def _restore_mouse(self, saved):
        self._restore_many(saved)
        vals = {n: int(old[0]) if old else 0 for _, _, n, old in saved}
        params = (ctypes.c_int * 3)(vals.get("MouseThreshold1", 6), vals.get("MouseThreshold2", 10),
                                    vals.get("MouseSpeed", 1))
        ctypes.windll.user32.SystemParametersInfoW(0x0004, 0, params, 0x02)

    def _apply_hags(self):
        self._set_many("hags", [
            ("HKLM", r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2),
        ])

    _restore_hags = _restore_many