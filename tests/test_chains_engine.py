"""Chains engine (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 1): resume, for_each, budget, waiting models,
cross-process ownership/cancel/GPU lock, router client, file-tool guards, validator."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from sol_control_hud.chains import file_tools as ft
from sol_control_hud.chains.gpulock import LockTimeout, gpu_lock, holder
from sol_control_hud.chains.llm import ChatResult, LLMError
from sol_control_hud.chains.router import ModelUnavailable, RouterClient
from sol_control_hud.chains.runner import Runner, compact_text, with_today
from sol_control_hud.chains.store import Store
from sol_control_hud.chains.workflow import WorkflowError, parse


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "runs.sqlite")


class Scripted:
    """Answers by model; an Exception in the script is raised. Records every call with its kwargs."""

    def __init__(self, script, fits=None):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self._fits = fits

    def chat(self, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})
        item = self.script[model].pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ChatResult):
            return item
        return ChatResult(content=item, finish_reason="stop", completion_tokens=5)


class Budgeted(Scripted):
    def fits(self, model, messages, max_tokens):
        return self._fits(model, messages, max_tokens)


THREE = {"name": "three", "inputs": ["topic"], "system": "You are terse.", "steps": [
    {"id": "a", "model": "m", "prompt": "A about {{ inputs.topic }}"},
    {"id": "b", "model": "m", "prompt": "B from {{ steps.a.output }}", "reasoning": "high", "temperature": 0.7},
    {"id": "c", "model": "m", "prompt": "C from {{ steps.b.output }}", "system": "You are a critic."},
]}


def test_system_prompt_reasoning_and_temperature_reach_the_client(store):
    client = Scripted({"m": ["a1", "b1", "c1"]})
    rid = Runner(client, store).run(parse(THREE), {"topic": "x"})
    assert store.run(rid)["status"] == "succeeded"
    today = with_today(None)
    assert client.calls[0]["messages"][0] == {"role": "system", "content": f"You are terse.\n\n{today}"}
    assert client.calls[1]["reasoning"] == "high" and client.calls[1]["temperature"] == 0.7
    assert "reasoning" not in client.calls[0]                      # default: the model's own setting
    assert client.calls[2]["messages"][0]["content"] == f"You are a critic.\n\n{today}"  # a step's system replaces the default


def test_every_call_is_told_todays_date(store):
    from datetime import datetime
    assert with_today(None, datetime(2026, 10, 28)) == "Today is Wednesday 2026-10-28."
    assert with_today("Be brief.", datetime(2026, 10, 28)) == "Be brief.\n\nToday is Wednesday 2026-10-28."
    client = Scripted({"m": ["a1"]})                               # a workflow with no system prompt at all
    Runner(client, store).run(parse({"name": "one", "steps": [{"id": "a", "model": "m", "prompt": "A"}]}), {})
    assert client.calls[0]["messages"][0] == {"role": "system", "content": with_today(None)}


def test_json_check_rejects_bad_output_then_accepts_a_retry(store):
    wf = parse({"name": "j", "steps": [{"id": "a", "model": "m", "prompt": "list", "retries": 1,
                                        "schema": {"type": "array", "items": {"type": "string"}}}]})
    client = Scripted({"m": ["- not json", '["x", "y"]']})
    rid = Runner(client, store).run(wf, {})
    assert store.run(rid)["status"] == "succeeded" and len(client.calls) == 2


def test_each_step_is_saved_and_resume_skips_finished_steps(store):
    wf = parse(THREE)
    client = Scripted({"m": ["a1", LLMError("engine down")]})
    rid = Runner(client, store).run(wf, {"topic": "x"})
    assert store.run(rid)["status"] == "failed"
    assert store.saved_outputs(rid)["a"][-1] == "a1"               # saved before the run ended
    client2 = Scripted({"m": ["b2", "c2"]})
    Runner(client2, store).resume(rid, wf)
    run = store.run(rid)
    assert run["status"] == "succeeded" and json.loads(run["outputs"]) == {"a": "a1", "b": "b2", "c": "c2"}
    assert len(client2.calls) == 2 and "a1" in client2.calls[0]["messages"][-1]["content"]


LOOP = {"name": "loop", "inputs": ["items"], "steps": [
    {"id": "each", "model": "m", "for_each": {"items": "{{ inputs.items }}", "as": "it"}, "prompt": "Do {{ it }}"},
    {"id": "sum", "model": "m", "prompt": "Combine {{ steps.each.output }}"},
]}


def test_for_each_saves_items_and_resumes_mid_loop(store):
    wf = parse(LOOP)
    client = Scripted({"m": ["r1", "r2", LLMError("crash")]})
    rid = Runner(client, store).run(wf, {"items": ["x", "y", "z"]})
    assert set(store.saved_outputs(rid)["each"]) == {0, 1}
    client2 = Scripted({"m": ["r3", "total"]})
    Runner(client2, store).resume(rid, wf)
    out = json.loads(store.run(rid)["outputs"])
    assert out["each"] == ["r1", "r2", "r3"] and out["sum"] == "total"
    assert client2.calls[0]["messages"][-1]["content"] == "Do z"   # only the missing item ran again


def test_continue_on_error_keeps_going(store):
    doc = json.loads(json.dumps(LOOP)); doc["steps"][0]["continue_on_error"] = True
    doc["steps"][0]["checks"] = [{"path": "", "min_length": 3}]; doc["steps"][0]["retries"] = 0
    client = Scripted({"m": ["ok1", "no", "ok3", "total"]})
    rid = Runner(client, store).run(parse(doc), {"items": [1, 2, 3]})
    out = json.loads(store.run(rid)["outputs"])
    assert out["each"][0] == "ok1" and "error" in out["each"][1] and out["each"][2] == "ok3"


def test_file_items_get_their_text_lazily(store):
    doc = {"name": "files", "inputs": ["files"], "steps": [
        {"id": "s", "model": "m", "for_each": {"items": "{{ inputs.files }}", "as": "file"}, "prompt": "{{ file.name }}: {{ file.text }}"}]}
    client = Scripted({"m": ["ok"]})
    Runner(client, store, item_loader=lambda item: f"TEXT OF {item['name']}").run(parse(doc), {"files": [{"name": "a.md", "path": "p"}]})
    assert client.calls[0]["messages"][-1]["content"] == "a.md: TEXT OF a.md"


def test_unavailable_model_waits_then_resumes(store):
    wf = parse(THREE)
    client = Scripted({"m": ["a1", ModelUnavailable("model 'm' isn't served in desk mode")]})
    rid = Runner(client, store).run(wf, {"topic": "x"})
    run = store.run(rid)
    assert run["status"] == "waiting" and "desk mode" in run["error"]
    Runner(Scripted({"m": ["b", "c"]}), store).resume(rid, wf)
    assert store.run(rid)["status"] == "succeeded"


def test_budget_moves_to_long_model_or_stops_clearly(store):
    doc = {"name": "big", "steps": [{"id": "s", "model": "small", "long_model": "big", "prompt": "huge"}]}
    fits = lambda model, messages, mt: (model == "big", 40000, 32768 if model == "small" else 65536)
    client = Budgeted({"big": ["done"]}, fits=fits)
    rid = Runner(client, store).run(parse(doc), {})
    assert store.run(rid)["status"] == "succeeded" and client.calls[0]["model"] == "big"
    doc["steps"][0].pop("long_model")
    client = Budgeted({}, fits=fits)
    rid = Runner(client, store).run(parse(doc), {})
    run = store.run(rid)
    assert run["status"] == "needs_user" and "40000" in run["error"] and "for_each" in run["error"] and not client.calls


def test_cancel_from_another_process_stops_before_next_step(store, tmp_path):
    wf = parse(THREE)
    other = Store(tmp_path / "runs.sqlite")  # a second connection, like the `cancel` command

    class CancelDuringA(Scripted):
        def chat(self, model, messages, **kw):
            other.request_cancel(1)
            return super().chat(model, messages, **kw)

    rid = Runner(CancelDuringA({"m": ["a1"]}), store).run(wf, {"topic": "x"})
    assert rid == 1 and store.run(rid)["status"] == "cancelled" and "a" in store.saved_outputs(rid)


def test_recovery_leaves_live_runs_alone(tmp_path):
    s = Store(tmp_path / "r.sqlite")
    live, dead = s.create_run("w", {}), s.create_run("w", {})
    for r in (live, dead):
        s.claim(r); s.set_status(r, "running")
    s._db.execute("UPDATE runs SET owner_pid=999999 WHERE id=?", (dead,)); s._db.commit()
    assert Store(tmp_path / "r.sqlite").recover_after_restart() == 1
    assert s.run(live)["status"] == "running" and s.run(dead)["status"] == "interrupted"


def test_store_prune_and_capping(tmp_path):
    s = Store(tmp_path / "prune.sqlite")
    # Giant output capping
    giant = "x" * 2_500_000
    s.save_output(1, "step1", giant)
    saved = s.saved_outputs(1)["step1"][-1]
    assert "[truncated" in saved and len(saved) < 2_100_000

    # Prune
    now = time.time()
    for i in range(10):
        rid = s.create_run(f"wf_{i}", {})
        s.set_status(rid, "succeeded")
        if i < 5:
            s._db.execute("UPDATE runs SET created_at=? WHERE id=?", (now - (30 * 86400), rid))
    s._db.commit()
    pruned = s.prune(max_age_days=14, keep_latest=3)
    assert pruned == 5
    s.vacuum()



# ---- workflow validation
@pytest.mark.parametrize("step, msg", [
    ({"id": "a", "model": "m", "prompt": "p", "reasoning": "max"}, "reasoning must be"),
    ({"id": "a", "model": "m", "prompt": "p", "tempreature": 1}, "unknown setting"),
    ({"id": "a", "model": "m", "prompt": "p", "for_each": {"items": "x {{ inputs.i }}"}}, "exactly one reference"),
    ({"id": "a", "model": "m", "prompt": "{{ it }}", "for_each": {"items": "{{ inputs.i }}", "as": "steps"}}, "simple name"),
    ({"id": "a", "model": "m", "prompt": "{{ file.text }}"}, "unknown reference"),
])
def test_chain_options_are_validated(step, msg):
    with pytest.raises(WorkflowError, match=msg):
        parse({"name": "x", "inputs": ["i"], "steps": [step]})


def test_bare_yaml_off_means_reasoning_off():
    import yaml
    wf = parse(yaml.safe_load("name: x\nsteps:\n  - {id: a, model: m, prompt: p, reasoning: off}\n"))
    assert wf.steps[0].reasoning == "off"


# ---- GPU lock across processes
def test_gpu_lock_is_shared_across_processes_and_freed_when_the_holder_dies(tmp_path):
    lock = tmp_path / "gpu.lock"
    code = textwrap.dedent(f"""
        import time, sys
        sys.path.insert(0, {str(Path(__file__).parents[1])!r})
        from pathlib import Path
        from sol_control_hud.chains.gpulock import gpu_lock
        with gpu_lock("other process", path=Path({str(lock)!r})):
            print("held", flush=True); time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        assert holder(lock)["what"] == "other process"
        with pytest.raises(LockTimeout):
            with gpu_lock("me", path=lock, timeout=1.5, poll=0.2):
                pass
    finally:
        p.kill(); p.wait()
    t = time.monotonic()
    with gpu_lock("me", path=lock, timeout=5, poll=0.1):   # the OS dropped the dead holder's lock
        assert holder(lock)["what"] == "me"
    assert time.monotonic() - t < 3


