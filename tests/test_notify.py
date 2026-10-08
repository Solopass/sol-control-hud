"""Notifications (phase B): what's worth one, quiet hours, no repeats, held during games."""
import json
from datetime import datetime

from sol_control_hud import hub, notify
from sol_control_hud.data.snapshot import Snapshot

S = lambda **k: Snapshot(sampled_at=1.0, **k)   # noqa: E731


def test_old_log_lines_never_pop_up(tmp_path):
    log = tmp_path / "chains.log"
    log.write_text("2026-09-25T16:54:19 Summarize a folder.md: run 9 failed: boom\n")
    tail = notify.LogTail(log)
    assert tail.new_lines() == []                                        # starts at the end of the file
    with open(log, "a") as f:
        f.write("2026-09-26T20:00:00 Weekly digest.md: run 4 succeeded\n2026-09-26T20:00:01 half a li")
    assert tail.new_lines() == ["2026-09-26T20:00:00 Weekly digest.md: run 4 succeeded"]   # whole lines only
    with open(log, "a") as f:
        f.write("ne\n")
    assert tail.new_lines() == ["2026-09-26T20:00:01 half a line"]


def test_what_is_worth_a_notice():
    got = notify.from_snapshots(S(), S(gpu_resets=1, vram_evicted=True, vram_spill_impact="slow", ai_model="sol-smart", disk_trends={"D": -12.0}))
    assert [n.kind for n in got] == ["crash", "vram", "disk"]
    assert got[0].level == "error" and "Away is blocked" in got[0].text and got[2].title == "D: filling up fast"
    assert notify.from_snapshots(S(gpu_resets=1), S(gpu_resets=1)) == []            # only when it changes
    for impact in ("unknown", "fine"):                                               # counter alone: no notice
        assert notify.from_snapshots(S(), S(vram_evicted=True, vram_spill_impact=impact)) == []
    assert notify.from_snapshots(None, S(gpu_resets=1)) == []                       # not at the first look
    chains = notify.from_chain_lines(["2026-09-26T20:00:00 Weekly digest.md: run 3 failed: model gone",
                                      "2026-09-26T20:01:00 Summarize a folder.md: run 9 succeeded",
                                      "2026-09-26T20:02:00 Code review.md: run 15 cancelled: cancelled by user"])
    assert [(n.kind, n.title, n.text) for n in chains] == [
        ("chain_failed", "Chain failed: Weekly digest", "model gone"),
        ("chain_done", "Chain finished: Summarize a folder", "result in 1Notebook\\Chains\\Results")]
    away = notify.from_away_lines([json.dumps({"event": "away-end", "reason": "queue done", "minutes": 130}),
                                   json.dumps({"event": "job-done", "job": "ask-Pasta ideas"})],
                                  {"done": 3, "failed": ["x"], "chains": ["Code review (finished)"]})
    assert away[0].title == "Away finished (queue done)" and away[0].text == "3 jobs done, 1 failed, Code review (finished)"
    assert away[1].kind == "answer" and "Pasta ideas" in away[1].text


def make(tmp_path, now, **settings):
    for name in ("away.jsonl", "chains.log"):
        (tmp_path / name).write_text("")
    clock = {"t": now}
    n = notify.Notifier({**notify.DEFAULTS, **settings}, tmp_path / "away.jsonl", tmp_path / "chains.log",
                        clock=lambda: clock["t"], session_fn=lambda: {"done": 2, "failed": [], "chains": []})
    return n, clock


def at(h, m=0):
    return datetime(2026, 9, 26, h, m).timestamp()


def test_quiet_hours_let_only_crashes_through(tmp_path):
    n, clock = make(tmp_path, at(2))
    got = n.check(S(), S(gpu_resets=1, vram_evicted=True, vram_spill_impact="slow"), busy=False)
    assert [x.kind for x in got] == ["crash"]
    assert notify.in_quiet_hours("23:00-08:00", datetime(2026, 9, 26, 23, 30))
    assert not notify.in_quiet_hours("23:00-08:00", datetime(2026, 9, 26, 12, 0))


def test_no_repeats_and_switches(tmp_path):
    n, clock = make(tmp_path, at(14))
    assert len(n.check(S(), S(vram_evicted=True, vram_spill_impact="slow"), busy=False)) == 1
    assert n.check(S(), S(vram_evicted=True, vram_spill_impact="slow"), busy=False) == []            # same thing within 10 min
    clock["t"] += 601
    assert len(n.check(S(), S(vram_evicted=True, vram_spill_impact="slow"), busy=False)) == 1
    off, _ = make(tmp_path, at(14), kinds={**notify.DEFAULTS["kinds"], "vram": False})
    assert off.check(S(), S(vram_evicted=True, vram_spill_impact="slow"), busy=False) == []          # that kind switched off
    muted, _ = make(tmp_path, at(14), enabled=False)
    assert muted.check(S(), S(gpu_resets=1), busy=False) == []             # all off


def test_held_during_a_game_then_one_summary(tmp_path):
    n, clock = make(tmp_path, at(15))
    with open(tmp_path / "away.jsonl", "a") as f:
        f.write(json.dumps({"event": "away-end", "reason": "queue done"}) + "\n")
    assert n.check(S(), S(vram_evicted=True, vram_spill_impact="slow"), busy=True) == []              # nothing while you play
    out = n.check(S(), S(), busy=False)
    assert len(out) == 1 and out[0].kind == "summary" and out[0].title == "While you were busy: 2 things"
    assert "Away finished (queue done)" in out[0].text and out[0].level == "warning"


def test_hub_switch_and_test_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    shown = []
    h.tray = type("T", (), {"notify": lambda self, title, text, level: shown.append((title, level)) or True})()
    assert h.do_action("notify", "test")["ok"] and shown == [("SOL Control HUD", "info")]
    assert h.do_action("notify", "off")["notify"] is False and h.views()["notify"] is False
    assert json.loads((tmp_path / "hub-settings.json").read_text())["notify"]["enabled"] is False
    assert h.do_action("notify", "on")["notify"] is True


def test_a_stuck_media_job_is_told_once():
    seen = set()
    tiles = [{"name": "media-api", "title": "Media API", "stuck": [
        {"id": "j1", "title": "youtube.com · watch?v=9", "status": "processing", "why": "stuck"},
        {"id": "j2", "title": "long one", "status": "processing", "why": "stalled"}]}]      # stalled: shown, not told
    (n,) = notify.from_media(tiles, seen)
    assert n.kind == "media_stuck" and "Media API" in n.title and "watch?v=9" in n.text and n.key == "media:media-api:j1"
    assert notify.from_media(tiles, seen) == []                                      # same job: not again
    assert notify.from_media([], seen) == [] and notify.from_media(None, seen) == []


def test_media_notices_go_through_the_same_switches(tmp_path):
    s = {"enabled": True, "kinds": {"media_stuck": False}, "quiet": ""}
    n = notify.Notifier(s, tmp_path / "a.jsonl", tmp_path / "c.log")
    extra = [notify.Notice("media_stuck", "x", "y", "warning", "media:m:1")]
    assert n.check(S(), S(), busy=False, extra=extra) == []                         # that kind switched off
    s["kinds"]["media_stuck"] = True
    assert [x.kind for x in n.check(S(), S(), busy=False, extra=extra)] == ["media_stuck"]
