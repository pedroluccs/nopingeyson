"""Ferramentas de rede e do PC:
  * lista de conexões de internet (para o Multi Internet);
  * monitor de atividade de rede por processo (para pedir um jogo novo);
  * diagnóstico: teste de ping/jitter/perda, teste de velocidade e monitor do PC.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
import time
import urllib.request

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd: list[str], timeout: float = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW, errors="ignore").stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# -- Multi Internet ----------------------------------------------------------
def list_interfaces() -> list[dict]:
    """Placas de rede com IPv4 ativo: nome, ip, índice e se têm gateway (internet)."""
    if sys.platform != "win32":
        return []
    ps = ("Get-NetIPConfiguration | Where-Object { $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' } | "
          "ForEach-Object { [pscustomobject]@{ name=$_.InterfaceAlias; index=$_.InterfaceIndex; "
          "ip=$_.IPv4Address[0].IPAddress; gateway=[bool]$_.IPv4DefaultGateway; "
          "desc=$_.InterfaceDescription } } | ConvertTo-Json")
    out = _run(["powershell", "-NoProfile", "-Command", ps])
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if not str(d.get("ip", "")).startswith("169.254.")]


# -- pedido de jogo novo -------------------------------------------------------
def network_activity() -> list[dict]:
    """Processos com conexões de rede abertas agora, mais ativos primeiro."""
    import psutil
    procs: dict[int, dict] = {}
    for c in psutil.net_connections(kind="inet4"):
        if not c.pid:
            continue
        p = procs.setdefault(c.pid, {"pid": c.pid, "udp": 0, "tcp": 0, "remotes": set()})
        p["udp" if c.type == 2 else "tcp"] += 1
        if c.raddr:
            p["remotes"].add(c.raddr.ip)
    out = []
    for pid, p in procs.items():
        try:
            proc = psutil.Process(pid)
            name, exe = proc.name(), proc.exe()
        except Exception:
            continue
        if name.lower() in ("svchost.exe", "system", "lsass.exe", "services.exe"):
            continue
        out.append({"pid": pid, "name": name.lower(), "exe": exe, "udp": p["udp"],
                    "tcp": p["tcp"], "remotes": len(p["remotes"])})
    out.sort(key=lambda p: (p["udp"] * 3 + p["remotes"]), reverse=True)
    return out[:60]


# -- diagnóstico ---------------------------------------------------------------
_RTT_RE = re.compile(r"(?:time|tempo)[=<]\s*([\d.,]+)\s*ms", re.I)


def ping_test(host: str, count: int = 20) -> dict:
    """Ping ICMP: média, mínimo, jitter e perda."""
    if sys.platform == "win32":
        out = _run(["ping", "-n", str(count), "-w", "1000", host], timeout=count * 1.5 + 5)
    else:
        out = _run(["ping", "-c", str(count), "-i", "0.2", "-W", "1", host], timeout=count + 5)
    rtts = [float(x.replace(",", ".")) for x in _RTT_RE.findall(out)]
    if not rtts:
        return {"host": host, "ok": False, "loss": 100.0}
    jitter = statistics.mean(abs(a - b) for a, b in zip(rtts, rtts[1:])) if len(rtts) > 1 else 0.0
    return {"host": host, "ok": True, "avg": round(statistics.mean(rtts), 1),
            "min": round(min(rtts), 1), "max": round(max(rtts), 1),
            "jitter": round(jitter, 1), "loss": round(100 * (1 - len(rtts) / count), 1)}


# arquivo público de teste de velocidade da OVH (feito para esse uso)
SPEED_URL = "https://proof.ovh.net/files/100Mb.dat"


def speed_test(seconds: float = 8.0) -> dict:
    """Teste de download com o arquivo público de teste da OVH. Upload não é
    medido: não há um destino público próprio para isso."""
    result = {"download_mbps": None, "upload_mbps": None}
    total, t0 = 0, time.perf_counter()
    try:
        while time.perf_counter() - t0 < seconds:
            with urllib.request.urlopen(SPEED_URL, timeout=10) as r:
                while chunk := r.read(256 * 1024):
                    total += len(chunk)
                    if time.perf_counter() - t0 > seconds:
                        break
        result["download_mbps"] = round(total * 8 / (time.perf_counter() - t0) / 1e6, 1)
    except Exception as e:
        result["error"] = f"download: {e}"
    return result


def wifi_signal() -> dict | None:
    if sys.platform != "win32":
        return None
    out = _run(["netsh", "wlan", "show", "interfaces"], timeout=5)
    sig = re.search(r"(?:Signal|Sinal)\s*:\s*(\d+)%", out)
    ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
    if not sig:
        return None
    return {"signal": int(sig.group(1)), "ssid": ssid.group(1).strip() if ssid else ""}


def device_monitor() -> dict:
    """CPU, memória, bateria, Wi-Fi e uso de rede do PC."""
    import psutil
    mem = psutil.virtual_memory()
    bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    net = psutil.net_io_counters()
    top = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            top.append((p.info["memory_info"].rss, p.info["name"]))
        except Exception:
            pass
    top.sort(reverse=True)
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(),
        "ram_used": round(mem.used / 2**30, 1), "ram_total": round(mem.total / 2**30, 1),
        "ram_percent": mem.percent,
        "battery": {"percent": bat.percent, "plugged": bat.power_plugged} if bat else None,
        "wifi": wifi_signal(),
        "net_sent": net.bytes_sent, "net_recv": net.bytes_recv,
        "top_memory": [{"name": n, "mb": round(r / 2**20)} for r, n in top[:6]],
    }
