"""Chain runner (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 3): triggers, lanes, politeness, recovery."""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sol_control_hud.chains import chain_daemon as cd
from sol_control_hud.chains import chain_note as cn
from sol_control_hud.chains import file_tools as ft
from sol_control_hud.chains.llm import ChatResult
from sol_control_hud.chains.router import ModelUnavailable
from sol_control_hud.chains.store import Store


class FakeRouter:
    def __init__(self, answers=None, loaded=None, busy=False):
        self.answers = list(answers or [])
        self.calls, self._loaded, self._busy = [], loaded, busy

    def chat(self, model, messages, **kw):
        self.calls.append(model)
        a = self.answers.pop(0) if self.answers else "ok"
        if isinstance(a, Exception):
            raise a
        return ChatResult(content=a, finish_reason="stop", completion_tokens=3)

    def loaded(self):
        return self._loaded

    def busy(self, model):
        self.busy_asked = getattr(self, "busy_asked", []) + [model]
        return self._busy


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    chains = tmp_path / "1Notebook" / "Chains"; chains.mkdir(parents=True)
    monkeypatch.setattr(cn, "CHAINS_DIR", chains)
    monkeypatch.setattr(ft, "POLICY", ft.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path / "1Notebook")],
                                                repo_roots=[str(tmp_path)]))
    monkeypatch.setattr(cd, "QUEUE_RUNNING", tmp_path / "queue" / "running")
    monkeypatch.setattr(cd, "gpu_lock", None)
    store = Store(tmp_path / "runs.sqlite")
    ctx = {"mode": "desk", "idle": 0.0, "router": FakeRouter()}
    clock = Clock(datetime(2026, 9, 27, 12, 0))   # a Sunday, noon

    def make(**kw):
        return cd.Daemon(store, chains_dir=chains, state_file=tmp_path / "state.json", status_file=tmp_path / "chains.json",
                         client_factory=lambda: ctx["router"], log=lambda m: ctx.setdefault("log", []).append(m),
                         idle=lambda: ctx["idle"], mode=lambda: ctx["mode"], clock=clock, **kw)
    return chains, store, ctx, clock, make, tmp_path


def note(chains, name, props="", body="## A\nSay a.\n"):
    p = chains / f"{name}.md"
    p.write_text(f"---\ntype: chain\n{props}---\n{body}", encoding="utf-8")
    return p


def status(p):
    return cd.front(p).get("status")


def run_to_end(d):
    d.tick()
    if d.current:
        d.current["thread"].join(10)
    d.tick()


# ---- schedules
@pytest.mark.parametrize("text, days, hm", [
    ("daily 07:00", list(range(7)), (7, 0)), ("weekly Sun 20:00", [6], (20, 0)), ("weekly Mon,Thu 9:30", [0, 3], (9, 30)),
    ("Weekly  sunday 20:00", [6], (20, 0))])
def test_parse_schedule(text, days, hm):
    s = cd.parse_schedule(text)
    assert s["days"] == days and (s["h"], s["m"]) == hm


def test_bad_schedule_is_explained():
    with pytest.raises(cd.ScheduleError, match="weekly Sun 20:00"):
        cd.parse_schedule("every sunday evening")


