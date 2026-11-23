"""Low-overhead status & lifecycle controller for SOL-Pad (iPad 2 USB monitor & dock)."""

from __future__ import annotations

import ctypes
import plistlib
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

PAD_DIR = Path(r"D:\Workspace\sol-pad-dock")
STREAM_EXE = PAD_DIR / "sol-pad-stream.exe"
DOCK_EXE = PAD_DIR / "sol-pad-dock.exe"

_SYNCHRONIZE = 0x00100000
_DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def _mutex_exists(name: str) -> bool:
    """Check a Windows named mutex in microseconds with zero subprocesses."""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenMutexW(_SYNCHRONIZE, False, name)
        if h:
            k32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _usbmux_ipad_attached() -> bool:
    """Query local Apple usbmuxd (127.0.0.1:27015) for a USB-attached iOS device."""
    try:
        with socket.create_connection(("127.0.0.1", 27015), timeout=0.35) as s:
            s.settimeout(0.35)
            body = plistlib.dumps(
                {
                    "MessageType": "ListDevices",
                    "ClientVersionString": "sol-control-hud",
                    "ProgName": "sol-control-hud",
                },
                fmt=plistlib.FMT_XML,
            )
            s.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
            hdr = s.recv(16)
            if len(hdr) < 16:
                return False
            total = struct.unpack("<IIII", hdr)[0]
            if total <= 16:
                return False
            data = b""
            while len(data) < total - 16:
                chunk = s.recv(total - 16 - len(data))
                if not chunk:
                    break
                data += chunk
            resp = plistlib.loads(data)
            return any(
                (d.get("Properties") or {}).get("ConnectionType") == "USB"
                for d in resp.get("DeviceList", [])
            )
    except Exception:
        return False


def get_sol_pad_status() -> dict:
    usb = _usbmux_ipad_attached()
    stream_up = _mutex_exists(r"Local\SOLPadStreamMutex")
    dock_up = _mutex_exists(r"Local\SOLPadDockMutex")
    if stream_up and dock_up:
        state = "online"
    elif stream_up or dock_up:
        state = "partial"
    elif usb:
        state = "standby"
    else:
        state = "offline"
    return {
        "usb_connected": usb,
        "stream_running": stream_up,
        "dock_running": dock_up,
        "state": state,
        "installed": STREAM_EXE.exists() and DOCK_EXE.exists(),
        "popos": get_popos_status(),
    }


def start_pad() -> dict:
    if not STREAM_EXE.exists() or not DOCK_EXE.exists():
        return {"ok": False, "why": "sol-pad executables not found in D:\\Workspace\\sol-pad-dock"}
    started = []
    for exe, mutex in ((STREAM_EXE, r"Local\SOLPadStreamMutex"), (DOCK_EXE, r"Local\SOLPadDockMutex")):
        if not _mutex_exists(mutex):
            subprocess.Popen([str(exe)], cwd=str(PAD_DIR), creationflags=_DETACHED)
            started.append(exe.name)
    return {"ok": True, "why": f"started {', '.join(started)}" if started else "already running"}


def stop_pad() -> dict:
    errors = []
    for exe_name in ("sol-pad-stream.exe", "sol-pad-dock.exe"):
        r = subprocess.run(
            ["taskkill", "/IM", exe_name, "/T", "/F"],
            capture_output=True,
            text=True,
            creationflags=0x08000000,
        )
        msg = (r.stderr or r.stdout or "").strip()
        if r.returncode != 0 and "not found" not in msg.lower():
            errors.append(f"{exe_name}: {msg}")
    time.sleep(0.15)
    still = [
        name
        for name, m in (("sol-pad-stream", r"Local\SOLPadStreamMutex"), ("sol-pad-dock", r"Local\SOLPadDockMutex"))
        if _mutex_exists(m)
    ]
    if still:
        why = f"still running ({', '.join(still)})"
        if errors:
            why += f" — {'; '.join(errors)}"
        return {"ok": False, "why": why}
    return {"ok": True, "why": "stopped sol-pad-stream & sol-pad-dock"}


def restart_pad() -> dict:
    stop_pad()
    time.sleep(0.35)
    return start_pad()


POPOS_HOST = "popos.lan"
POPOS_IP = "popos.lan"
POPOS_MAC = "00:00:00:00:00:00"
MOONLIGHT_EXE = Path(r"C:\Users\cohen\AppData\Local\Programs\Moonlight\Moonlight.exe")
_popos_cache: dict = {"at": 0.0, "data": {}}


