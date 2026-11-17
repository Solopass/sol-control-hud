"""Cyber Security & Canary Watchdog collector for SOL HUD.
Talks to sol-deck (:5000) over loopback (sol-deck trusts SOL itself, so no PIN
is needed or stored here), and manages
the sol-deck background service lifecycle on SOL PC.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
import httpx
import psutil

from ... import swallow

DECK_URL = "http://127.0.0.1:5000"
DECK_DIR = Path("D:/Workspace/sol-deck")
RUN_DECK_PS1 = DECK_DIR / "run-deck.ps1"

_HTTP: httpx.Client | None = None


def _client() -> httpx.Client:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client()
    return _HTTP


def close_client() -> None:
    global _HTTP
    if _HTTP is not None:
        try:
            _HTTP.close()
        except httpx.HTTPError:
            swallow.note("security.close_client")
        _HTTP = None


_DECK_NAMES = ("sol-deck.exe", "sol-deck-cli.exe", "sol-deck")
FULL_SCAN_EVERY = 30.0                    # seconds between full process scans while sol-deck isn't found
_NOT_RUNNING = {"running": False, "pid": None, "ram_mb": 0.0, "uptime_sec": 0}
_deck_seen: dict = {"pid": None, "create_time": 0.0, "name": "", "scan_at": None}   # scan_at: monotonic time of the last full scan


def reset_deck_cache() -> None:
    """Forget the remembered pid and the scan time, so the next status call rescans at once (after start/stop/restart)."""
    _deck_seen.update(pid=None, create_time=0.0, name="", scan_at=None)


def _deck_info(p: "psutil.Process", create_t: float) -> dict:
    try:
        ram_mb = round(p.memory_info().rss / (1024 * 1024), 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        ram_mb = 0.0
    return {"running": True, "pid": p.pid, "ram_mb": ram_mb,
            "uptime_sec": int(time.time() - create_t) if create_t else 0}


def get_deck_process_status() -> dict:
    """Check whether sol-deck.exe is running on Windows even if HTTP is unreachable.

    Cheap on purpose (it is called every 2 s): the last pid found is re-checked directly; a full process scan happens
    only when that fails, and then at most every FULL_SCAN_EVERY seconds."""
    pid = _deck_seen["pid"]
    if pid is not None:
        try:
            p = psutil.Process(pid)
            create_t = p.create_time()
            if (p.name() or "").lower() == _deck_seen["name"] and abs(create_t - _deck_seen["create_time"]) <= 1.0:
                return _deck_info(p, create_t)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        _deck_seen.update(pid=None, create_time=0.0, name="")
    now = time.monotonic()
    last = _deck_seen["scan_at"]
    if last is not None and now - last < FULL_SCAN_EVERY:
        return dict(_NOT_RUNNING)
    _deck_seen["scan_at"] = now
    try:
        for p in psutil.process_iter(["pid", "name"]):
            name = (p.info.get("name") or "").lower()
            if name not in _DECK_NAMES:
                continue
            try:
                create_t = p.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            _deck_seen.update(pid=p.pid, create_time=create_t, name=name)
            return _deck_info(p, create_t)
    except (psutil.Error, OSError):
        swallow.note("security.get_deck_process_status")
    return dict(_NOT_RUNNING)


def start_deck() -> dict:
    """Start sol-deck background service via run-deck.ps1."""
    if not RUN_DECK_PS1.exists():
        return {"ok": False, "why": f"run-deck.ps1 not found at {RUN_DECK_PS1}"}
    try:
        CREATE_NO_WINDOW = 0x08000000
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUN_DECK_PS1),
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(0.7)
        reset_deck_cache()
        st = get_deck_process_status()
        if st["running"]:
            close_client()
            return {"ok": True, "why": f"sol-deck started (PID {st['pid']})"}
        out = (r.stdout + " " + r.stderr).strip()
        return {"ok": False, "why": f"Start failed: {out or 'process exited'}"}
    except Exception as e:
        return {"ok": False, "why": f"Start error: {e}"}


def stop_deck() -> dict:
    """Stop sol-deck background service via run-deck.ps1 -Stop."""
    if not RUN_DECK_PS1.exists():
        return {"ok": False, "why": f"run-deck.ps1 not found at {RUN_DECK_PS1}"}
    try:
        CREATE_NO_WINDOW = 0x08000000
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUN_DECK_PS1),
            "-Stop",
        ]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        close_client()
        reset_deck_cache()
        return {"ok": True, "why": "sol-deck stopped"}
    except Exception as e:
        return {"ok": False, "why": f"Stop error: {e}"}


def restart_deck() -> dict:
    """Restart sol-deck background service via run-deck.ps1 -Restart."""
    if not RUN_DECK_PS1.exists():
        return {"ok": False, "why": f"run-deck.ps1 not found at {RUN_DECK_PS1}"}
    try:
        CREATE_NO_WINDOW = 0x08000000
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUN_DECK_PS1),
            "-Restart",
        ]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(0.7)
        close_client()
        reset_deck_cache()
        st = get_deck_process_status()
        if st["running"]:
            return {"ok": True, "why": f"sol-deck restarted (PID {st['pid']})"}
        return {"ok": True, "why": "sol-deck restarted"}
    except Exception as e:
        return {"ok": False, "why": f"Restart error: {e}"}


def get_security_status(timeout: float = 1.5) -> dict:
    """Fetch current security posture and watchdog state from sol-deck."""
    proc_status = get_deck_process_status()
    try:
        c = _client()
        r = c.get(f"{DECK_URL}/api/security", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            proc = data.get("process") or proc_status
            if isinstance(proc, dict) and proc_status.get("running"):
                proc["running"] = True
            client = data.get("client") or {
                "ip": "192.168.0.124",
                "name": "iPad mini 1",
                "active": False,
                "status_text": "No recent requests",
            }
            return {
                "up": True,
                "summary": data.get("summary", {}),
                "settings": data.get("settings", {}),
                "alerts": data.get("alerts", []),
                "devices": data.get("devices", []),
                "arp_table": data.get("arp_table", []),
                "port_leaks": data.get("port_leaks", []),
                "process": proc,
                "client": client,
            }
        return {
            "up": False,
            "status": "error",
            "error": f"sol-deck responded with HTTP {r.status_code}",
            "process": proc_status,
            "client": {"ip": "192.168.0.124", "name": "iPad mini 1", "active": False, "status_text": f"HTTP {r.status_code}"},
            "summary": {"status": "error", "canary_active": False},
        }
    except Exception as e:
        return {
            "up": False,
            "status": "offline",
            "error": f"{type(e).__name__}: {e}",
            "process": proc_status,
            "client": {"ip": "192.168.0.124", "name": "iPad mini 1", "active": False, "status_text": "Service Offline (:5000)"},
            "summary": {"status": "offline", "canary_active": False},
        }


def toggle_security_feature(target: str = "all", timeout: float = 2.0) -> dict:
    """Toggle canary, arp, unknown device alerts, sound, or master switch."""
    cur = get_security_status(timeout=timeout)
    if not cur.get("up"):
        return {"ok": False, "why": "sol-deck is not running (:5000)"}

    settings = dict(cur.get("settings", {}))
    summary = cur.get("summary", {})

    if target == "all":
        # If any guard is on, turn both canary and arp off; otherwise turn both on
        any_on = summary.get("canary_active") or settings.get("arp_watchdog_enabled")
        settings["canary_enabled"] = not any_on
        settings["arp_watchdog_enabled"] = not any_on
        action_name = "enabled" if not any_on else "disabled"
    elif target == "canary":
        settings["canary_enabled"] = not settings.get("canary_enabled", True)
        action_name = "enabled" if settings["canary_enabled"] else "disabled"
    elif target == "arp":
        settings["arp_watchdog_enabled"] = not settings.get("arp_watchdog_enabled", True)
        action_name = "enabled" if settings["arp_watchdog_enabled"] else "disabled"
    elif target == "unknown":
        settings["unknown_device_alerts"] = not settings.get("unknown_device_alerts", True)
        action_name = "enabled" if settings["unknown_device_alerts"] else "disabled"
    elif target == "ipad_sound":
        settings["ipad_sound_alert"] = not settings.get("ipad_sound_alert", True)
        action_name = "unmuted" if settings["ipad_sound_alert"] else "muted"
    else:
        return {"ok": False, "why": f"Unknown toggle target: {target}"}

    try:
        c = _client()
        payload = {"action": "update_settings", "settings": settings}
        r = c.post(f"{DECK_URL}/api/security", json=payload, timeout=timeout)
        if r.status_code == 200:
            return {"ok": True, "why": f"Watchdog {target} {action_name}"}
        return {"ok": False, "why": f"HTTP {r.status_code} saving settings"}
    except Exception as e:
        return {"ok": False, "why": f"Error updating settings: {e}"}


def trigger_test_alarm(timeout: float = 2.0) -> dict:
    """Trigger canary test alarm on iPad."""
    try:
        c = _client()
        payload = {"action": "test_canary"}
        r = c.post(f"{DECK_URL}/api/security", json=payload, timeout=timeout)
        if r.status_code == 200:
            return {"ok": True, "why": "🚨 Test Canary Alarm sent to iPad (PC silent)"}
        return {"ok": False, "why": f"HTTP {r.status_code} triggering alarm"}
    except Exception as e:
        return {"ok": False, "why": f"Error triggering alarm: {e}"}


def acknowledge_alerts(timeout: float = 2.0) -> dict:
    """Acknowledge active alerts."""
    try:
        c = _client()
        payload = {"action": "ack_alerts"}
        r = c.post(f"{DECK_URL}/api/security", json=payload, timeout=timeout)
        if r.status_code == 200:
            return {"ok": True, "why": "Security alerts acknowledged"}
        return {"ok": False, "why": f"HTTP {r.status_code} acknowledging alerts"}
    except Exception as e:
        return {"ok": False, "why": f"Error acknowledging alerts: {e}"}
