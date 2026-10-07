"""The dashboard's Settings panel: what it reads, what it accepts, and that ticker changes reach the Tk thread whole."""
import queue

import pytest

from sol_control_hud import hub, settings_api
from sol_control_hud.views.settings_dialog import PRESETS


class FakeTicker:
    def __init__(self):
        self.settings = {"interval_seconds": 6, "poll_pace": 2.0, "scan_interval": 60, "stability_interval": 300,
                         "monitor_self": True, "alerts_sound": True, "theme": "cyber-cyan"}
        self.slides_enabled = {"HW": True, "AI": True}
        self.font_scale = "normal"
        self.user_hidden = False
        self.theme_name = "cyber-cyan"


@pytest.fixture
def the_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.ticker = FakeTicker()
    return h


def drain(h):
    out = []
    while True:
        try:
            out.append(h.cmds.get_nowait())
        except queue.Empty:
            return out


def test_read_lists_everything(the_hub):
    d = settings_api.read(the_hub)
    assert set(d["app"]) == {"ticker_shown", "start_at_login", "dashboard_at_start", "notify", "hud_heal"}
    assert d["app"]["hud_heal"] is False
    t = d["ticker"]
    assert t["preset"] == "balanced" and t["interval_seconds"] == 6 and t["alerts_sound"] is True
    assert any(s["tag"] == "APPS" for s in t["slides"]) and {p["key"] for p in t["presets"]} == set(PRESETS)


def test_ticker_changes_go_whole_to_the_tk_thread(the_hub):
    """apply_new_settings treats a missing key as the default: a lone {"interval_seconds": 8} would reset the pace,
    the alert sound and the rest. So the panel always sends the full settings with the one change merged in."""
    assert settings_api.change(the_hub, "interval_seconds", "8")["ok"]
    (name, arg), = drain(the_hub)
    assert name == "ticker_settings" and arg["interval_seconds"] == 8 and arg["alerts_sound"] is True and arg["poll_pace"] == 2.0
    settings_api.change(the_hub, "preset", "eco")
    (name, arg), = drain(the_hub)
    assert arg["poll_pace"] == PRESETS["eco"]["poll_pace"] and arg["alerts_sound"] is True
    settings_api.change(the_hub, "slide:HW", False)
    assert drain(the_hub) == [("slide", ("HW", False))]
    settings_api.change(the_hub, "font_scale", "small")
    assert drain(the_hub) == [("font_scale", "small")]


def test_bad_values_are_refused(the_hub):
    for key, value in (("interval_seconds", "1"), ("interval_seconds", "x"), ("preset", "ludicrous"),
                       ("font_scale", "huge"), ("slide:NOPE", True), ("hud_heal", "maybe"), ("rm -rf", True)):
        with pytest.raises(settings_api.SettingError):
            settings_api.change(the_hub, key, value)
    the_hub.ticker.slides_enabled = {"HW": True, **{t: False for t, _ in settings_api.SLIDE_LABELS if t != "HW"}}
    with pytest.raises(settings_api.SettingError, match="at least one"):
        settings_api.change(the_hub, "slide:HW", False)
    assert drain(the_hub) == []


def test_hud_heal_is_saved(the_hub):
    assert settings_api.change(the_hub, "hud_heal", True)["ok"]
    assert hub.load_settings().get("hud_heal") is True


def test_setting_action_needs_our_header(the_hub):
    from fastapi.testclient import TestClient
    from sol_control_hud.data.snapshot import Snapshot
    the_hub.collector = type("C", (), {"get_snapshot": lambda self: Snapshot(), "_sampler": type("S", (), {"latest": {}})()})()
    the_hub.guard = hub.LazyGuard(the_hub.collector._sampler)
    client = TestClient(the_hub.build_app())
    body = {"action": "setting", "target": "interval_seconds", "value": 9}
    assert client.post("/api/action", json=body).status_code == 403
    assert client.post("/api/action", json=body, headers={"X-SOL-Control": "1"}).json()["ok"]
    assert client.get("/api/settings").json()["ticker"]["interval_seconds"] == 6       # applied later, on the Tk thread
