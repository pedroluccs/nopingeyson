"""Descobre jogos e apps instalados no PC e extrai os ícones de verdade.

Fontes: Steam (bibliotecas + appmanifest), Epic Games (manifests), Riot
(metadados do Riot Client), registro do Windows (desinstaladores: Battle.net,
Ubisoft, EA, jogos avulsos) e o catálogo interno (procura os executáveis
conhecidos nas pastas de instalação).

Ícones: ícone de 256 px do próprio .exe (API do Windows) e, para jogos da
Steam, a capa que a própria Steam guarda no PC. Tudo fica em cache em
%APPDATA%\\NoPingeyson\\icons.
"""

from __future__ import annotations

import base64
import ctypes
import glob
import hashlib
import io
import json
import logging
import os
import re
import sys
import time

from .booster import app_dir
from .games import CATALOG

log = logging.getLogger("nopingeyson.detect")

IGNORE_EXE = re.compile(
    r"(unins|uninst|setup|install|redist|vc_?redist|dxsetup|crash|report|helper|"
    r"easyanticheat|eac_|beservice|battleye|_be\.exe|update|patch|launcherpatch|"
    r"unitycrashhandler|cefprocess|webhelper|overlay|dotnet|prereq|ue4prereq|"
    r"directx|physx|touchup|cleanup|quicksfv|7z|python|java|steamcmd|steampipe|sdk)", re.I)
STEAM_SKIP = {"228980"}          # Steamworks Common Redistributables
CACHE = os.path.join(app_dir(), "detect.json")
ICON_DIR = os.path.join(app_dir(), "icons")


# -- utilidades ------------------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "jogo"


def _exes(folder: str, depth: int = 5, limit: int = 400) -> list[str]:
    """Executáveis dentro da pasta (até `depth` níveis), já sem instaladores etc."""
    out = []
    base = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.lower().startswith(("_commonredist", "redist", "directx",
                                                               "support", "engine\\extras", "__"))]
        for f in files:
            if f.lower().endswith(".exe") and not IGNORE_EXE.search(f):
                out.append(os.path.join(root, f))
                if len(out) >= limit:
                    return out
    return out


def _main_exes(folder: str) -> list[str]:
    """Os .exe que provavelmente são o jogo: os maiores, preferindo *-Shipping.exe."""
    try:
        exes = _exes(folder)
    except OSError:
        return []
    if not exes:
        return []
    def size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    ranked = sorted(exes, key=lambda p: ("shipping" in p.lower(), size(p)), reverse=True)
    return ranked[:4]


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


