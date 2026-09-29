"""RAM, disks, backups, stability event counts, WSL state. Read-only; never starts WSL."""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

BASELINE = datetime(2026, 9, 13, 21, 22, 27)  # D:\OBVLT\reports\baseline-2026-09-13.txt
ACK_FILE = Path(r"D:\OBVLT\reports\stability-ack.json")
_NO_WINDOW = 0x08000000


def memory() -> dict:
    vm = psutil.virtual_memory()
    return {"total_gb": round(vm.total / 1024**3, 1), "used_gb": round(vm.used / 1024**3, 1), "percent": vm.percent}


def disks(letters=("C", "D", "E")) -> list[dict]:
    out = []
    for d in letters:
        try:
            u = psutil.disk_usage(f"{d}:\\")
            out.append({"drive": d, "free_gb": round(u.free / 1024**3), "total_gb": round(u.total / 1024**3), "percent": u.percent})
        except OSError:
            out.append({"drive": d, "error": "unavailable"})
    return out


def backups(pattern: str = r"E:\Backups\ai-dev-backup-*.tar") -> dict:
    files = glob.glob(pattern)
    if not files:
        return {"latest": None, "age_hours": None}
    newest = max(files, key=os.path.getmtime)
    age_h = (time.time() - os.path.getmtime(newest)) / 3600
    return {"latest": os.path.basename(newest), "age_hours": round(age_h, 1), "stale": age_h > 24 * 7}


def _run(args: list[str], timeout: float) -> str:
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
                          env={**os.environ, "WSL_UTF8": "1"}).stdout.decode("utf-8", "replace")


def acknowledged_until(path: Path = ACK_FILE) -> tuple[datetime | None, str]:
    """Events up to this time were investigated (reports\\stability-ack.json, written by hand after an analysis)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return datetime.fromisoformat(data["acknowledged_until"]), data.get("note", "")
    except (OSError, ValueError, KeyError):
        return None, ""


def stability(since: datetime | None = None) -> dict:
    """Counts of WHEA hardware errors, unexpected reboots (41) and GPU driver resets (4101) since the baseline,
    or since the acknowledged time if that is later - so an already-analyzed crash doesn't FAIL forever."""
    ack, note = acknowledged_until()
    if since is None:
        since = max(BASELINE, ack) if ack else BASELINE
    ps = (
        f"$b=[datetime]'{since:%Y-%m-%d %H:%M:%S}';"
        "function C($h){@(Get-WinEvent -FilterHashtable $h -ErrorAction SilentlyContinue).Count};"
        "'{0} {1} {2}' -f (C @{LogName='System';ProviderName='Microsoft-Windows-WHEA-Logger';StartTime=$b}),"
        "(C @{LogName='System';Id=41;StartTime=$b}),(C @{LogName='System';Id=4101;StartTime=$b})"
    )
    try:
        whea, kp41, tdr = (int(x) for x in _run(["powershell", "-NoProfile", "-Command", ps], 20).split())
    except (ValueError, subprocess.SubprocessError, OSError):
        return {"available": False}
    return {"available": True, "since": since.isoformat(), "acknowledged": note if ack and since == ack else "",
            "whea": whea, "unexpected_reboots": kp41, "gpu_driver_resets": tdr}


def wsl(distro: str = "Ubuntu-24.04") -> dict:
    """State from `wsl -l -v`. Only queries failed units when the distro is already running (never boots it)."""
    try:
        listing = _run(["wsl.exe", "-l", "-v"], 10)
    except (subprocess.SubprocessError, OSError):
        return {"available": False}
    state = parse_wsl_list(listing).get(distro)
    result = {"available": True, "distro": distro, "state": state or "not registered"}
    if state == "Running":
        try:
            failed = _run(["wsl.exe", "-d", distro, "--", "systemctl", "--failed", "--no-legend", "--plain"], 10)
            result["failed_units"] = [line.split()[0] for line in failed.splitlines() if line.strip()]
        except (subprocess.SubprocessError, OSError):
            result["failed_units"] = None
    return result


def parse_wsl_list(text: str) -> dict[str, str]:
    states = {}
    for line in text.replace("\x00", "").splitlines()[1:]:
        parts = line.replace("*", " ").split()
        if len(parts) >= 3:
            states[parts[0]] = parts[1]
    return states
