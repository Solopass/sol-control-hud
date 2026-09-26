"""Away screen (OBVLT plans/AWAY_SCREEN_PLAN.md): the way back, time left, what the screen says, chain progress."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sol_control_hud.away import screen as a
from sol_control_hud.chains import chain_note as cn
from sol_control_hud.chains.chain_daemon import chain_progress


def test_enter_or_five_quick_spaces_come_back():
    k = a.ExitKeys()
    assert k.press("Return", 0.0)
    k = a.ExitKeys()
    assert not any(k.press("space", t) for t in (0.0, 0.5, 1.0, 1.5))       # 4 taps: not yet
    assert k.press("space", 2.0)                                              # the 5th within 3 s
    k = a.ExitKeys()
    assert not any(k.press("space", t) for t in (0, 1, 2, 3, 4, 5, 6, 7))     # 1 tap a second: never 5 within 3 s
    k = a.ExitKeys()
    assert not any(k.press(key, 0.1 * i) for i, key in enumerate(["a", "Escape", "Super_L", "Tab", "Alt_L", "x"]))


def test_time_left_only_after_two_minutes_and_restarts_per_task():
    e = a.EtaTracker()
    assert e.update("job1", 0, 45, 0.0) is None
    assert e.update("job1", 5, 45, 60.0) is None                  # too early to guess
    assert e.update("job1", 10, 45, 300.0) == pytest.approx(35 * 300 / 10)
    assert e.update("job2", 1, 10, 310.0) is None                  # a new task starts over
    assert a.minutes_text(None, datetime(2026, 9, 25, 21, 0)) == ""
    assert a.minutes_text(18 * 60, datetime(2026, 9, 25, 21, 52)) == "about 18 min left · done around 22:10"


NOW = datetime(2026, 9, 25, 21, 0).timestamp()
ISO = lambda secs_ago: datetime.fromtimestamp(NOW - secs_ago).isoformat(timespec="seconds")


def view(state, progress=None, chains=None, events=(), countdown=None, returning=False, monkeypatch=None, tmp_path=None):
    return a.build_view(state, progress, chains, list(events), countdown, returning, a.EtaTracker(), NOW)


@pytest.fixture(autouse=True)
def empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "QUEUE_DIR", tmp_path)


def test_working_on_a_queue_job_with_progress():
    p = {"state": "running", "job": "first-run-evals", "started": ISO(600), "updated": ISO(3), "done": 31, "total": 45,
         "detail": "gpt-oss-120b: code test, run 2 of 3"}
    v = view({"mode": "away"}, p, events=[{"event": "job-done", "job": "warmup", "minutes": 2}])
    assert v.phase == "working" and v.fraction == pytest.approx(31 / 45)
    assert v.line == "first-run-evals — gpt-oss-120b: code test, run 2 of 3"
    assert "job 2 of 2" in v.jobs and "warmup ✓" in v.jobs
    assert v.footer.startswith("Press Enter")


def test_job_without_progress_shows_elapsed_time():
    p = {"state": "running", "job": "backup", "started": ISO(420), "updated": ISO(2), "done": None, "total": None}
    v = view({"mode": "away"}, p)
    assert v.line == "backup (running 7 min)" and v.fraction is None


def test_working_on_a_chain():
    chains = {"running": {"chain": "Code review", "run": 9, "step": 2, "steps": 3, "step_name": "Per file", "item": 57,
                          "items": 339, "done": 116, "total": 300}}
    v = view({"mode": "away"}, None, chains)
    assert v.line == "Chain “Code review” — step 2 of 3: Per file, item 57 of 339"
    assert v.fraction == pytest.approx(116 / 300)


def test_done_stopped_countdown_and_returning():
    done = view({"mode": "desk", "reason": "queue done"}, events=[{"event": "job-done", "job": "evals", "minutes": 78}])
    assert done.phase == "done" and done.color == a.GREEN and done.summary == ["✓ evals (78 min)"]
    ram = view({"mode": "desk", "reason": "RAM guard"})
    assert ram.phase == "stopped" and "memory" in ram.line and ram.color == a.RED
    game = view({"mode": "off", "reason": "game: game Slay the Spire 2"})
    assert game.phase == "stopped" and "game" in game.line
    cd = view({"mode": "desk", "reason": "queue done"}, countdown={"at": ISO(-42)})
    assert cd.phase == "countdown" and "Sleeping in 42 s" in cd.footer
    back = view({"mode": "away"}, returning=True)
    assert back.phase == "returning" and back.footer == ""


def test_chain_progress_counts_steps_and_loop_items(tmp_path):
    p = tmp_path / "C.md"
    p.write_text("## Collect\nSay a.\n\n## Per file\nfor each line in {{Collect}}:\nDo {{line}}.\n\n## Sum\nSum {{Per file}}.\n",
                 encoding="utf-8")
    c = cn.load_chain(p)
    assert chain_progress(c, {}) == {"steps": 3, "step": 1, "done": 0, "total": 300, "step_name": "Collect"}
    saved = {"collect": {-1: "x"}, "per_file__items": {-1: ["a", "b", "c", "d"]}, "per_file": {0: "A", 1: "B"}}
    got = chain_progress(c, saved)
    assert (got["step"], got["step_name"], got["item"], got["items"], got["done"]) == (2, "Per file", 3, 4, 150)
    finished = {s.id: {-1: "x"} for s in c.steps}
    assert chain_progress(c, finished)["done"] == 300


def test_the_screen_closes_itself_when_a_game_starts():
    assert a.closes_by_itself({"mode": "off", "reason": "game: game cs2"}, returning=False)
    assert not a.closes_by_itself({"mode": "off", "reason": "game: game cs2"}, returning=True)   # already closing
    assert not a.closes_by_itself({"mode": "desk", "reason": "queue done"}, returning=False)     # 'All done' waits for Enter
    assert not a.closes_by_itself({"mode": "away"}, returning=False)


def test_the_corner_panel_says_what_is_still_running():
    p = {"state": "running", "job": "hard-evals", "started": ISO(600), "updated": ISO(3), "done": 5, "total": 15,
         "detail": "gpt-oss-120b: parser test, run 2 of 3"}
    line, sub = a.panel_text(view({"mode": "away"}, p))
    assert line == "AI still working — hard-evals — gpt-oss-120b: parser test, run 2 of 3" and sub.startswith("33 %")
    done = a.panel_text(view({"mode": "desk", "reason": "queue done"}, events=[{"event": "job-done", "job": "x", "minutes": 3}]))
    assert done == ("AI work all done ✓", "✓ x (3 min)")
    stopped = view({"mode": "desk", "reason": "stopped by you"})
    assert stopped.phase == "stopped" and "stays queued" in a.panel_text(stopped)[0]
