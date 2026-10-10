"""Dashboard layout presets (layouts.py + /api/layouts): validation, storage, actions, auto-switch, ticker slides."""
import json

import pytest

from sol_control_hud import layouts as L


def test_everyday_is_the_dashboard_as_it_was():
    every = L.default_presets()[0]
    assert every["name"] == "Everyday" and every["ticker_slides"] is None and every["auto_when"] is None
    assert [w["id"] for w in every["widgets"]] == list(L.WIDGETS)                 # today's order
    assert all(not w["hidden"] for w in every["widgets"])
    assert {w["id"]: w["size"] for w in every["widgets"]}["c-gpu"] == "L"          # tall, as before
    for w in every["widgets"]:
        assert w["size"] == L.WIDGETS[w["id"]][2]


def test_every_card_has_its_sizes_and_the_starters_are_valid():
    for wid, (title, sizes, default) in L.WIDGETS.items():
        assert default in sizes and set(sizes) <= set(L.SIZES), wid
        assert 2 <= len(sizes) <= 5 or wid == "c-review", wid                    # the exam card: layout only
    for p in L.default_presets():
        assert L.clean_preset(p) == p                                             # starters pass their own checks
    names = [p["name"] for p in L.default_presets()]
    assert names == ["Everyday", "Gaming", "AI work", "Dev", "Overnight", "Minimal"]


def test_clean_preset_repairs_what_it_can_and_refuses_the_rest():
    p = L.clean_preset({"name": " Mine ", "widgets": [{"id": "c-vram", "size": "XS"}, {"id": "nope", "size": "M"},
                                                       {"id": "c-vram", "size": "L"}, {"id": "c-svc", "size": "XL"}]})
    ws = {w["id"]: w for w in p["widgets"]}
    assert p["name"] == "Mine" and p["widgets"][0] == {"id": "c-vram", "size": "XS", "hidden": False}
    assert ws["c-svc"]["size"] == "M"                                             # no XL for Services: its default
    assert "nope" not in ws and len(p["widgets"]) == len(L.WIDGETS)               # missing cards added, hidden
    assert ws["c-gpu"]["hidden"] is True
    for bad in ({"name": ""}, {"name": "<script>"}, {"name": "x", "auto_when": "always"}, {"name": "x", "ticker_slides": []},
                {"name": "x", "ticker_slides": ["BOGUS"]}, "not a dict"):
        with pytest.raises(L.Bad):
            L.clean_preset(bad)
    assert L.clean_preset({"name": "x", "ticker_slides": ["AI", "AI", "BOGUS", "HW"]})["ticker_slides"] == ["AI", "HW"]


def test_a_card_added_later_shows_in_everyday_and_hides_elsewhere():
    old = L.default_data()
    for p in old["presets"]:
        p["widgets"] = [w for w in p["widgets"] if w["id"] != "c-health"]       # a file from before the card existed
    m = L.merge(json.loads(json.dumps(old)))
    every = {w["id"]: w for w in m["presets"][0]["widgets"]}
    gaming = {w["id"]: w for w in m["presets"][1]["widgets"]}
    assert every["c-health"]["hidden"] is False and gaming["c-health"]["hidden"] is True


def test_load_save_and_a_broken_file_is_kept(tmp_path):
    f = tmp_path / "layouts.json"
    assert L.load(f)["active"] == "Everyday"                                      # no file: the defaults
    d = L.save(L.apply_action(L.load(f), {"action": "activate", "name": "Dev"})[0], f)
    assert L.load(f)["active"] == "Dev" and d["rev"] == 1
    assert not list(tmp_path.glob("*.tmp"))
    f.write_text("{broken", encoding="utf-8")
    assert L.load(f)["active"] == "Everyday"
    assert (tmp_path / "layouts.json.bad").read_text(encoding="utf-8") == "{broken"


