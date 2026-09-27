"""The hub: one collector, two views, the tray, the pace (phase 2 of SOL_UNIFIED_APP_PLAN)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from sol_control_hud import hub
from sol_control_hud.data.snapshot import Snapshot

OURS = {"X-SOL-Control": "1"}


def test_which_views_start():
    saved = {"ticker": True, "dashboard_at_start": False}
    assert hub.views_for(None, saved) == (True, False)                      # your saved choice
    assert hub.views_for(None, {"ticker": False, "dashboard_at_start": True}) == (False, True)
    assert hub.views_for("dashboard", saved) == (False, True)
    assert hub.views_for("both", saved) == (True, True)
    assert hub.views_for("tray", saved) == (False, False)


def test_pace_slows_down_while_nobody_looks():
    assert hub.pace_for(True, 1e9) == hub.ACTIVE_PACE                     # the ticker shows it
    assert hub.pace_for(False, 5.0) == hub.ACTIVE_PACE                    # a dashboard tab asked 5 s ago
    assert hub.pace_for(False, 120.0) == hub.IDLE_PACE                    # tray only / game on top


class FakeCollector:
    def __init__(self):
        self.snap = Snapshot(ai_state="sleeping", ai_model="sol-fast", gpu_resets=1, sampled_at=1.0)
        self._sampler = type("S", (), {"latest": {"available": False}})()

    def get_snapshot(self):
        return self.snap


class FakeTicker:
    user_hidden = False
    _is_hidden_for_fullscreen = False


@pytest.fixture
def the_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.collector, h.ticker = FakeCollector(), FakeTicker()
    h.guard = hub.LazyGuard(h.collector._sampler)
    return h


def test_dashboard_gets_the_tickers_data_and_can_switch_views(the_hub):
    c = TestClient(the_hub.build_app())
    s = c.get("/api/snapshot").json()
    assert s["snapshot"]["ai_model"] == "sol-fast"
    assert s["attention"][0][1].startswith("1 new crash event")             # the same alerts as the ticker
    assert any(sl["tag"] == "ALERT" for sl in s["slides"]) and s["views"]["ticker"] is True
    json.dumps(s)                                                            # all plain JSON
    assert the_hub.last_web > 0                                              # counts as someone looking
    r = c.post("/api/views", json={"ticker": False}, headers=OURS).json()
    assert r["queued"] and the_hub.cmds.get_nowait() == "hide_ticker"
    c.post("/api/views", json={"dashboard_at_start": True}, headers=OURS)
    assert the_hub.cmds.get_nowait() == ("dashboard_at_start", True)
    assert c.get("/").status_code == 200


def test_web_guard_starts_on_first_use_and_stops_when_idle(monkeypatch):
    started, stopped = [], []

    class FakeLoop:
        def __init__(self, *a, **k):
            self.latest = {"available": True}
            assert k.get("interval") == 4.0          # the web guard's pace

        def start(self):
            started.append(1)

        def stop(self):
            stopped.append(1)
    monkeypatch.setattr(hub.vram, "GuardLoop", FakeLoop)
    g = hub.LazyGuard(sampler=None)
    assert not started                                                       # nothing until a browser asks
    assert g.latest == {"available": False, "error": "starting"} and started == [1]
    assert g.latest == {"available": True}
    g.stop_if_idle(idle_s=3600)
    assert not stopped
    g.last_used = time.monotonic() - 7200
    g.stop_if_idle(idle_s=3600)
    assert stopped == [1] and g.loop is None


def test_tray_icon_comes_and_goes():
    from sol_control_hud.tray import Tray
    t = Tray("SOL Control HUD test", lambda: [("x", lambda: None, False), None])
    assert t.start() and t.hwnd
    t.set_tooltip("changed")
    t.stop()


def test_second_start_asks_the_running_one(monkeypatch):
    sent = []

    class R:
        status_code = 200
    monkeypatch.setattr("httpx.post", lambda url, json, timeout: sent.append((url, json)) or R())
    assert hub.tell_running_hub(7911, "dashboard")
    assert sent == [("http://127.0.0.1:7911/api/views", {"dashboard": True})]
    hub.tell_running_hub(7911, None)
    assert sent[-1][1] == {"ticker": True}


def test_running_marker_is_written_and_cleared(tmp_path):
    m = tmp_path / "app-running.json"
    hub.write_running_marker(m)
    data = json.loads(m.read_text())
    assert data["pid"] == __import__("os").getpid() and data["started"]
    hub.clear_running_marker(m)
    assert not m.exists()
    hub.clear_running_marker(m)                                   # already gone: fine


def test_start_at_login_switch(the_hub, monkeypatch):
    state = {"on": False}
    monkeypatch.setattr(hub, "start_at_login", lambda on=None: state.update(on=on) or on if on is not None else state["on"])
    r = the_hub.do_action("login_start", "on")
    assert r["ok"] and r["start_at_login"] is True and the_hub.views()["start_at_login"] is True
    r = the_hub.do_action("login_start", "off")
    assert r["start_at_login"] is False
    assert the_hub.do_action("login_start", "maybe")["ok"] is False
    c = TestClient(the_hub.build_app())
    assert c.post("/api/action", json={"action": "login_start", "target": "on"}).status_code == 403   # our page only