def test_schedule_fires_once_catches_up_and_ignores_the_past(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Digest", "status: done\nschedule: weekly Sun 20:00\n")
    d = make()
    d.scan(); assert not d.state["pending"]                           # noon: not yet
    clock.t = datetime(2026, 9, 27, 20, 0, 5); d.scan()
    assert [j["reason"] for j in d.state["pending"]] == ["schedule (weekly Sun 20:00)"] and status(p) == "queued"
    d.scan(); assert len(d.state["pending"]) == 1                     # never twice
    d.state["pending"].clear(); cn.set_chain_status(p, "done")
    clock.t = datetime(2026, 10, 5, 9, 0)                             # asleep through Sun 10-04 20:00
    d.scan()
    assert len(d.state["pending"]) == 1 and "catching up" in d.state["pending"][0]["reason"]
    late = note(chains, "Late", "status: draft\nschedule: daily 07:00\n")   # created after today's 07:00
    d.scan(); assert not any(j["path"] == str(late) for j in d.state["pending"])


def test_on_away_schedule(env, monkeypatch):
    chains, store, ctx, clock, make, _ = env
    note(chains, "Review", "status: done\nschedule: on away\nmodel: sol-coder\n")
    d = make(); d.scan()
    ctx["mode"] = "away"; monkeypatch.setattr(cd, "engine_since", lambda: clock.t.timestamp() + 1)  # Away began after it was seen
    d.scan(); assert d.state["pending"][0]["reason"] == "schedule: on away"


# ---- lanes and running
def test_queued_fast_chain_runs_and_finishes(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Quick", "status: queued\n", "## A\nSay a.\n\n## B\nUse {{A}}.\n")
    ctx["router"] = FakeRouter(["a!", "b!"])
    d = make(); run_to_end(d)
    assert status(p) == "done" and ctx["router"].calls == ["sol-fast", "sol-fast"]
    assert "b!" in (chains / "Results" / "Quick (result).md").read_text(encoding="utf-8")
    st = json.loads((env[5] / "chains.json").read_text(encoding="utf-8"))
    assert st["running"] is None and st["pending"] == []


def test_lanes_wait_for_their_moment(env):
    chains, store, ctx, clock, make, tmp = env
    note(chains, "Smart", "status: queued\nmodel: sol-smart\n")
    note(chains, "Big", "status: queued\nmodel: sol-away-120b\n")
    d = make(); d.tick()
    assert d.current is None
    st = json.loads((tmp / "chains.json").read_text(encoding="utf-8"))
    assert {p["chain"]: p["blocked"] for p in st["pending"]} == {
        "Smart": "waits until you've been idle 10 min (or Away mode)", "Big": "waits for Away mode"}
    assert st["runnable_now"] == 0
    ctx["idle"] = 700; d.tick()
    assert d.current and Path(d.current["path"]).stem == "Smart"
    d.current["thread"].join(10); d.tick()
    assert d.current is None                                          # Big still waits in Desk
    ctx["mode"] = "off"; ctx["idle"] = 0
    d.tick(); assert d.current is None
    ctx["mode"] = "away"; d.tick()
    assert d.current and Path(d.current["path"]).stem == "Big"
    d.current["thread"].join(10)


def test_nothing_starts_while_a_queue_job_runs(env):
    chains, store, ctx, clock, make, tmp = env
    note(chains, "Quick", "status: queued\n")
    (tmp / "queue" / "running").mkdir(parents=True); (tmp / "queue" / "running" / "job.json").write_text("{}")
    d = make(); d.tick()
    assert d.current is None


def test_polite_turn_rules(env):
    chains, store, ctx, clock, make, _ = env
    d = make()
    r = FakeRouter(loaded="sol-smart")
    assert "you're using sol-smart" in d._turn_blocker(r, "sol-fast")     # never unload your model
    assert not getattr(r, "busy_asked", [])                                # ...and never poll it (that keeps it loaded)
    assert d._turn_blocker(FakeRouter(loaded=None), "sol-fast") is None
    assert d._turn_blocker(FakeRouter(loaded="sol-fast"), "sol-fast") is None
    assert "answering someone else" in d._turn_blocker(FakeRouter(loaded="sol-fast", busy=True), "sol-fast")
    assert "idle" in d._turn_blocker(FakeRouter(loaded="sol-fast"), "sol-long")   # a swap waits until you're idle
    assert d._turn_blocker(FakeRouter(loaded="sol-smart"), "sol-smart") is None   # an idle-lane chain on the loaded model
    ctx["idle"] = 700
    assert d._turn_blocker(r, "sol-fast") is None
    ctx["mode"] = "off"
    assert d._turn_blocker(r, "sol-fast") == "OFF"


def test_away_chains_wait_for_queue_jobs_and_pause_when_away_ends(env):
    chains, store, ctx, clock, make, tmp = env
    d = make()
    ctx["mode"] = "away"
    (tmp / "queue").mkdir(parents=True, exist_ok=True); (tmp / "queue" / "next-job.json").write_text("{}")
    assert d.lane_open("away") == (False, "Away queue jobs go first")   # 09-26: Stop requeued the jobs, then it started
    assert d.lane_open("fast") == (True, "")
    (tmp / "queue" / "next-job.json").unlink()
    assert d.lane_open("away") == (True, "")
    assert d._turn_blocker(FakeRouter(loaded=None), "sol-coder") is None
    ctx["mode"] = "desk"; ctx["idle"] = 9999                            # Away ended: pause, don't load it at your desk
    assert d._turn_blocker(FakeRouter(loaded=None), "sol-coder") == "AWAY_ENDED"
    stop = d.gate(FakeRouter(loaded=None))
    with pytest.raises(ModelUnavailable, match="next Away"):
        stop(1, "sol-coder", lambda: False)


def test_engine_gone_means_waiting_then_retry(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Flaky", "status: queued\n", "## A\nSay a.\n\n## B\nSay b.\n")
    ctx["router"] = FakeRouter(["a!", ModelUnavailable("the engine stopped mid-answer")])
    d = make(); run_to_end(d)
    assert status(p) == "waiting" and d.current is None
    d.retry_at[str(p)] = 0
    ctx["router"] = FakeRouter(["b!"])
    run_to_end(d)
    assert status(p) == "done" and ctx["router"].calls == ["sol-fast"]    # A wasn't redone


def test_away_chain_needing_an_unserved_model_does_not_keep_the_pc_up(env):
    chains, store, ctx, clock, make, tmp = env
    note(chains, "Huge", "status: queued\nmodel: sol-away-120b\n")
    ctx["mode"] = "away"
    ctx["router"].models = lambda: {"sol-away-35b": {}, "sol-fast": {}}
    d = make(); d.tick()
    st = json.loads((tmp / "chains.json").read_text(encoding="utf-8"))
    assert d.current is None and st["runnable_now"] == 0 and "sol-away-120b" in st["pending"][0]["blocked"]


# ---- recovery / control
def test_cancel_a_chain_that_is_not_running(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "X", "status: cancel\n")
    make().tick()
    assert status(p) == "cancelled"


def test_interrupted_chain_is_resumed(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Crashed", "status: running\n")
    d = make(); d.scan()
    assert d.state["pending"][0]["reason"] == "resume after an interruption"


def test_chain_run_by_hand_is_left_alone(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Manual", "status: running\n")
    rid = store.create_run("chain: Manual", {}, source=str(p)); store.claim(rid); store.set_status(rid, "running")
    d = make(); d.scan()
    assert d.state["pending"] == []


def test_back_to_draft_leaves_the_queue(env):
    chains, store, ctx, clock, make, _ = env
    ctx["mode"] = "off"
    p = note(chains, "Q", "status: queued\n")
    d = make(); d.tick(); assert len(d.state["pending"]) == 1
    cn.set_chain_status(p, "draft"); d.tick()
    assert d.state["pending"] == []


def test_broken_chain_says_why_in_its_result(env):
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Typo", "status: queued\n", "## A\nUse {{Nope}}.\n")
    make().tick()
    assert status(p) == "needs-you"
    assert "I don't know {{Nope}}" in (chains / "Results" / "Typo (result).md").read_text(encoding="utf-8")


# ---- watched folders
def test_watch_runs_once_per_new_stable_file_with_a_limit(env):
    chains, store, ctx, clock, make, tmp = env
    inbox = tmp / "inbox"; inbox.mkdir()
    (inbox / "old.md").write_text("old")
    p = note(chains, "Summ", f"status: done\nwatch: {inbox}\\*.md\nwatch_limit: 2\n", "## A\nSummarize {{new file text}}\n")
    d = make(); d.scan(); assert d.state["pending"] == []            # existing files are the baseline
    for n in ("a", "b", "c"):
        f = inbox / f"{n}.md"; f.write_text(f"text {n}")
        os.utime(f, (time.time() - 120, time.time() - 120))
    fresh = inbox / "fresh.md"; fresh.write_text("still being written")
    clock.t = datetime.now()
    d.scan()
    jobs = d.state["pending"]
    assert len(jobs) == 2 and jobs[0]["extra"]["_new_file_text"] == "text a"   # limit 2/hour; fresh.md not yet stable
    assert all(j["new"] for j in jobs) and status(p) == "queued"
    d.state["pending"].clear(); cn.set_chain_status(p, "done"); d.scan()   # (as if both ran)
    assert d.state["pending"] == []                                   # the third waits for the hourly limit


# ---- "Run now" on a waiting chain (2026-10-07): skip the polite waits, never the hard stops
def test_skip_wait_skips_politeness_not_hard_stops(env):
    chains, store, ctx, clock, make, tmp = env
    d = make()
    busy = FakeRouter(loaded="sol-fast")
    assert "idle" in d._turn_blocker(busy, "sol-long")                       # normally: swap only when you're idle
    assert d._turn_blocker(busy, "sol-long", force=True) is None             # Run now: go
    assert d.lane_open("idle")[0] is False and d.lane_open("idle", force=True) == (True, "")
    ok, why = d.lane_open("away", force=True)
    assert not ok and "next Away session" in why                             # an Away-only model can't be skipped to
    assert d._turn_blocker(FakeRouter(loaded=None), "sol-coder", force=True) == "AWAY_ENDED"
    ctx["mode"] = "off"
    assert d._turn_blocker(busy, "sol-long", force=True) == "OFF"           # the game guard still wins
    assert d.lane_open("idle", force=True)[0] is False
    ctx["mode"] = "desk"
    (tmp / "queue" / "running").mkdir(parents=True); (tmp / "queue" / "running" / "job.json").write_text("{}")
    assert d.lane_open("idle", force=True) == (False, "an Away queue job is running")


def test_run_now_starts_a_waiting_chain_once(env):
    from sol_control_hud import control
    chains, store, ctx, clock, make, _ = env
    p = note(chains, "Monthly digest", "status: queued\nmodel: sol-long\n")
    ctx["router"] = FakeRouter(loaded=None)
    d = make()
    d.tick()
    assert d.current is None and d.state["pending"][0]["blocked"]           # idle lane: waits for 10 idle minutes
    assert control.chain_op("Monthly digest", "now") == "queued"
    assert cd.skip_wait(p)
    run_to_end(d)
    assert status(p) == "done" and ctx["router"].calls == ["sol-long"]
    assert not cd.skip_wait(p) and "skip_wait" not in p.read_text(encoding="utf-8")   # one run only
    cn.set_chain_status(p, "queued")
    d.tick()
    assert d.current is None                                                # the next run waits politely again
