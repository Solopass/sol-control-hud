"""Phase C: ask it overnight (queue jobs, answers) and chain controls; only names from the page, never paths."""
import json
from datetime import datetime

import pytest

from sol_control_hud import control
from sol_control_hud.chains import chain_note, file_tools


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    q, ans, chains = tmp_path / "Queue", tmp_path / "Answers", tmp_path / "Chains"
    q.mkdir(); (q / "running").mkdir(); ans.mkdir(); (chains / "Results").mkdir(parents=True)
    monkeypatch.setattr(control, "QUEUE_DIR", q)
    monkeypatch.setattr(control, "ANSWERS_DIR", ans)
    monkeypatch.setattr(chain_note, "CHAINS_DIR", chains)
    cfg = tmp_path / "local-ai.json"
    cfg.write_text(json.dumps({"away": {"maxHours": 6, "models": {
        "sol-away-27b": {"file": "a.gguf", "quality": "15/15", "tokps": 62}, "sol-away-120b": {"file": "b.gguf"}}}}))
    monkeypatch.setattr(control, "LOCAL_AI", cfg)
    return q, ans, chains


def test_models_to_choose_from(dirs):
    ids = [m["id"] for m in control.away_models()]
    assert ids == ["sol-away", "sol-away-27b", "sol-away-120b"]
    assert control.away_models()[1]["label"] == "sol-away-27b (quality 15/15, 62 tok/s)"


def test_a_question_becomes_an_away_queue_job(dirs, tmp_path, monkeypatch):
    q, ans, _ = dirs
    note = tmp_path / "notes.md"; note.write_text("the notes")
    monkeypatch.setattr(file_tools, "POLICY", file_tools.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path)]))
    r = control.queue_ask("Pasta / ideas?", "Give me 5 dinners", [str(note)], "sol-away-27b", now=datetime(2026, 9, 26, 21, 0))
    assert r["job"] == "ask-Pasta  ideas"                                  # no path characters survive
    job = json.loads((q / f"{r['job']}.json").read_text())
    assert job["type"] == "chat" and job["model"] == "sol-away-27b" and job["maxTokens"] == control.ASK_MAX_TOKENS
    assert job["outFile"] == str(ans / "2026-09-26 Pasta  ideas.md") and "the notes" in job["prompt"]
    assert job["ask"]["question"] == "Give me 5 dinners" and job["ask"]["files"] == [str(note)]
    again = control.queue_ask("Pasta / ideas?", "again", model="sol-away", now=datetime(2026, 9, 26, 21, 0))
    assert again["job"] == "ask-Pasta  ideas (2)"                          # never overwrites a waiting one
    assert [a["title"] for a in control.list_asks()] == ["Pasta  ideas", "Pasta  ideas (2)"]


def test_what_a_question_may_not_do(dirs):
    with pytest.raises(control.ControlError, match="question"):
        control.queue_ask("t", "   ")
    with pytest.raises(control.ControlError, match="unknown model"):
        control.queue_ask("t", "q", model="gpt-5")
    with pytest.raises(control.ControlError, match="can't use"):
        control.queue_ask("t", "q", files=[r"C:\Windows\System32\drivers\etc\hosts"])      # outside the allowed folders
    with pytest.raises(control.ControlError, match="too long"):
        control.queue_ask("t", "x" * 70000)


def test_remove_only_waiting_questions(dirs):
    q, _, _ = dirs
    control.queue_ask("One", "q")
    (q / "running" / "ask-Two.json").write_text(json.dumps({"type": "chat", "ask": {"title": "Two"}}))
    with pytest.raises(control.ControlError, match="running"):
        control.remove_ask("ask-Two")
    with pytest.raises(control.ControlError, match="not a question"):
        control.remove_ask("..\\..\\secrets")
    control.remove_ask("ask-One")
    assert not (q / "ask-One.json").exists() and (q / "removed" / "ask-One.json").exists()   # moved, not deleted


