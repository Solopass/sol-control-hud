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
from sol_control_hud.chains.runner import Runner
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
    assert client.calls[0]["messages"][0] == {"role": "system", "content": "You are terse."}
    assert client.calls[1]["reasoning"] == "high" and client.calls[1]["temperature"] == 0.7
    assert "reasoning" not in client.calls[0]                      # default: the model's own setting
    assert client.calls[2]["messages"][0]["content"] == "You are a critic."  # a step's system replaces the default


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
    assert len(client.calls[1]["messages"]) == 1          # a fresh try, not "your answer was rejected"
