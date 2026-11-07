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
        except Exception:
            pass
        _HTTP = None


def get_deck_process_status() -> dict:
    """Check whether sol-deck.exe is running on Windows even if HTTP is unreachable."""
    try:
        for p in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
            name = (p.info.get("name") or "").lower()
            if name in ("sol-deck.exe", "sol-deck-cli.exe", "sol-deck"):
                mem = p.info.get("memory_info")
                ram_mb = round(mem.rss / (1024 * 1024), 1) if mem else 0.0
                create_t = p.info.get("create_time") or 0
                uptime = int(time.time() - create_t) if create_t else 0
                return {
                    "running": True,
                    "pid": p.info.get("pid"),
                    "ram_mb": ram_mb,
                    "uptime_sec": uptime,
                }
    except Exception:
        pass
    return {"running": False, "pid": None, "ram_mb": 0.0, "uptime_sec": 0}


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
                "ip": "192.168.0.25",
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
            "client": {"ip": "192.168.0.25", "name": "iPad mini 1", "active": False, "status_text": f"HTTP {r.status_code}"},
            "summary": {"status": "error", "canary_active": False},
        }
    except Exception as e:
        return {
            "up": False,
            "status": "offline",
            "error": f"{type(e).__name__}: {e}",
            "process": proc_status,
            "client": {"ip": "192.168.0.25", "name": "iPad mini 1", "active": False, "status_text": "Service Offline (:5000)"},
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