def test_answers_list_and_read(dirs):
    _, ans, _ = dirs
    (ans / "2026-09-26 Pasta.md").write_text("---\nmodel: sol-away-27b\n---\n\n# Pasta\n\nMake **carbonara**.")
    a = control.list_answers()
    assert a[0]["title"] == "2026-09-26 Pasta" and a[0]["model"] == "sol-away-27b" and "carbonara" in a[0]["preview"]
    assert "carbonara" in control.read_answer("2026-09-26 Pasta.md")
    with pytest.raises(control.ControlError):
        control.read_answer("..\\..\\OBVLT\\CLAUDE.md")


def test_chain_controls_change_only_the_status(dirs):
    _, _, chains = dirs
    p = chains / "Weekly digest.md"
    p.write_text("---\nstatus: done\nschedule: weekly Sun 20:00\nmodel: sol-smart\n---\n\n## Step\nSay hi.\n")
    (chains / "Idea request.md").write_text("---\ntype: chain-request\nstatus: queued\n---\nx\n")
    (chains / "Results" / "Weekly digest (result).md").write_text("# Result\nok")
    listed = control.list_chains()
    assert [c["name"] for c in listed] == ["Weekly digest"] and listed[0]["result_time"]      # requests aren't chains
    assert control.chain_op("Weekly digest", "pause") == "paused"
    assert "status: paused" in p.read_text() and "schedule: weekly Sun 20:00" in p.read_text()
    assert control.chain_op("Weekly digest", "resume") == "done"
    assert control.chain_op("Weekly digest", "run") == "queued"
    assert "ok" in control.chain_result("Weekly digest")
    with pytest.raises(control.ControlError):
        control.chain_op("Nope", "run")
    with pytest.raises(control.ControlError):
        control.chain_op("Weekly digest", "delete")