# -- Steam -----------------------------------------------------------------------
def _steam_root() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return os.path.normpath(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
            if os.path.isdir(p):
                return p
    return None


def steam_games() -> list[dict]:
    root = _steam_root()
    if not root:
        return []
    vdf = _read(os.path.join(root, "steamapps", "libraryfolders.vdf"))
    libs = [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', vdf)] or [root]
    out = []
    for lib in libs:
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            text = _read(acf)
            get = lambda k: (re.search(rf'"{k}"\s+"([^"]*)"', text) or [None, ""])[1]
            appid, name, idir = get("appid"), get("name"), get("installdir")
            if not appid or appid in STEAM_SKIP or not name:
                continue
            folder = os.path.join(lib, "steamapps", "common", idir)
            if not os.path.isdir(folder):
                continue
            cache = os.path.join(root, "appcache", "librarycache", appid)
            cover = os.path.join(cache, "library_600x900.jpg")
            hero = os.path.join(cache, "library_hero.jpg")
            out.append({"name": name, "dir": folder, "source": "Steam", "appid": appid,
                        "cover": cover if os.path.exists(cover) else None,
                        "hero": hero if os.path.exists(hero) else None})
    return out


# -- Epic ------------------------------------------------------------------------
def epic_games() -> list[dict]:
    out = []
    for item in glob.glob(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item"):
        try:
            j = json.loads(_read(item))
        except ValueError:
            continue
        folder = j.get("InstallLocation")
        if not folder or not os.path.isdir(folder):
            continue
        exe = j.get("LaunchExecutable")
        exes = [os.path.join(folder, exe)] if exe else []
        out.append({"name": j.get("DisplayName") or os.path.basename(folder), "dir": folder,
                    "source": "Epic Games", "exes_hint": exes})
    return out


# -- Riot ------------------------------------------------------------------------
RIOT_NAMES = {"league_of_legends": "League of Legends", "valorant": "VALORANT",
              "bacon": "Legends of Runeterra", "lion": "2XKO"}


def riot_games() -> list[dict]:
    out = []
    for yaml in glob.glob(r"C:\ProgramData\Riot Games\Metadata\*.live\*.product_settings.yaml"):
        m = re.search(r'product_install_full_path:\s*"([^"]+)"', _read(yaml))
        if not m:
            continue
        folder = os.path.normpath(m.group(1))
        if not os.path.isdir(folder):
            continue
        key = os.path.basename(os.path.dirname(yaml)).split(".")[0]
        if key == "teamfighttactics":
            continue        # TFT roda dentro do cliente do LoL
        out.append({"name": RIOT_NAMES.get(key, key.replace("_", " ").title()), "dir": folder,
                    "source": "Riot Games"})
    return out


# -- registro (Battle.net, Ubisoft, EA, avulsos) ----------------------------------
GAME_PUBLISHERS = re.compile(r"(blizzard|ubisoft|electronic arts|\bea\b|riot|valve|epic games|"
                             r"bethesda|rockstar|activision|garena|tencent|krafton|nexon|"
                             r"square enix|bandai|capcom|sega|wargaming|hoyoverse|mihoyo)", re.I)


def registry_games() -> list[dict]:
    if sys.platform != "win32":
        return []
    import winreg
    out, seen = [], set()
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                vals = {}
                for n in ("DisplayName", "InstallLocation", "Publisher", "DisplayIcon"):
                    try:
                        vals[n] = str(winreg.QueryValueEx(sub, n)[0])
                    except OSError:
                        vals[n] = ""
            except OSError:
                continue
            name, folder = vals["DisplayName"], vals["InstallLocation"].strip('"')
            if not name or not folder or not os.path.isdir(folder) or name in seen:
                continue
            if not GAME_PUBLISHERS.search(vals["Publisher"]):
                continue
            seen.add(name)
            out.append({"name": name, "dir": folder, "source": "Windows",
                        "publisher": vals["Publisher"]})
    return out


# -- montagem ----------------------------------------------------------------------
LAUNCHER_NAMES = re.compile(r"(launcher|client|battle\.net|riot client|steam|epic games|"
                            r"ubisoft connect|ea app|origin)$", re.I)


def scan() -> list[dict]:
    """Lista final: catálogo instalado + jogos descobertos. Demora alguns segundos."""
    t0 = time.time()
    found: list[dict] = []
    for fn in (steam_games, epic_games, riot_games, registry_games):
        try:
            found += fn()
        except Exception as e:
            log.warning("detecção %s falhou: %s", fn.__name__, e)

    catalog_exes = {exe.lower(): g for g in CATALOG for exe in g["exes"]}
    games: dict[str, dict] = {}
    for f in found:
        exes = [p for p in f.get("exes_hint", []) if os.path.exists(p)] or []
        all_exes = _exes(f["dir"]) if os.path.isdir(f["dir"]) else []
        known = [p for p in all_exes if os.path.basename(p).lower() in catalog_exes]
        if known:
            g = catalog_exes[os.path.basename(known[0]).lower()]
            gid, name, category = g["id"], g["name"], g.get("category", "Jogos")
            exes = known
        else:
            if LAUNCHER_NAMES.search(f["name"]) and f["source"] == "Windows":
                continue
            exes = exes or _main_exes(f["dir"])
            if not exes:
                continue
            gid, name, category = _slug(f["name"]), f["name"], "Jogos"
        g = games.setdefault(gid, {"id": gid, "name": name, "category": category,
                                   "source": f["source"], "dir": f["dir"], "exes": [],
                                   "paths": [], "cover": f.get("cover"), "hero": f.get("hero")})
        for p in exes:
            b = os.path.basename(p).lower()
            if b not in g["exes"]:
                g["exes"].append(b)
                g["paths"].append(p)

    # apps do catálogo que não são jogos (navegador, voz, etc.) em lugares comuns
    for g in CATALOG:
        if g["id"] in games or not g.get("paths"):
            continue
        for pattern in g["paths"]:
            hits = glob.glob(os.path.expandvars(pattern))
            if hits:
                games[g["id"]] = {"id": g["id"], "name": g["name"],
                                  "category": g.get("category", "Jogos"), "source": "Windows",
                                  "dir": os.path.dirname(hits[0]), "exes": list(g["exes"]),
                                  "paths": [hits[0]], "cover": None, "hero": None}
                break
    result = sorted(games.values(), key=lambda g: g["name"].lower())
    log.info("detecção: %d jogos/apps em %.1fs", len(result), time.time() - t0)
    try:
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
    except OSError:
        pass
    return result


def cached() -> list[dict] | None:
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# -- ícones -------------------------------------------------------------------------
def _data_url(png: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(png).decode()


def icon_for(path: str, size: int = 96) -> str | None:
    """Ícone do .exe como data URL (PNG), com cache em disco."""
    if not path or not os.path.exists(path):
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}|{size}".encode()).hexdigest()[:16]
    cache = os.path.join(ICON_DIR, key + ".png")
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            return _data_url(f.read())
    try:
        img = extract_exe_icon(path)
    except Exception as e:
        log.debug("ícone de %s falhou: %s", path, e)
        img = None
    if img is None:
        return None
    from PIL import Image
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    with open(cache, "wb") as f:
        f.write(buf.getvalue())
    return _data_url(buf.getvalue())


def image_url(path: str | None, max_w: int = 300) -> str | None:
    """Imagem local (ex.: capa da Steam) reduzida, como data URL JPEG."""
    if not path or not os.path.exists(path):
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    key = hashlib.sha1(f"{path}|{os.path.getmtime(path)}|{max_w}".encode()).hexdigest()[:16]
    cache = os.path.join(ICON_DIR, key + ".jpg")
    if not os.path.exists(cache):
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_w, max_w * 2))
            im.save(cache, "JPEG", quality=85)
    with open(cache, "rb") as f:
        return _data_url(f.read(), "image/jpeg")


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def extract_exe_icon(path: str, size: int = 256):
    """Maior ícone do .exe via PrivateExtractIconsW, desenhado num bitmap 32 bits."""
    from ctypes import wintypes
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                                            wintypes.UINT, wintypes.UINT]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int,
                                  ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    hicon = wintypes.HICON()
    icon_id = wintypes.UINT()
    n = user32.PrivateExtractIconsW(path, 0, size, size, ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
    if n < 1 or not hicon:
        return None
    hdc = gdi32.CreateCompatibleDC(None)
    bmi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, hbmp)
    try:
        user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 3)  # DI_NORMAL
        raw = ctypes.string_at(bits, size * size * 4)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.DestroyIcon(hicon)
    img = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
    if img.getchannel("A").getextrema()[1] == 0:      # ícone antigo sem alfa
        img = img.convert("RGB").convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox and (bbox[2] - bbox[0]) < size * 0.6:     # ícone pequeno no canto: recorta
        img = img.crop(bbox)
    return img
