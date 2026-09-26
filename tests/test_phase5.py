"""Phase 5 (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md): critic, model auto, paused, exact checks, chain forge."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from sol_control_hud.chains import chain_daemon as cd
from sol_control_hud.chains import chain_note as cn
from sol_control_hud.chains import file_tools as ft
from sol_control_hud.chains import forge
from sol_control_hud.chains.llm import ChatResult
from sol_control_hud.chains.runner import Runner
from sol_control_hud.chains.store import Store
from sol_control_hud.chains.workflow import WorkflowError, parse


class Scripted:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def chat(self, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})
        a = self.answers.pop(0)
        return ChatResult(content=a if isinstance(a, str) else json.dumps(a), finish_reason="stop", completion_tokens=3)

    def loaded(self):
        return None

    def busy(self, model):
        return False


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "runs.sqlite")


CRITIC_WF = {"name": "c", "steps": [{"id": "s", "model": "m", "prompt": "Write a haiku about rain.",
                                     "critic": {"criteria": "exactly 3 lines; mentions rain", "model": "judge"}}]}


def test_critic_passes_a_good_answer(store):
    client = Scripted(["rain on the tin roof\nsoft\nend", {"pass": True, "problems": []}])
    rid = Runner(client, store).run(parse(CRITIC_WF), {})
    assert json.loads(store.run(rid)["outputs"])["s"].startswith("rain")
    assert [c["model"] for c in client.calls] == ["m", "judge"]
    assert client.calls[1]["schema"]["required"] == ["pass", "problems"]


def test_critic_sends_a_bad_answer_back_once(store):
    client = Scripted(["sunny day", {"pass": False, "problems": ["doesn't mention rain"]}, "rain falls\non roofs\nquietly"])
    rid = Runner(client, store).run(parse(CRITIC_WF), {})
    out = json.loads(store.run(rid)["outputs"])["s"]
    assert out == "rain falls\non roofs\nquietly" and len(client.calls) == 3     # bounded: no second critique
    assert "doesn't mention rain" in client.calls[2]["messages"][-1]["content"]
    kinds = [e["kind"] for e in store.events(rid)]
    assert "critic" in kinds and "critic_revised" in kinds


def test_critic_needs_criteria():
    with pytest.raises(WorkflowError, match="critic needs criteria"):
        parse({"name": "x", "steps": [{"id": "a", "model": "m", "prompt": "p", "critic": {"model": "j"}}]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    chains = tmp_path / "1Notebook" / "Chains"; chains.mkdir(parents=True)
    monkeypatch.setattr(cn, "CHAINS_DIR", chains)
    monkeypatch.setattr(ft, "POLICY", ft.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path / "1Notebook")],
                                                repo_roots=[str(tmp_path)]))
    monkeypatch.setattr(cd, "QUEUE_RUNNING", tmp_path / "q")
    monkeypatch.setattr(cd, "gpu_lock", None)
    return chains, Store(tmp_path / "runs.sqlite"), tmp_path


def test_chain_critic_auto_model_and_exact_checks(env):
    chains, store, tmp = env
    p = chains / "C.md"
    p.write_text("---\ncritic_model: sol-smart\n---\n## A\nmodel: auto\ncheck: 5 bullets\ncritic: covers five topics\nList.\n",
                 encoding="utf-8")
    c = cn.load_chain(p)
    step = cn.to_workflow(c)["steps"][0]
    assert step["model"] == "sol-fast" and step["escalate_to"] == "sol-smart" and step["long_model"] == "sol-long"
    assert step["checks"] == [{"path": "", "min_bullets": 5, "max_bullets": 5}]
    assert step["critic"] == {"criteria": "covers five topics", "model": "sol-smart"}
    assert cn.lane(c) == "idle"                      # the critic model decides the lane too
    assert cn.parse_check("200 words", "x") == {"path": "", "min_words": 180, "max_words": 220}


def test_paused_chain_ignores_its_schedule(env):
    chains, store, tmp = env
    p = chains / "Weekly.md"
    p.write_text("---\ntype: chain\nstatus: paused\nschedule: daily 07:00\n---\n## A\nSay a.\n", encoding="utf-8")
    clock = [datetime(2026, 9, 27, 6, 0)]
    d = cd.Daemon(store, chains_dir=chains, state_file=tmp / "s.json", status_file=tmp / "c.json", log=lambda m: None,
                  idle=lambda: 0, mode=lambda: "desk", clock=lambda: clock[0])
    d.scan(); clock[0] = datetime(2026, 9, 27, 7, 1); d.scan()
    assert d.state["pending"] == []
    cn.set_chain_status(p, "draft"); clock[0] = datetime(2026, 9, 28, 7, 1); d.scan()
    assert len(d.state["pending"]) == 1                # unpaused: the schedule counts again


GOOD_NOTE = "---\ntype: chain\nstatus: queued\nmodel: sol-fast\nschedule: weekly Fri 18:00\n---\n## Topics\nList topics.\n\n## Plan\nPlan from {{Topics}}.\n"


def test_forge_cleans_forces_draft_and_feeds_errors_back(env):
    chains, store, tmp = env
    bad = "Sure! Here it is:\n```markdown\n---\ntype: chain\n---\n## A\nUse {{Nope}}.\n```\nHope that helps."
    client = Scripted([bad, "```markdown\n" + GOOD_NOTE + "```"])
    note, tries = forge.forge("make a plan", "Plan", client.chat)
    assert tries == 2 and "I don't know {{Nope}}" in client.calls[1]["messages"][-1]["content"]
    assert "status: paused" in note and "status: queued" not in note   # never starts by itself (it has a schedule)
    assert "Sure!" not in note and "Hope" not in note
    target = forge.save_draft(note, "Plan", chains)
    again = forge.save_draft(note, "Plan", chains)
    assert target.name == "Plan.md" and again.name == "Plan (2).md"   # never overwrites


def test_forge_gives_up_after_three_bad_drafts(env):
    chains, store, tmp = env
    with pytest.raises(forge.ForgeError, match="3 tries"):
        forge.forge("x", "X", Scripted(["no note here"] * 3).chat)


def test_chain_request_in_obsidian_drafts_a_chain(env):
    chains, store, tmp = env
    req = chains / "Study list request.md"
    req.write_text("---\ntype: chain-request\nstatus: queued\ntitle: Study list\n---\n> help box\n\nEvery week make me a study list.\n",
                   encoding="utf-8")
    client = Scripted([GOOD_NOTE.replace("schedule: weekly Fri 18:00\n", "")])
    d = cd.Daemon(store, chains_dir=chains, state_file=tmp / "s.json", status_file=tmp / "c.json", client_factory=lambda: client,
                  log=lambda m: None, idle=lambda: 0, mode=lambda: "desk")
    d.tick(); d.current["thread"].join(10); d.tick()
    assert "Every week make me a study list." in client.calls[0]["messages"][-1]["content"]
    assert "help box" not in client.calls[0]["messages"][-1]["content"]
    made = chains / "Study list.md"
    assert made.exists() and "status: draft" in made.read_text(encoding="utf-8")
    meta = cd.front(req)
    assert meta["status"] == "done" and meta["made"] == "[[Study list]]"


def test_resuming_an_older_run_gets_new_builtins(env):
    chains, store, tmp = env
    p = chains / "Old.md"; p.write_text("---\ntype: chain\n---\n## A\nSay a.\n\n## B\nSay b.\n", encoding="utf-8")
    c = cn.load_chain(p)
    rid, _, _ = cn.plan_run(c, store)
    inputs = json.loads(store.run(rid)["inputs"]); inputs.pop("_new_file_name")    # as a run from before 09-25 19:40
    store.set_inputs(rid, inputs); store.set_status(rid, "interrupted")
    rid2, resuming, _ = cn.plan_run(c, store)
    assert rid2 == rid and resuming and json.loads(store.run(rid)["inputs"])["_new_file_name"] == ""