# ---- router client (no real router: httpx mock transport)
def _router(handler, mode="desk", tmp_path=None):
    return RouterClient(registry={"sol-fast": {"num_ctx": 32768, "think": None}, "sol-smart": {"num_ctx": 65536, "think": "medium"}},
                        http=httpx.Client(transport=httpx.MockTransport(handler)))


MODELS = {"data": [{"id": "sol-fast", "aliases": ["sol-vision"], "status": {"value": "loaded"}},
                   {"id": "sol-smart", "aliases": [], "status": {"value": "unloaded"}}]}


def test_router_client_streams_and_maps_reasoning(monkeypatch):
    monkeypatch.setattr("sol_control_hud.chains.router.engine_mode", lambda: "desk")
    seen = {}

    def handler(req):
        if req.url.path == "/models":
            return httpx.Response(200, json=MODELS)
        seen.update(json.loads(req.content))
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in [
            {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
            {"choices": [{"delta": {"content": "It is "}}]},
            {"choices": [{"delta": {"content": "blue."}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}}]) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    c = _router(handler)
    r = c.chat("sol-fast", [{"role": "user", "content": "sky?"}], reasoning="off", max_tokens=100)
    assert (r.content, r.thinking, r.finish_reason, r.completion_tokens) == ("It is blue.", "hmm", "stop", 7)
    assert seen["reasoning_effort"] == "none" and seen["stream"] is True
    assert c.reasoning_effort("sol-smart", "off") == "low"   # gpt-oss can't switch thinking off
    with pytest.raises(ModelUnavailable, match="Away models"):
        c.resolve("sol-specialist")
    c.allow_fallback = True
    assert c.resolve("sol-specialist") == "sol-smart"


def test_adaptive_length_retry_turns_reasoning_off(store):
    doc = {"name": "length_test", "steps": [
        {"id": "gen", "model": "m", "prompt": "write long essay", "reasoning": "high", "max_tokens": 1000, "retries": 1}
    ]}
    r1 = ChatResult(content="cut off text...", finish_reason="length", completion_tokens=1000)
    r2 = ChatResult(content="complete answer here", finish_reason="stop", completion_tokens=1800)
    client = Scripted({"m": [r1, r2]})
    rid = Runner(client, store).run(parse(doc), {})
    assert store.run(rid)["status"] == "succeeded"
    assert len(client.calls) == 2
    assert client.calls[0]["reasoning"] == "high"
    assert client.calls[0]["max_tokens"] == 1000
    assert client.calls[1]["reasoning"] == "off"
    assert client.calls[1]["max_tokens"] == 2000


def test_router_502_retries_and_recovers(monkeypatch):
    monkeypatch.setattr("sol_control_hud.chains.router.engine_mode", lambda: "desk")
    attempts = 0

    def handler(req):
        nonlocal attempts
        if req.url.path == "/models":
            return httpx.Response(200, json=MODELS)
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, text="bad gateway (model loading)")
        sse = "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    c = _router(handler)
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = c.chat("sol-fast", [{"role": "user", "content": "hi"}])
    assert res.content == "ok"
    assert attempts == 2


