"""Low-overhead status & lifecycle controller for SOL-Pad (iPad 2 USB monitor & dock)."""

from __future__ import annotations

import ctypes
import plistlib
import socket
import struct
import subprocess
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
    subprocess.run(
        ["taskkill", "/IM", "sol-pad-stream.exe", "/IM", "sol-pad-dock.exe", "/F"],
        capture_output=True,
        creationflags=0x08000000,
    )
    return {"ok": True, "why": "stopped sol-pad-stream & sol-pad-dock"}


def restart_pad() -> dict:
    stop_pad()
    time.sleep(0.35)
    return start_pad()