def test_actions():
    d = L.default_data()
    d, _ = L.apply_action(d, {"action": "save", "preset": {"name": "Study", "widgets": [{"id": "c-notes", "size": "XL"}]}})
    assert d["presets"][-1]["name"] == "Study"
    d, _ = L.apply_action(d, {"action": "activate", "name": "Study"})
    d, _ = L.apply_action(d, {"action": "save", "old": "Study", "preset": {"name": "Reading", "widgets": []}})
    assert d["active"] == "Reading" and "Study" not in [p["name"] for p in d["presets"]]      # renamed, still active
    with pytest.raises(L.Bad):
        L.apply_action(d, {"action": "save", "old": "Reading", "preset": {"name": "Dev", "widgets": []}})   # taken
    d, _ = L.apply_action(d, {"action": "move", "name": "Reading", "to": 0})
    assert d["presets"][0]["name"] == "Reading"
    d, _ = L.apply_action(d, {"action": "delete", "name": "Reading"})
    assert d["active"] == d["presets"][0]["name"] == "Everyday"
    g = next(p for p in d["presets"] if p["name"] == "Gaming")
    g["widgets"][0]["size"] = "M"
    d, _ = L.apply_action(d, {"action": "reset", "name": "Gaming"})
    assert next(p for p in d["presets"] if p["name"] == "Gaming")["widgets"][0]["size"] == "XS"
    d, _ = L.apply_action(d, {"action": "auto", "on": False})
    assert d["auto"] is False
    for bad in ({"action": "activate", "name": "Nope"}, {"action": "reset", "name": "Mine"}, {"action": "explode"}):
        with pytest.raises(L.Bad):
            L.apply_action(d, bad)
    one = {**L.default_data(), "presets": L.default_presets()[:1]}
    with pytest.raises(L.Bad):
        L.apply_action(one, {"action": "delete", "name": "Everyday"})


def test_auto_switch_gives_back_what_you_had():
    a, d = L.Auto(), L.default_data()
    d["active"] = "Dev"
    assert a.step(d, None) is None
    assert a.step(d, "game") == "Gaming"
    d["active"] = "Gaming"
    assert a.step(d, "game") is None                                              # still in the game
    assert a.step(d, None) == "Dev"                                               # game over: back to Dev
    d["active"] = "Dev"
    assert a.step(d, "away") == "Overnight"
    d["active"] = "Minimal"                                                       # picked by hand during Away
    assert a.step(d, None) is None                                                # ... so it stays
    d["auto"] = False
    assert a.step(d, "game") is None                                              # auto-switch off


def test_ticker_slides_follow_the_preset_and_come_back():
    d = L.default_data()
    mine = {t: t in ("HW", "NET", "MEDIA") for t in L.TICKER_SLIDES}
    slides, base = L.ticker_changes(d, "AI work", mine)
    assert {t for t, on in slides.items() if on} == {"HW", "AI", "RUN", "SVC"} and base == ["HW", "NET", "MEDIA"]
    d["ticker_base"] = base
    slides, base = L.ticker_changes(d, "Everyday", {t: True for t in L.TICKER_SLIDES})
    assert {t for t, on in slides.items() if on} == {"HW", "NET", "MEDIA"} and base is None        # yours, back
    assert L.ticker_changes(L.default_data(), "Gaming", mine) == (None, None)       # no slides of its own: untouched


def test_condition_from_the_snapshot():
    from sol_control_hud.data.snapshot import Snapshot
    assert L.condition(Snapshot(ai_mode="off", ai_reason="game: game cs2")) == "game"
    assert L.condition(Snapshot(ai_mode="away")) == "away"
    assert L.condition(Snapshot(ai_mode="off", ai_reason="manual")) is None and L.condition(None) is None


def test_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sol_control_hud import hub
    monkeypatch.setattr(L, "FILE", tmp_path / "layouts.json")
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    from tests.conftest import FakeCollector
    h.collector = FakeCollector()
    h.guard = hub.LazyGuard(h.collector._sampler)
    h.ticker = type("T", (), {"slides_enabled": {t: True for t in L.TICKER_SLIDES}})()
    c = TestClient(h.build_app())
    got = c.get("/api/layouts").json()
    assert got["active"] == "Everyday" and len(got["widgets"]) == len(L.WIDGETS) and got["slides"][0]["tag"] == "HW"
    assert c.post("/api/layouts", json={"action": "activate", "name": "AI work"}).status_code == 403   # no header
    r = c.post("/api/layouts", json={"action": "activate", "name": "AI work"}, headers={"X-SOL-Control": "1"}).json()
    assert r["ok"] and r["active"] == "AI work" and r["ticker_base"] == list(L.TICKER_SLIDES)
    queued = []
    while not h.cmds.empty():
        queued.append(h.cmds.get())
    assert ("slide", ("NET", False)) in queued and not any(q[1][0] == "AI" for q in queued)   # only what must change
    assert json.loads((tmp_path / "layouts.json").read_text(encoding="utf-8"))["active"] == "AI work"
    bad = c.post("/api/layouts", json={"action": "activate", "name": "Nope"}, headers={"X-SOL-Control": "1"}).json()
    assert bad["ok"] is False and "Nope" in bad["why"]
