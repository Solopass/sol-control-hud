"""The dashboard's Settings panel: everything you can change about the app, in one place.

Before this, the settings were spread out: start at login and notifications in the dashboard footer, the ticker's
performance preset / slides / timing / font only in the ticker's right-click dialog, and newer switches (the HUD's
own spill heal) nowhere at all.

Each change arrives as one (key, value). It is checked here; ticker changes go through the hub's command queue so
the Tk thread applies them (the ticker is not thread-safe), and always as the ticker's full settings with the one
change merged in: `apply_new_settings` treats a missing key as "back to the default".
"""
from __future__ import annotations

from .views.settings_dialog import PRESETS
from .views.ticker import FONT_SCALES, SLIDE_LABELS

PRESET_KEYS = ("poll_pace", "interval_seconds", "scan_interval", "stability_interval", "monitor_self")
TICKER_BOOLS = {"auto_hide_fullscreen": True, "alerts_pulse": True, "alerts_sound": False}


class SettingError(ValueError):
    pass


def _preset_of(settings: dict) -> str:
    for key, p in PRESETS.items():
        if all(settings.get(k, p[k]) == p[k] for k in PRESET_KEYS if k != "monitor_self"):
            return key
    return "custom"


def read(hub) -> dict:
    t = getattr(hub, "ticker", None)
    ts = dict(getattr(t, "settings", {}) or {})
    enabled = dict(getattr(t, "slides_enabled", {}) or {})
    views = hub.views()
    return {
        "app": {
            "ticker_shown": bool(views.get("ticker")),
            "start_at_login": bool(views.get("start_at_login")),
            "dashboard_at_start": bool(hub.settings.get("dashboard_at_start")),
            "notify": bool(views.get("notify")),
            "hud_heal": bool(hub.settings.get("hud_heal", False)),
        },
        "ticker": {
            "available": t is not None,
            "preset": _preset_of(ts),
            "presets": [{"key": k, "name": p["name"], "desc": p["desc"]} for k, p in PRESETS.items()],
            "interval_seconds": int(ts.get("interval_seconds", 6)),
            "font_scale": str(getattr(t, "font_scale", ts.get("font_scale", "normal"))),
            "font_scales": [{"key": k, "name": v["name"]} for k, v in FONT_SCALES.items()],
            "slides": [{"tag": tag, "label": label, "on": bool(enabled.get(tag, True))} for tag, label in SLIDE_LABELS],
            **{k: bool(ts.get(k, d)) for k, d in TICKER_BOOLS.items()},
        },
    }


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1", "on", "yes"):
        return True
    if str(value).lower() in ("false", "0", "off", "no"):
        return False
    raise SettingError("expected on or off")


def change(hub, key: str, value) -> dict:
    """Apply one change. Returns {"ok", "why"}; raises SettingError for anything not on the list."""
    t = getattr(hub, "ticker", None)

    def ticker_merge(update: dict) -> None:
        if t is None:
            raise SettingError("the ticker isn't running")
        hub.cmds.put(("ticker_settings", {**(t.settings or {}), **update}))

    if key == "ticker_shown":
        hub.show_ticker() if _bool(value) else hub.hide_ticker()
        return {"ok": True, "why": "ticker shown" if _bool(value) else "ticker hidden (still in the tray)"}
    if key == "start_at_login":
        return hub.do_action("login_start", "on" if _bool(value) else "off")
    if key == "notify":
        return hub.do_action("notify", "on" if _bool(value) else "off")
    if key == "dashboard_at_start":
        hub.cmds.put(("dashboard_at_start", _bool(value)))
        return {"ok": True, "why": f"open the dashboard when SOL starts: {'on' if _bool(value) else 'off'}"}
    if key == "hud_heal":
        from .hub import save_settings
        hub.settings["hud_heal"] = _bool(value)
        save_settings(hub.settings)
        return {"ok": True, "why": "the HUD's own spill heal is " + ("on (the watcher heals too: two may reload)"
                                                                     if _bool(value) else "off (the watcher heals)")}
    if key == "preset":
        if value not in PRESETS:
            raise SettingError(f"unknown preset {value!r}")
        ticker_merge({k: PRESETS[value][k] for k in PRESET_KEYS})
        return {"ok": True, "why": f"ticker: {PRESETS[value]['name']}"}
    if key == "interval_seconds":
        try:
            secs = int(value)
        except (TypeError, ValueError) as e:
            raise SettingError("seconds must be a whole number") from e
        if not 2 <= secs <= 60:
            raise SettingError("pick between 2 and 60 seconds")
        ticker_merge({"interval_seconds": secs})
        return {"ok": True, "why": f"each slide shows for {secs} s"}
    if key == "font_scale":
        if value not in FONT_SCALES:
            raise SettingError(f"unknown size {value!r}")
        if t is None:
            raise SettingError("the ticker isn't running")
        hub.cmds.put(("font_scale", value))
        return {"ok": True, "why": f"ticker text: {FONT_SCALES[value]['name']}"}
    if key in TICKER_BOOLS:
        ticker_merge({key: _bool(value)})
        return {"ok": True, "why": f"{key.replace('_', ' ')}: {'on' if _bool(value) else 'off'}"}
    if key.startswith("slide:"):
        tag = key.split(":", 1)[1]
        if tag not in dict(SLIDE_LABELS):
            raise SettingError(f"unknown slide {tag!r}")
        if t is None:
            raise SettingError("the ticker isn't running")
        on = _bool(value)
        if not on and sum(1 for g, _ in SLIDE_LABELS if t.slides_enabled.get(g, True)) <= 1:
            raise SettingError("keep at least one slide on")
        hub.cmds.put(("slide", (tag, on)))
        return {"ok": True, "why": f"{dict(SLIDE_LABELS)[tag]}: {'shown' if on else 'hidden'}"}
    raise SettingError(f"unknown setting {key!r}")