def test_router_502_exhausted_raises_unavailable(monkeypatch):
    monkeypatch.setattr("sol_control_hud.chains.router.engine_mode", lambda: "desk")

    def handler(req):
        if req.url.path == "/models":
            return httpx.Response(200, json=MODELS)
        return httpx.Response(503, text="service unavailable")

    c = _router(handler)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(ModelUnavailable, match="switching models"):
        c.chat("sol-fast", [{"role": "user", "content": "hi"}])


def test_router_client_waits_while_the_game_guard_has_the_ai_off(monkeypatch):
    monkeypatch.setattr("sol_control_hud.chains.router.engine_mode", lambda: "off")
    with pytest.raises(ModelUnavailable, match="game guard"):
        _router(lambda req: httpx.Response(200, json=MODELS)).resolve("sol-fast")


def test_router_budget_never_loads_a_model_just_to_count(monkeypatch):
    monkeypatch.setattr("sol_control_hud.chains.router.engine_mode", lambda: "desk")
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path == "/models":
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json={"tokens": list(range(100))})

    c = _router(handler)
    assert c.fits("sol-fast", [{"role": "user", "content": "x"}], 4000) == (True, 116, 32768)   # loaded: exact
    ok, n, ctx = c.fits("sol-smart", [{"role": "user", "content": "y" * 300000}], 4000)       # unloaded: estimate
    assert not ok and n > 65536 and paths.count("/tokenize") == 1


