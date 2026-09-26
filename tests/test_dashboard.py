"""Phase 3 dashboard: who may press its buttons, what it gets pushed, the history and activity feed behind it."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from sol_control_hud import hub
from sol_control_hud.data.snapshot import Snapshot
from sol_control_hud.views.web import feed
from sol_control_hud.views.web.app import Cached

OURS = {"X-SOL-Control": "1"}


class FakeCollector:
    def __init__(self):
        self.snap = Snapshot(ai_state="loaded", ai_model="sol-fast", gpu_load=40.0, vram_used_gb=9.5,
                             vram_total_gb=16.0, ram_used_gb=30.0, cpu_percent=12.0, gpu_temp=60, sampled_at=100.0)
        self._sampler = type("S", (), {"latest": {"available": True, "engines": {"3D": 40.0}}})()

    def get_snapshot(self):
        return self.snap


@pytest.fixture
def app_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.collector = FakeCollector()
    h.ticker = type("T", (), {"user_hidden": False, "_is_hidden_for_fullscreen": False, "theme_name": "nordic-frost"})()
    h.guard = hub.LazyGuard(h.collector._sampler)
    app = h.build_app()
    fixed = {"llama_swap": {"up": True, "running": [{"model": "sol-fast", "state": "loaded"}]},
             "wsl": {"available": True, "state": "Stopped"}, "stability": {"available": True, "since": "2026-09-25T20:35:15"},
             "vram": {"available": False}, "away": {"mode": "desk", "queued": [], "running": []},
             "chains": {"running": None, "pending": [], "scheduled": []}, "activity": []}
    for k, v in fixed.items():
        h.collectors[k] = Cached(lambda v=v: v, 999)
    return h, TestClient(app)


def test_only_our_page_can_press_buttons(app_hub):
    h, c = app_hub
    assert c.post("/api/action", json={"action": "open", "target": "RUN"}).status_code == 403          # no header
    evil = {**OURS, "Origin": "https://evil.example"}
    assert c.post("/api/action", json={"action": "open", "target": "RUN"}, headers=evil).status_code == 403
    ok = c.post("/api/action", json={"action": "open", "target": "RUN"}, headers={**OURS, "Origin": h.url})
    assert ok.json() == {"ok": True} and h.cmds.get_nowait() == ("open", "RUN")
    bad = c.post("/api/action", json={"action": "open", "target": "C:\\Windows"}, headers=OURS).json()
    assert bad["ok"] is False                                             # only the ticker's own targets
    assert c.post("/api/action", json={"action": "rm -rf"}, headers=OURS).json()["ok"] is False


def test_stop_ai_only_while_away(app_hub, tmp_path, monkeypatch):
    h, c = app_hub
    llm = tmp_path / "llm"; llm.mkdir()
    monkeypatch.setattr(hub, "Path", lambda p: llm if "Cache" in str(p) else __import__("pathlib").Path(p))
    (llm / "state.json").write_text(json.dumps({"mode": "desk"}))
    assert c.post("/api/action", json={"action": "stop_ai"}, headers=OURS).json()["ok"] is False
    (llm / "state.json").write_text(json.dumps({"mode": "away"}))
    assert c.post("/api/action", json={"action": "stop_ai"}, headers=OURS).json()["ok"] is True
    assert (llm / "away-stop.flag").exists()                             # the same safe path as the Away panel


def test_the_pushed_message_has_everything_the_page_draws(app_hub):
    h, c = app_hub
    h.history.add(h.collector.snap)
    p = h.dashboard_payload()
    for key in ("snapshot", "attention", "slides", "rows", "views", "gpu", "vram_guard", "router", "wsl", "stability",
                "away", "chains", "activity", "point"):
        assert key in p, key
    assert p["views"]["theme"] == "nordic-frost" and p["point"]["gpu"] == 40.0
    json.dumps(p, default=str)
    meta = c.get("/api/meta").json()
    assert "nordic-frost" in meta["themes"] and meta["theme"] == "nordic-frost"
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/app.css").status_code == 200
    page = c.get("/").text
    for part in ("c-gpu", "c-vram", "c-ai", "c-chains", "c-awayq", "c-system", "c-disks", "c-stab", "c-svc", "c-work", "c-activity"):
        assert f'id="{part}"' in page, part


def test_history_keeps_an_hour_and_thins_for_the_charts():
    hist = feed.History(seconds=3600)
    for i in range(2000):
        assert hist.add(Snapshot(gpu_load=float(i % 100), sampled_at=1000.0 + i * 2))
    assert not hist.add(Snapshot(sampled_at=1000.0 + 1999 * 2))          # the same sample twice: ignored
    t = [p[0] for p in hist.points]
    assert t[-1] - t[0] <= 3600                                           # older points dropped
    out = hist.export(max_points=300)
    assert len(out["t"]) == 300 and out["t"][-1] == t[-1] and set(out) == {"t", *feed.SERIES}
    assert hist.latest()["t"] == t[-1]


def test_activity_feed_merges_away_and_chain_events(tmp_path):
    away = tmp_path / "away.jsonl"
    away.write_text("\n".join(json.dumps(e) for e in [
        {"time": "2026-09-26T02:57:25", "event": "away-start", "model": "sol-away-27b"},
        {"time": "2026-09-26T03:14:10", "event": "job-done", "job": "hard-evals"},
        {"time": "2026-09-26T12:24:02", "event": "job-requeued", "job": "evals", "why": "stopped by you"}]) + "\n")
    chains = tmp_path / "chains.log"
    chains.write_text("2026-09-26T12:24:05 start Code review.md (status: queued, lane away)\n"
                      "2026-09-26T12:28:05 Code review.md: run 15 cancelled: cancelled by user\n")
    items = feed.activity(away, chains)
    assert [i["text"] for i in items] == [
        "chain Code review: cancelled", "chain Code review started", "job back in the queue: evals (stopped by you)",
        "job done: hard-evals", "Away started: sol-away-27b"]
    assert items[-2]["color"] == feed.GREEN and items[0]["color"] == feed.AMBER


def test_away_info_reads_state_progress_and_queue(tmp_path):
    llm, q = tmp_path / "llm", tmp_path / "q"
    (q / "running").mkdir(parents=True); llm.mkdir()
    (llm / "state.json").write_text(json.dumps({"mode": "away", "until": "2026-09-26T12:57:23", "present": True}))
    (llm / "progress.json").write_text(json.dumps({"state": "running", "job": "evals", "done": 3, "total": 15}))
    (q / "pick.json").write_text("{}"); (q / "running" / "evals.json").write_text("{}")
    a = feed.away_info(llm, q)
    assert a["mode"] == "away" and a["present"] and a["queued"] == ["pick"] and a["running"] == ["evals"]
    assert a["progress"]["done"] == 3


def test_stream_takes_no_parameters(app_hub):
    # 09-26: `Request` imported inside a function + postponed annotations -> FastAPI saw a required query parameter
    # and every stream request got 422 (the page sat on "connecting…")
    h, c = app_hub
    route = next(r for r in c.app.routes if getattr(r, "path", "") == "/api/stream")
    assert not route.dependant.query_params and not route.dependant.body_params