def _fetch_popos_telemetry() -> dict:
    """Query lightweight CPU load, free RAM, GPU temp, and Docker count over SSH."""
    cmd = (
        "awk '{print $1}' /proc/loadavg; "
        "awk '/MemAvailable/{printf \"%.1f\\n\", $2/1048576}' /proc/meminfo; "
        "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null || echo ''; "
        "docker ps -q 2>/dev/null | wc -l"
    )
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=1", "popos", cmd],
            capture_output=True,
            text=True,
            timeout=2.5,
            creationflags=0x08000000,
        )
        if r.returncode != 0:
            return {}
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        out: dict = {}
        if len(lines) >= 1 and lines[0]:
            out["load1"] = lines[0]
        if len(lines) >= 2 and lines[1]:
            out["mem_avail_gb"] = float(lines[1])
        if len(lines) >= 3 and lines[2].isdigit():
            out["gpu_temp_c"] = int(lines[2])
        if len(lines) >= 4 and lines[3].isdigit():
            out["docker_running"] = int(lines[3])
        return out
    except Exception:
        return {}


def get_popos_status() -> dict:
    """Return awake/sleeping status and cached telemetry for the Pop!_OS node."""
    if not _tcp_up(POPOS_IP, 22, timeout=0.25):
        _popos_cache["at"] = 0.0
        _popos_cache["data"] = {}
        return {"online": False, "state": "sleeping"}
    now = time.monotonic()
    if now - _popos_cache["at"] > 25.0 or not _popos_cache["data"]:
        data = _fetch_popos_telemetry()
        if data:
            _popos_cache["data"] = data
            _popos_cache["at"] = now
    return {"online": True, "state": "awake", **_popos_cache["data"]}


def wake_popos(mac: str = POPOS_MAC) -> dict:
    """Send a Wake-on-LAN magic packet to Pop!_OS without launching RDP."""
    try:
        send_wol(mac)
        return {"ok": True, "why": f"sent Wake-on-LAN packet to {POPOS_HOST}"}
    except OSError as exc:
        return {"ok": False, "why": f"WoL failed: {exc}"}


def sleep_popos() -> dict:
    """Suspend the Pop!_OS PC over SSH."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", "popos", "sudo -n systemctl suspend"],
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=0x08000000,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "suspend failed").strip()
            return {"ok": False, "why": f"Pop!_OS sleep failed: {msg}"}
        _popos_cache["at"] = 0.0
        _popos_cache["data"] = {}
        return {"ok": True, "why": "Pop!_OS is going to sleep"}
    except Exception as exc:
        return {"ok": False, "why": f"Pop!_OS sleep failed: {exc}"}


def _tcp_up(host: str, port: int = 3389, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def send_wol(mac: str = POPOS_MAC, broadcast: str = "192.168.0.255") -> None:
    """Broadcast a Wake-on-LAN magic packet for the target MAC address."""
    hw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + hw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in ("255.255.255.255", broadcast):
            try:
                s.sendto(pkt, (target, 9))
            except OSError:
                pass


def _launch_desktop_client(host: str) -> str:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    if MOONLIGHT_EXE.exists():
        subprocess.Popen([str(MOONLIGHT_EXE), "stream", host, "Desktop", "--windowed"], creationflags=flags)
        return "Moonlight"
    subprocess.Popen(["mstsc.exe", f"/v:{host}"], creationflags=flags)
    return "Remote Desktop"


def _wake_and_open(host: str, mac: str, port: int = 47989, wait_sec: float = 20.0) -> None:
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        if _tcp_up(host, port, timeout=0.5):
            _launch_desktop_client(host)
            return
        time.sleep(0.5)
    try:
        _launch_desktop_client(host)
    except OSError:
        pass


def open_popos_rdp(host: str = POPOS_HOST, mac: str = POPOS_MAC) -> dict:
    """Wake Pop!_OS via WoL if sleeping, then launch Moonlight Desktop (or RDP fallback)."""
    port = 47989 if MOONLIGHT_EXE.exists() else 3389
    try:
        if _tcp_up(host, port, timeout=0.35):
            client = _launch_desktop_client(host)
            return {"ok": True, "why": f"opened {client} -> {host}"}
        send_wol(mac)
        threading.Thread(target=_wake_and_open, args=(host, mac, port), daemon=True).start()
        return {"ok": True, "why": f"waking {host} (WoL) — stream will open when ready"}
    except OSError as exc:
        return {"ok": False, "why": f"failed to open remote client: {exc}"}