# ---- file tools
@pytest.fixture
def area(tmp_path):
    read, write, other = tmp_path / "read", tmp_path / "read" / "notebook", tmp_path / "secret-place"
    for d in (read, write, other):
        d.mkdir(parents=True, exist_ok=True)
    (other / "private.md").write_text("private", encoding="utf-8")
    return ft.Policy(read_roots=[str(read)], write_roots=[str(write)], repo_roots=[str(read)]), read, write, other


def test_path_guards_resolve_dotdot_and_junctions(area):
    policy, read, write, other = area
    with pytest.raises(ft.FileToolError, match="not allowed to read"):
        ft.check_read(read / ".." / "secret-place" / "private.md", policy)
    link = read / "sneaky"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(other)], check=True, capture_output=True)
    with pytest.raises(ft.FileToolError, match="not allowed to read"):     # the junction points outside
        ft.check_read(link / "private.md", policy)
    with pytest.raises(ft.FileToolError, match="not allowed to write"):
        ft.check_write(read / "x.md", policy)                               # readable is not writable
    (read / ".env").write_text("KEY=1"); (read / "api_token.txt").write_text("t")
    for bad in (".env", "api_token.txt"):
        with pytest.raises(ft.FileToolError, match="secret"):
            ft.check_read(read / bad, policy)