def test_through_the_dashboard(dirs, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sol_control_hud import hub
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    h = hub.Hub(dict(hub.DEFAULTS), True, False)
    h.collector = type("C", (), {"_sampler": type("S", (), {"latest": {}})(), "get_snapshot": lambda self: None})()
    h.guard = hub.LazyGuard(h.collector._sampler)
    c = TestClient(h.build_app())
    ours = {"X-SOL-Control": "1"}
    assert c.post("/api/action", json={"action": "ask", "question": "hi"}).status_code == 403
    r = c.post("/api/action", json={"action": "ask", "title": "Hello", "question": "hi", "model": "sol-away"}, headers=ours).json()
    assert r["ok"] and r["job"] == "ask-Hello"
    state = c.get("/api/control").json()
    assert [a["title"] for a in state["asks"]] == ["Hello"] and state["models"][0]["id"] == "sol-away"
    assert c.post("/api/action", json={"action": "ask_remove", "target": "ask-Hello"}, headers=ours).json()["ok"]
    assert c.get("/api/answer", params={"file": "..\\x.md"}).json()["ok"] is False


def test_edit_a_chains_schedule(dirs):
    """The Chains card's ⏱ editor: only `schedule:` changes, checked by the runner's own parser, canonical form."""
    _, _, chains = dirs
    p = chains / "Code review.md"
    original = "---\r\ntype: chain\r\nstatus: done\r\nschedule: daily 01:00\r\nmodel: sol-fast\r\n---\r\n\r\n## Step\r\nHi.\r\n"
    p.write_bytes(original.encode())
    assert control.set_schedule("Code review", "weekly mon, thu 9:30") == "weekly Mon,Thu 09:30"
    text = p.read_bytes().decode()
    assert text == original.replace("daily 01:00", "weekly Mon,Thu 09:30")            # CRLF and everything else kept
    assert control.set_schedule("Code review", "weekly sun,mon,tue,wed,thu,fri,sat 7:05") == "daily 07:05"
    assert control.set_schedule("Code review", "on away") == "on away"
    assert control.set_schedule("Code review", "") == ""
    assert p.read_bytes().decode() == original.replace("schedule: daily 01:00\r\n", "")   # the line is gone, nothing else
    assert control.list_chains()[0]["schedule"] == ""
    for bad in ("hourly", "daily 25:00", "weekly Funday 10:00"):
        with pytest.raises(control.ControlError):
            control.set_schedule("Code review", bad)
    with pytest.raises(control.ControlError):
        control.set_schedule("Nope", "daily 07:00")


class _Reply:
    def __init__(self, content, model="sol-fast"):
        self.content, self.finish_reason, self.model = content, "stop", model


class _Client:
    def __init__(self, content="Five dinners: ..."):
        self.content, self.calls = content, []

    def chat(self, model, messages, *, max_tokens):
        self.calls.append((model, messages, max_tokens))
        return _Reply(self.content, model)


def _no_lock(what, timeout=None):
    import contextlib
    return contextlib.nullcontext()


@pytest.fixture
def desk(monkeypatch):
    from sol_control_hud.chains import llm
    monkeypatch.setattr(llm, "load_registry", lambda: {"sol-fast": {"num_ctx": 8192}, "sol-smart": {"num_ctx": 32768}})
    control._now_jobs.clear()
    yield
    control._now_jobs.clear()


def test_ask_now_answers_right_away_in_the_overnight_format(dirs, desk):
    _, ans, _ = dirs
    client = _Client()
    r = control.ask_now("Pasta?", "Give me 5 dinners\nquick ones", model="sol-fast", now=datetime(2026, 10, 7, 12, 0),
                        client=client, mode="desk", lock=_no_lock, background=False)
    assert r["answer"] == str(ans / "2026-10-07 Pasta.md") and control.list_asks() == []   # done: off the Waiting list
    model, messages, max_tokens = client.calls[0]
    assert model == "sol-fast" and max_tokens == 4096 and messages[0]["content"].startswith("Give me 5 dinners")
    text = (ans / "2026-10-07 Pasta.md").read_text(encoding="utf-8")
    assert "source: SOL Control HUD (ask now)" in text and "model: sol-fast" in text and "asked: 2026-10-07T12:00:00" in text
    assert "# Pasta\n\n> Give me 5 dinners\n> quick ones\n\n---\n\nFive dinners: ...\n" in text
    a = control.list_answers()[0]
    assert a["title"].endswith("Pasta") and a["model"] == "sol-fast"
    again = control.ask_now("Pasta?", "more", model="sol-fast", now=datetime(2026, 10, 7, 12, 0),
                            client=client, mode="desk", lock=_no_lock, background=False)
    assert again["answer"].endswith("2026-10-07 Pasta (2).md")                 # never overwrites an answer


def test_ask_now_refusals_and_failures(dirs, desk):
    with pytest.raises(control.ControlError, match="Desk model"):
        control.ask_now("t", "q", model="sol-away", mode="desk")
    with pytest.raises(control.ControlError, match="off"):
        control.ask_now("t", "q", model="sol-fast", mode="off")
    with pytest.raises(control.ControlError, match="Away mode"):
        control.ask_now("t", "q", model="sol-fast", mode="away")
    with pytest.raises(control.ControlError, match="more context"):
        control.ask_now("t", "x" * 20000, model="sol-fast", mode="desk")     # fits sol-smart, not sol-fast's 8k
    control.ask_now("Empty", "q", model="sol-fast", mode="desk", client=_Client(""), lock=_no_lock, background=False)
    [a] = control.list_asks()
    assert a["state"].startswith("failed: empty answer") and a["now"]
    with pytest.raises(control.ControlError, match="no such"):
        control.remove_ask("now-nothing")
    control.remove_ask(a["job"])                                              # ✕ clears a failed one
    assert control.list_asks() == []


def test_ask_now_through_the_dashboard(dirs, desk, monkeypatch):
    from sol_control_hud.chains import router
    monkeypatch.setattr(router, "engine_mode", lambda: "away")
    from sol_control_hud.hub import Hub
    r = Hub.do_action(Hub.__new__(Hub), "ask", "", {"question": "q", "model": "sol-fast", "now": True})
    assert not r["ok"] and "Away mode" in r["why"]
