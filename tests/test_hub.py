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


def test_dashboard_shortcut_opens_the_page_without_touching_the_ticker(monkeypatch):
    sent = []

    class R:
        status_code = 200
    monkeypatch.setattr("httpx.post", lambda url, json, timeout: sent.append(json) or R())
    hub.tell_running_hub(7900, "open-dashboard")
    assert sent == [{"dashboard": True}]                       # no "ticker" key: a hidden ticker stays hidden


def test_tray_tooltip_alert_and_normal(the_hub):
    class FakeTray:
        def __init__(self):
            self.tip = ""

        def set_tooltip(self, tip: str):
            self.tip = tip

    the_hub.tray = FakeTray()
    the_hub.root = type("R", (), {"after": lambda *a, **k: None})()

    # Case 1: Crash alert present (gpu_resets=1)
    the_hub._tick()
    assert "🔴 CRASH ALERT:" in the_hub.tray.tip

    # Case 2: Clean snapshot
    the_hub.collector.snap = Snapshot(ai_state="loaded", ai_model="sol-smart", ai_mode="desk", gpu_temp=45, sampled_at=2.0)
    the_hub._tick()
    assert the_hub.tray.tip == "🟢 SOL: sol-smart · desk · GPU 45°C"


def test_tray_menu_includes_unload_models(the_hub, monkeypatch):
    menu_fn = None

    class CaptureTray:
        def __init__(self, tip, menu, *a, **k):
            nonlocal menu_fn
            menu_fn = menu

        def start(self):
            return True

    monkeypatch.setattr("sol_control_hud.tray.Tray", CaptureTray)
    the_hub._start_tray()
    assert menu_fn is not None
    items = menu_fn()
    labels = [item[0] for item in items if item is not None]
    assert "Unload AI models" in labels



def test_closed_dashboard_connection_is_not_logged_as_an_error():
    import logging
    import sys as _sys
    f = hub.ClosedConnectionFilter()

    def record(msg, exc):
        try:
            raise exc
        except Exception:
            return logging.LogRecord("asyncio", logging.ERROR, __file__, 1, msg, None, _sys.exc_info())
    reset = ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
    assert not f.filter(record("Exception in callback _ProactorBasePipeTransport._call_connection_lost()", reset))
    assert f.filter(record("Exception in callback something_else()", reset))            # other callbacks still logged
    assert f.filter(record("Exception in callback _ProactorBasePipeTransport._call_connection_lost()", ValueError()))


def test_the_start_notes_how_the_last_run_ended(tmp_path, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(hub, "LOG_FILE", tmp_path / "hub.log")
    marker = tmp_path / "app-running.json"
    boot = datetime(2026, 10, 8, 9, 0, 0).timestamp()
    assert hub.note_previous_end(marker, boot) is None                  # last run exited cleanly: no marker, no line
    marker.write_text(json.dumps({"pid": 7, "started": "2026-10-08T12:00:00"}))
    assert hub.note_previous_end(marker, boot) == hub.health.ENDED_UNEXPECTEDLY
    marker.write_text(json.dumps({"pid": 8, "started": "2026-10-08T08:00:00"}))
    assert hub.note_previous_end(marker, boot) == hub.health.ENDED_WITH_PC
    marker.write_text("{half a file")
    assert hub.note_previous_end(marker, boot) == hub.health.ENDED_UNEXPECTEDLY
    lines = (tmp_path / "hub.log").read_text().splitlines()
    assert len(lines) == 3 and "(pid 7, started 2026-10-08T12:00:00)" in lines[0]
    assert hub.health.restarts("\n".join(lines), now=datetime(2026, 10, 8, 18).timestamp())["week"] == 2


def test_the_hud_alert_tops_the_ticker_attention():
    from sol_control_hud.data.snapshot import RED, attention
    got = attention(Snapshot(hud_alert="HUD: GPU counters stopped updating"))
    assert got[0] == (RED, "HUD: GPU counters stopped updating", "SYS")
    assert not any("HUD" in a[1] for a in attention(Snapshot()))


def test_the_weekly_digest_is_written_once_outside_quiet_hours(tmp_path, monkeypatch):
    from datetime import datetime as real_dt
    from sol_control_hud import digest
    monkeypatch.setattr(digest, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(digest, "NOTES_DIR", tmp_path / "Digests")
    monkeypatch.setattr(hub, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hub, "LOG_FILE", tmp_path / "hub.log")
    clock = {"now": real_dt(2026, 10, 8, 18, 0)}

    class FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]
    import datetime as dtmod
    monkeypatch.setattr(dtmod, "datetime", FakeDT)
    stub = type("StubHub", (), {"settings": {"notify": {"quiet": "23:00-08:00"}}})()
    run = lambda: hub.Hub._weekly_digest(stub, Snapshot())    # noqa: E731
    assert run() == [] and digest.load_state() == {"last": "2026-W40"}       # first start: nothing written
    clock["now"] = real_dt(2026, 10, 11, 23, 30)                              # due, but quiet hours
    assert run() == [] and not (tmp_path / "Digests").exists()
    clock["now"] = real_dt(2026, 10, 12, 8, 1)                                # the morning after: written
    (n,) = run()
    assert n.kind == "digest" and "2026-W41 machine.md" in n.text
    assert (tmp_path / "Digests" / "2026-W41 machine.md").exists() and digest.load_state() == {"last": "2026-W41"}
    assert run() == []                                                        # once


def test_the_hub_shuts_the_ticker_down_for_a_game(the_hub):
    calls = []
    t = type("T", (), {"user_hidden": False, "_is_hidden_for_fullscreen": False, "_game_off": False})()
    t.shut_down_for_game = lambda: (calls.append("down"), setattr(t, "_game_off", True))
    t.come_back_after_game = lambda: (calls.append("back"), setattr(t, "_game_off", False))
    the_hub.ticker = t
    the_hub.root = type("R", (), {"after": lambda *a, **k: None})()
    the_hub.collector.snap = Snapshot(ai_mode="off", ai_reason="game: game cs2", sampled_at=1.0)
    the_hub._tick()
    the_hub._tick()                                       # still in the game: not again
    the_hub.collector.snap = Snapshot(ai_mode="desk", ai_reason="game ended", sampled_at=2.0)
    the_hub._tick()
    assert calls == ["down", "back"]
    assert hub.snap_game_on(Snapshot(ai_mode="off", ai_reason="manual")) is False