def test_save_note_never_overwrites_your_notes_but_updates_its_own(area):
    policy, read, write, other = area
    mine = write / "Mine.md"; mine.write_text("my writing", encoding="utf-8")
    with pytest.raises(ft.FileToolError, match="not overwriting"):
        ft.save_note({"path": str(mine), "text": "x"}, policy=policy)
    assert mine.read_text(encoding="utf-8") == "my writing"
    out = ft.save_note({"folder": str(write), "name": "Res: a/b?", "text": "v1", "frontmatter": {"status": "running"}}, policy=policy)
    assert Path(out["path"]).name == "Res a b.md"
    ft.save_note({"path": out["path"], "text": "v2"}, policy=policy)          # its own note: updated
    with pytest.raises(ft.FileToolError, match="not allowed to write"):     # a path can't climb out of the write area
        ft.save_note({"path": str(write / ".." / ".." / "escape.md"), "text": "x"}, policy=policy)
    escaped = ft.save_note({"folder": str(write), "name": "..\\..\\escape", "text": "x"}, policy=policy)
    assert Path(escaped["path"]).parent == write                           # a title becomes one file name in the folder
    text = Path(out["path"]).read_text(encoding="utf-8")
    assert "made_by: sol-chain" in text and text.rstrip().endswith("v2")


def test_list_files_chunks_big_files_and_skips_denied(area):
    policy, read, write, other = area
    (read / "big.md").write_text("\n\n".join(f"paragraph {i} " + "word " * 50 for i in range(40)), encoding="utf-8")
    (read / "small.md").write_text("hi", encoding="utf-8")
    (read / ".git").mkdir(); (read / ".git" / "x.md").write_text("no")
    items = ft.list_files({"folder": str(read), "glob": "*.md", "chunk_tokens": 1000}, policy=policy)
    names = [i["name"] for i in items]
    assert "small.md" in names and not any("x.md" in n for n in names)
    parts = [i for i in items if i["path"].endswith("big.md")]
    assert len(parts) > 1 and all(i["parts"] == len(parts) for i in parts)
    assert "".join(ft.read_item(i, policy) for i in parts) == (read / "big.md").read_text(encoding="utf-8")


def test_scanned_pdf_is_reported_as_no_text(area):
    from pypdf import PdfWriter
    policy, read, write, other = area
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    with open(read / "scan.pdf", "wb") as f:
        w.write(f)
    assert ft.read_file(read / "scan.pdf", policy)["no_text"] is True
    assert "no text layer" in ft.read_item({"path": str(read / "scan.pdf")}, policy)


def test_git_changes_skips_vendored_binary_and_lock_files(area):
    policy, read, write, other = area
    repo = read / "repo"; repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n"); g("add", "."); g("commit", "-qm", "one")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "a.py").write_text("x = 2\n")
    (repo / "public").mkdir(); (repo / "public" / "lib.js").write_text("var a;\n")
    (repo / "app.min.js").write_text("a\n"); (repo / "package-lock.json").write_text("{}\n")
    (repo / "img.png").write_bytes(b"\x89PNG\x00\x01")
    g("add", "."); g("commit", "-qm", "two")
    out = ft.git_changes({"repo": str(repo), "since": base}, policy=policy)
    by = {f["file"]: f for f in out["files"]}
    assert "diff" in by["a.py"] and "+x = 2" in by["a.py"]["diff"]
    assert by["public/lib.js"]["skipped"].startswith("vendored") and by["app.min.js"]["skipped"].startswith("minified")
    assert by["package-lock.json"]["skipped"] == "lock file" and by["img.png"]["skipped"] == "binary"
    assert [f["file"] for f in out["reviewable"]] == ["a.py"]


