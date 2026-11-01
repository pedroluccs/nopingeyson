"""Ferramentas que funcionam sem servidor nenhum: limpeza de RAM, otimizador
de DNS, rastreio de rota, ping para regiões de datacenter, limpeza de cache
DNS e iniciar com o Windows."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import random
import re
import socket
import statistics
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .booster import app_dir

log = logging.getLogger("nopingeyson.tools")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd, timeout=30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW, errors="ignore")
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


# -- Limpeza de RAM ------------------------------------------------------------------
def _enable_privilege(name: str) -> bool:
    from ctypes import wintypes
    advapi = ctypes.windll.advapi32
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(ctypes.windll.kernel32.GetCurrentProcess(), 0x0020 | 0x0008,
                                   ctypes.byref(token)):
        return False

    class LUID(ctypes.Structure):
        _fields_ = [("Low", wintypes.DWORD), ("High", wintypes.LONG)]

    class TP(ctypes.Structure):
        _fields_ = [("Count", wintypes.DWORD), ("Luid", LUID), ("Attr", wintypes.DWORD)]
    luid = LUID()
    if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
        return False
    tp = TP(1, luid, 0x2)   # SE_PRIVILEGE_ENABLED
    ok = advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
    ctypes.windll.kernel32.CloseHandle(token)
    return bool(ok) and ctypes.GetLastError() == 0


def clean_ram(protect: set[str] | None = None, standby: bool = True) -> dict:
    """Esvazia o working set dos programas em segundo plano (o Windows recarrega
    o que for usado) e, como Administrador, limpa a lista de espera da memória.
    Jogos em `protect` não são tocados."""
    import psutil
    protect = {p.lower() for p in (protect or set())}
    before = psutil.virtual_memory().available
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    k32.OpenProcess.restype = ctypes.c_void_p
    touched = 0
    for p in psutil.process_iter(["pid", "name"]):
        name = (p.info["name"] or "").lower()
        if p.info["pid"] in (0, 4, os.getpid()) or name in protect:
            continue
        h = k32.OpenProcess(0x0400 | 0x0100, False, p.info["pid"])  # QUERY_INFO | SET_QUOTA
        if not h:
            continue
        if psapi.EmptyWorkingSet(ctypes.c_void_p(h)):
            touched += 1
        k32.CloseHandle(ctypes.c_void_p(h))
    purged = False
    if standby and _enable_privilege("SeProfileSingleProcessPrivilege"):
        cmd = ctypes.c_int(4)   # MemoryPurgeStandbyList
        status = ctypes.windll.ntdll.NtSetSystemInformation(80, ctypes.byref(cmd), ctypes.sizeof(cmd))
        purged = status == 0
    time.sleep(0.5)
    after = psutil.virtual_memory().available
    return {"freed_mb": max(0, round((after - before) / 2**20)), "processes": touched,
            "standby_purged": purged, "available_mb": round(after / 2**20)}


# -- DNS Optimizer -------------------------------------------------------------------
DNS_SERVERS = [
    ("Cloudflare", "1.1.1.1", "1.0.0.1"),
    ("Google", "8.8.8.8", "8.8.4.4"),
    ("Quad9", "9.9.9.9", "149.112.112.112"),
    ("OpenDNS", "208.67.222.222", "208.67.220.220"),
    ("AdGuard", "94.140.14.14", "94.140.15.15"),
]
DNS_BACKUP = os.path.join(app_dir(), "dns_backup.json")


def _dns_query_time(server: str, tries: int = 5) -> float | None:
    times = []
    for _ in range(tries):
        name = random.choice(["google.com", "riotgames.com", "steampowered.com", "epicgames.com"])
        qid = random.randint(0, 0xFFFF)
        q = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
        q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack("!HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            t0 = time.perf_counter()
            s.sendto(q, (server, 53))
            data, _ = s.recvfrom(1024)
            if data[:2] == q[:2]:
                times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        finally:
            s.close()
    return round(statistics.median(times), 1) if times else None


def current_dns() -> list[dict]:
    """DNS configurado em cada placa de rede ativa."""
    ps = ("Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | "
          "ForEach-Object { [pscustomobject]@{ alias=$_.InterfaceAlias; servers=$_.ServerAddresses } } | ConvertTo-Json")
    _, out = _run(["powershell", "-NoProfile", "-Command", ps])
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        return []
    return [data] if isinstance(data, dict) else data


def dns_benchmark() -> list[dict]:
    servers = list(DNS_SERVERS)
    for cur in current_dns():
        for ip in cur.get("servers") or []:
            if not any(ip in s for s in servers) and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                servers.append((f"Atual ({cur['alias']})", ip, ""))
    with ThreadPoolExecutor(8) as ex:
        times = list(ex.map(lambda s: _dns_query_time(s[1]), servers))
    res = [{"name": n, "primary": a, "secondary": b, "ms": t} for (n, a, b), t in zip(servers, times)]
    return sorted(res, key=lambda r: (r["ms"] is None, r["ms"] or 0))


def dns_apply(primary: str, secondary: str) -> str:
    cur = current_dns()
    if not os.path.exists(DNS_BACKUP):
        with open(DNS_BACKUP, "w", encoding="utf-8") as f:
            json.dump(cur, f)
    errors = []
    for c in cur:
        alias = c["alias"]
        if alias.lower().startswith(("loopback", "vethernet")):
            continue
        code, out = _run(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={alias}",
                          "source=static", f"address={primary}", "register=primary", "validate=no"])
        if code == 0 and secondary:
            _run(["netsh", "interface", "ipv4", "add", "dnsservers", f"name={alias}",
                  f"address={secondary}", "index=2", "validate=no"])
        if code:
            errors.append(f"{alias}: {out.strip()}")
    _run(["ipconfig", "/flushdns"])
    return "ok" if not errors else "; ".join(errors)


def dns_restore() -> str:
    """Volta para o DNS automático (DHCP) nas placas que o NoPingeyson alterou."""
    try:
        with open(DNS_BACKUP, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = current_dns()
    for c in saved:
        _run(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={c['alias']}", "source=dhcp"])
    try:
        os.remove(DNS_BACKUP)
    except OSError:
        pass
    _run(["ipconfig", "/flushdns"])
    return "ok"


# -- Analisador de rede -----------------------------------------------------------------
REGIONS = [
    ("São Paulo", "sa-east-1"), ("Virgínia (EUA Leste)", "us-east-1"),
    ("Ohio (EUA)", "us-east-2"), ("Califórnia (EUA Oeste)", "us-west-1"),
    ("Oregon (EUA Oeste)", "us-west-2"), ("Irlanda", "eu-west-1"),
    ("Frankfurt", "eu-central-1"), ("Londres", "eu-west-2"),
    ("Tóquio", "ap-northeast-1"), ("Singapura", "ap-southeast-1"),
    ("Sydney", "ap-southeast-2"), ("Cidade do Cabo", "af-south-1"),
]


def _tcp_rtt(host: str, port: int = 443, tries: int = 4) -> float | None:
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return None
    times = []
    for _ in range(tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            t0 = time.perf_counter()
            s.connect((ip, port))
            times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        finally:
            s.close()
    return round(min(times), 1) if times else None


def region_ping() -> list[dict]:
    """Latência (handshake TCP) até datacenters usados por muitos jogos online."""
    def one(r):
        name, code = r
        return {"region": name, "code": code, "ms": _tcp_rtt(f"dynamodb.{code}.amazonaws.com")}
    with ThreadPoolExecutor(12) as ex:
        return sorted(ex.map(one, REGIONS), key=lambda r: (r["ms"] is None, r["ms"] or 0))


def traceroute(host: str, max_hops: int = 20) -> list[dict]:
    """Rastreio de rota (tracert) com o tempo de cada salto."""
    host = host.strip()
    if not re.match(r"^[A-Za-z0-9.\-:]+$", host):
        return []
    _, out = _run(["tracert", "-d", "-h", str(max_hops), "-w", "800", host], timeout=max_hops * 4 + 10)
    hops = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        rest = m.group(2)
        cols = re.findall(r"(<?\d+)\s*ms|(\*)", rest)[:3]
        if not cols:
            continue
        times = [None if star else int(ms.lstrip("<")) for ms, star in cols]
        ip = re.findall(r"\d+\.\d+\.\d+\.\d+", rest)
        ok = [x for x in times if x is not None]
        hops.append({"hop": int(m.group(1)), "ip": ip[-1] if ip else "*",
                     "ms": round(sum(ok) / len(ok), 1) if ok else None,
                     "loss": round(100 * (1 - len(ok) / len(times)))})
    return hops


def flush_dns() -> str:
    code, out = _run(["ipconfig", "/flushdns"])
    return "ok" if code == 0 else out.strip()


# -- iniciar com o Windows ------------------------------------------------------------------
TASK = "NoPingeyson"


def startup_enabled() -> bool:
    code, _ = _run(["schtasks", "/query", "/tn", TASK])
    return code == 0


def set_startup(on: bool) -> str:
    """Tarefa agendada no logon com privilégio máximo (sem pedir UAC a cada boot)."""
    if not on:
        _run(["schtasks", "/delete", "/tn", TASK, "/f"])
        return "ok"
    exe = sys.executable if getattr(sys, "frozen", False) else None
    if not exe:
        return "Disponível só na versão instalada (.exe)"
    code, out = _run(["schtasks", "/create", "/tn", TASK, "/tr", f'"{exe}" --minimizado',
                      "/sc", "onlogon", "/rl", "highest", "/f"])
    return "ok" if code == 0 else out.strip()
