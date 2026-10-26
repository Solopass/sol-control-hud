"""Cyber Security & Canary Watchdog collector for SOL HUD.
Talks to sol-deck (:5000) authenticated with X-Deck-PIN.
"""
from __future__ import annotations

import httpx

DECK_URL = "http://127.0.0.1:5000"
DECK_PIN = "4821"
_HTTP: httpx.Client | None = None


def _client() -> httpx.Client:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(headers={"X-Deck-PIN": DECK_PIN})
    return _HTTP


def close_client() -> None:
    global _HTTP
    if _HTTP is not None:
        try:
            _HTTP.close()
        except Exception:
            pass
        _HTTP = None


def get_security_status(timeout: float = 1.5) -> dict:
    """Fetch current security posture and watchdog state from sol-deck."""
    try:
        c = _client()
        r = c.get(f"{DECK_URL}/api/security", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            return {
                "up": True,
                "summary": data.get("summary", {}),
                "settings": data.get("settings", {}),
                "alerts": data.get("alerts", []),
                "devices": data.get("devices", []),
                "arp_table": data.get("arp_table", []),
                "port_leaks": data.get("port_leaks", []),
                "process": data.get("process", {}),
            }
        return {
            "up": False,
            "status": "error",
            "error": f"sol-deck responded with HTTP {r.status_code}",
            "summary": {"status": "error", "canary_active": False},
        }
    except Exception as e:
        return {
            "up": False,
            "status": "offline",
            "error": f"{type(e).__name__}: {e}",
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