# ---- validator
def test_check_reports_plain_english_problems(tmp_path):
    from sol_control_hud.chains.__main__ import check_file
    bad_yaml = tmp_path / "a.yaml"; bad_yaml.write_text("name: x\nsteps:\n  - id: a\n   model: m\n")
    assert "line" in check_file(bad_yaml)[0]
    wf = tmp_path / "b.yaml"
    wf.write_text("name: x\nsteps:\n  - {id: a, tool: nope}\n  - {id: b, model: sol-nonexistent, prompt: hi}\n")
    problems = check_file(wf)
    assert any("tool 'nope'" in p for p in problems) and any("sol-nonexistent" in p for p in problems)
    ok = Path(__file__).parents[1] / "workflows" / "transcript-note.yaml"
    assert check_file(ok) == []


def test_ran_out_of_tokens_retries_with_twice_the_room(store):
    class Cut(Scripted):
        def chat(self, model, messages, **kw):
            self.calls.append({"model": model, "messages": messages, **kw})
            if len(self.calls) == 1:
                return ChatResult(content="", finish_reason="length", completion_tokens=kw["max_tokens"])
            return ChatResult(content="done", finish_reason="stop", completion_tokens=5)
    client = Cut({})
    doc = {"name": "t", "steps": [{"id": "s", "model": "m", "prompt": "p", "max_tokens": 3000, "retries": 1}]}
    rid = Runner(client, store).run(parse(doc), {})
    assert store.run(rid)["status"] == "succeeded"
    assert [c["max_tokens"] for c in client.calls] == [3000, 6000]
    assert [m["role"] for m in client.calls[1]["messages"]] == ["system", "user"]  # a fresh try (date + prompt),
    assert client.calls[1]["messages"][-1]["content"] == "p"                        # not "your answer was rejected"


def test_compact_text_and_step_compaction(store):
    short = "Short message"
    assert compact_text(short, 100) == short

    long_text = "HEAD: Important introduction and context. " + ("x" * 200) + " TAIL: Final conclusion and key findings."
    compacted = compact_text(long_text, max_chars=80)
    assert len(compacted) < len(long_text)
    assert "HEAD: Important" in compacted
    assert "key findings" in compacted
    assert "[Compact memory:" in compacted

    # Test within Runner
    client = Scripted({"m": [long_text]})
    doc = {"name": "compaction_test", "steps": [
        {"id": "step1", "model": "m", "prompt": "test prompt", "compact": 80}
    ]}
    rid = Runner(client, store).run(parse(doc), {})
    run = store.run(rid)
    assert run["status"] == "succeeded"
    saved = store.saved_outputs(rid)
    out = saved["step1"][-1]
    assert "[Compact memory:" in out
    events = store.events(rid)
    assert any(ev["kind"] == "compacted" and ev["step"] == "step1" for ev in events)


def test_compact_after_directive_and_line_boundary_snapping(store):
    from sol_control_hud.chains.workflow import parse
    # Test line-boundary snapping in structured text
    lines = [
        "## Summary",
        "- Item 1: Critical architectural finding",
        "- Item 2: Secondary diagnostic metric",
    ] + [f"- Detail line {i} with some background text" for i in range(20)] + [
        "## Next Steps",
        "- Final recommendation 1",
        "- Final recommendation 2",
    ]
    doc_text = "\n".join(lines)
    compacted = compact_text(doc_text, max_chars=180)
    assert "[Compact memory:" in compacted
    assert "## Summary" in compacted
    assert "- Item 1: Critical architectural finding" in compacted
    assert "Final recommendation 2" in compacted

    # Test compact_after in workflow parse
    doc = {"name": "compact_after_workflow", "steps": [
        {"id": "step_a", "model": "m", "prompt": "prompt", "compact_after": 120}
    ]}
    wf = parse(doc)
    assert wf.steps[0].compact == 120


def test_abort_inference_flag_stops_run(store, tmp_path, monkeypatch):
    from sol_control_hud.chains import runner
    llm_dir = tmp_path / "llm"
    llm_dir.mkdir()
    monkeypatch.setattr(runner, "LLM_DIR", llm_dir)
    wf = parse(THREE)

    class AbortDuringA(Scripted):
        def chat(self, model, messages, **kw):
            (llm_dir / "abort_inference.flag").write_text("abort", encoding="utf-8")
            return super().chat(model, messages, **kw)

    rid = Runner(AbortDuringA({"m": ["a1"]}), store).run(wf, {"topic": "x"})
    assert store.run(rid)["status"] == "cancelled"


