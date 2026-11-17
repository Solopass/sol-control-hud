"""Deck services card: the payload builder and the down-too-long warning (fake clocks, no processes, no network)."""
from __future__ import annotations

from sol_control_hud import deck_services as ds

REG = {"projects": {
    "sol-deck": {"title": "SOL iPad Deck", "port": 5000, "autostart": True, "start": {"cmd": ["x"]}},
    "sol-cs2": {"title": "SOL CS2", "port": 5010, "autostart": True, "start": {"cmd": ["x"]}},
    "sol-control-hud": {"self": True, "autostart": True},
    "other": {"title": "Other", "port": 1, "start": {"cmd": ["x"]}},
}}


def _rows(deck_up=True, cs2_up=False):
    """The ports collector's listening rows."""
    out = []
    if deck_up:
        out.append({"port": 5000, "pid": 11, "name": "sol-deck", "up_seconds": 600})
    if cs2_up:
        out.append({"port": 5010, "pid": 22, "name": "sol-cs2", "up_seconds": 5})
    out.append({"port": 8082, "pid": 33, "name": "node", "up_seconds": 9})
    return out


def test_only_autostart_projects_and_never_self():
    assert ds.autostart_names(REG) == ["sol-deck", "sol-cs2"]


def test_payload_up_down_results_and_ipad():
    sec = {"up": True, "client": {"active": True, "name": "iPad mini 1", "status_text": "Connected"}}
    results = {"sol-cs2": {"ok": False, "why": "port taken", "at": 900.0}}
    p = ds.build_payload(REG, _rows(deck_up=True, cs2_up=False), results, sec, now=1000.0)
    deck, cs2 = p["services"]
    assert deck["up"] is True and deck["up_seconds"] == 600 and deck["pid"] == 11 and deck["autostart"] is None
    assert deck["can_start"] is False
    assert cs2["up"] is False and cs2["can_start"] is True
    assert cs2["autostart"] == {"ok": False, "why": "port taken", "ago_s": 100}
    assert p["down"] == ["sol-cs2"]
    assert p["ipad"] == {"deck_up": True, "active": True, "name": "iPad mini 1", "status_text": "Connected"}


def test_payload_nothing_listening_and_deck_offline():
    p = ds.build_payload(REG, [], None, {"up": False, "client": {"active": False, "status_text": "Service Offline (:5000)"}}, 1.0)
    assert [s["up"] for s in p["services"]] == [False, False]
    assert all(s["can_start"] for s in p["services"])            # down and startable
    assert p["ipad"]["deck_up"] is False and p["ipad"]["active"] is False
    assert ds.build_payload(REG, [], None, None, 1.0)["ipad"]["deck_up"] is False


def test_down_watch_waits_for_two_minutes_and_hud_uptime():
    w = ds.DownWatch()
    assert w.step(["sol-deck"], 0.0, 500.0) == []
    assert w.step(["sol-deck"], 120.0, 620.0) == []              # exactly 2 minutes is not "more than"
    got = w.step(["sol-deck"], 121.0, 621.0)
    assert [n for n, _ in got] == ["sol-deck"]


def test_down_watch_hud_just_started_holds_the_warning():
    w = ds.DownWatch()
    w.step(["sol-deck"], 0.0, 10.0)
    assert w.step(["sol-deck"], 200.0, 60.0) == []               # down 200 s, but the HUD is only 60 s old
    assert len(w.step(["sol-deck"], 260.0, 121.0)) == 1


def test_down_watch_once_per_period_and_rearms_when_back():
    w = ds.DownWatch()
    w.step(["sol-cs2"], 0.0, 1000.0)
    first = w.step(["sol-cs2"], 130.0, 1130.0)
    assert len(first) == 1
    assert w.step(["sol-cs2"], 200.0, 1200.0) == []              # same outage: no repeat
    assert w.step(["sol-cs2"], 5000.0, 6000.0) == []
    assert w.step([], 5001.0, 6001.0) == []                      # back up: cleared
    w.step(["sol-cs2"], 6000.0, 7000.0)
    second = w.step(["sol-cs2"], 6130.0, 7130.0)
    assert len(second) == 1 and second[0][1] != first[0][1]      # a new outage gets a new key


def test_down_watch_tracks_projects_separately():
    w = ds.DownWatch()
    w.step(["sol-deck"], 0.0, 1000.0)
    w.step(["sol-deck", "sol-cs2"], 100.0, 1100.0)
    got = w.step(["sol-deck", "sol-cs2"], 130.0, 1130.0)
    assert [n for n, _ in got] == ["sol-deck"]                   # cs2 has only been down 30 s


def test_deck_payload_never_reads_the_projects_collector(monkeypatch, tmp_path):
    from sol_control_hud import hub
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), False, False)

    class Boom:
        def get(self):
            raise AssertionError("projects collector read")

    class Val:
        def __init__(self, v):
            self.v = v

        def get(self):
            return self.v

    h.collectors = {"projects": Boom(), "media_probe": Boom(),
                    "ports": Val({"listening": [{"port": 5000, "pid": 7, "name": "sol-deck", "up_seconds": 42}]}),
                    "security": Val({"up": True, "client": {}})}
    p = h.deck_services_payload()
    deck = next(s for s in p["services"] if s["name"] == "sol-deck")
    assert deck["up"] is True and deck["pid"] == 7 and deck["up_seconds"] == 42