def test_router_client_aborts_on_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("sol_control_hud.chains.router.engine_mode", lambda: "desk")
    flag = tmp_path / "abort_inference.flag"
    flag.write_text("abort", encoding="utf-8")
    monkeypatch.setattr("sol_control_hud.chains.router.ABORT_FLAG", flag)

    def handler(req):
        if req.url.path == "/models":
            return httpx.Response(200, json=MODELS)
        sse = 'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    c = _router(handler)
    from sol_control_hud.chains.workflow import Cancelled
    with pytest.raises(Cancelled):
        c.chat("sol-fast", [{"role": "user", "content": "hi"}], reasoning="off")
    assert not flag.exists()


def test_scan_stale_notes_and_archive_note(area):
    policy, read, write, other = area
    rev_dir = write / "Reviews"
    rev_dir.mkdir(parents=True)
    stale = rev_dir / "old_review.md"
    stale.write_text("# Old review\nContent here", encoding="utf-8")

    old_time = time.time() - (40 * 86400)
    os.utime(stale, (old_time, old_time))

    fresh = rev_dir / "fresh.md"
    fresh.write_text("# Fresh review", encoding="utf-8")

    protected = rev_dir / "keep.md"
    protected.write_text("---\narchive: false\n---\nImportant", encoding="utf-8")
    os.utime(protected, (old_time, old_time))

    res = ft.scan_stale_notes({"folder": str(write), "days": 30}, policy=policy)
    assert res["count"] == 1
    assert res["notes"][0]["name"] == "old_review.md"

    arc = ft.archive_note({"path": str(stale), "digest": "Digests/2026-09 Summary"}, policy=policy)
    assert not stale.exists()
    archived_file = Path(arc["archived"])
    assert archived_file.exists()
    content = archived_file.read_text(encoding="utf-8")
    assert "[!NOTE] Archived Note" in content
    assert "Condensed into [[Digests/2026-09 Summary]]" in content
    manifest = archived_file.parent / "_archived.md"
    assert manifest.exists() and "old_review.md" in manifest.read_text(encoding="utf-8")


def test_sync_tasks_creates_and_deduplicates(tmp_path):
    tasks_file = tmp_path / "Tasks.md"
    policy = ft.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path)])

    tasks_input = [
        {"task": "Submit chemistry lab report", "due": "2026-11-15", "loop": "school"},
        {"task": "Fix subprocess timeout in media-api", "due": None, "source": "Weekly digest"},
        "Buy cat food",
    ]

    res = ft.sync_tasks({"file": str(tasks_file), "tasks": tasks_input}, policy=policy)
    assert res["ok"] is True
    assert res["added"] == 3
    assert res["total_open"] == 3

    content = tasks_file.read_text(encoding="utf-8")
    assert "- [ ] Submit chemistry lab report (due: 2026-11-15, loop: school)" in content
    assert "- [ ] Fix subprocess timeout in media-api (from: Weekly digest)" in content
    assert "- [ ] Buy cat food" in content

    # Run again with duplicates and one new task
    tasks_input_2 = [
        {"task": "Submit chemistry lab report", "due": "2026-11-15"},
        "buy cat food",  # case-insensitive duplicate
        {"task": "Review PR on Solopass/gatkam", "due": "tomorrow"},
    ]
    res2 = ft.sync_tasks({"file": str(tasks_file), "tasks": tasks_input_2}, policy=policy)
    assert res2["ok"] is True
    assert res2["added"] == 1
    assert res2["total_open"] == 4

    content2 = tasks_file.read_text(encoding="utf-8")
    assert content2.count("chemistry lab report") == 1
    assert content2.count("cat food") == 1
    assert "- [ ] Review PR on Solopass/gatkam" in content2







def test_default_policy_reads_claude_log_but_never_writes_it():
    """The Daily work summary chain gathers D:\AI\Cache\claude-log (allowed 2026-11-23); the rest of AI\Cache stays out."""
    from sol_control_hud.chains import file_tools as ft
    pol = ft.Policy()
    assert ft._under(Path(r"D:\AI\Cache\claude-log\2026-11-23.jsonl"), pol.read_roots)
    assert not ft._under(Path(r"D:\AI\Cache\other\x.txt"), pol.read_roots)
    assert not ft._under(Path(r"D:\AI\Cache\claude-log\x.md"), pol.write_roots)
