"""Chain notes (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 2): parsing, references, result note, status line, resume."""
import json
from pathlib import Path

import pytest

from sol_control_hud.chains import chain_note as cn
from sol_control_hud.chains import file_tools as ft
from sol_control_hud.chains.llm import ChatResult
from sol_control_hud.chains.runner import Runner
from sol_control_hud.chains.store import Store

NOTE = """---
type: chain
status: queued
model: sol-fast
system: You are terse about {{topic}}.
inputs:
  topic: rivers
tags: [keep-me]
---
A description line (not a step).

## Outline
reasoning: off
check: 2-4 bullets
Give an outline about {{topic}} for {{today}}.

```markdown
## Not a step (inside a code block)
```

## Draft
model: sol-smart
Write it from {{Outline}}. Then {{previous}} again.

## Ideas per line
for each line in {{Outline}}:
Expand {{line}}.

## Index
Combine {{Ideas per line}}.
"""


class Scripted:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})
        a = self.answers.pop(0)
        return ChatResult(content=a, finish_reason="stop", completion_tokens=5)


@pytest.fixture
def env(tmp_path, monkeypatch):
    chains = tmp_path / "1Notebook" / "Chains"
    chains.mkdir(parents=True)
    monkeypatch.setattr(cn, "CHAINS_DIR", chains)
    monkeypatch.setattr(cn, "VAULT", tmp_path)
    monkeypatch.setattr(ft, "POLICY", ft.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path / "1Notebook")],
                                                repo_roots=[str(tmp_path)]))
    store = Store(tmp_path / "runs.sqlite")
    return chains, store


def _run(path, store, client, new=False):
    tools = {**ft.TOOLS, **cn.TOOLS}
    snapshots = []

    def make(on_step):
        def hook(rid, step, item, out):
            on_step(rid, step, item, out)
            res = cn.results_dir(cn.load_chain(path)) / (cn.result_name(cn.load_chain(path)) + ".md")
            snapshots.append(res.read_text(encoding="utf-8"))
        return Runner(client, store, tools=tools, on_step=hook)
    return cn.run_chain(path, store, make, new=new), snapshots


def test_parse_steps_settings_and_code_fences(env):
    chains, _ = env
    p = chains / "Essay.md"; p.write_text(NOTE, encoding="utf-8")
    c = cn.load_chain(p)
    assert [s.name for s in c.steps] == ["Outline", "Draft", "Ideas per line", "Index"]
    o = c.steps[0]
    assert o.settings == {"reasoning": "off"} and o.checks == [{"path": "", "min_bullets": 2, "max_bullets": 4}]
    assert c.steps[2].loop == {"kind": "lines", "var": "line", "source": "{{Outline}}"}
    assert "## Not a step" in o.prompt   # the fenced heading stayed part of the Outline prompt
    doc = cn.to_workflow(c)
    ids = [s["id"] for s in doc["steps"]]
    assert ids == ["outline", "draft", "ideas_per_line__items", "ideas_per_line", "ideas_per_line__joined", "index"]
    by = {s["id"]: s for s in doc["steps"]}
    assert "{{ inputs.topic }}" in by["outline"]["prompt"] and "{{ inputs._today }}" in by["outline"]["prompt"]
    assert by["draft"]["prompt"] == "Write it from {{ steps.outline.output }}. Then {{ steps.outline.output }} again."
    assert by["index"]["prompt"] == "Combine {{ steps.ideas_per_line__joined.output }}."   # a loop reads as text
    assert by["ideas_per_line__items"]["args"] == {"value": "{{ steps.outline.output }}"}
    assert doc["system"] == "You are terse about {{ inputs.topic }}."


@pytest.mark.parametrize("body, msg", [
    ("## A\nUse {{B}}\n\n## B\nhi", "runs later"),
    ("## A\nUse {{nope}}", "I don't know {{nope}}"),
    ("## A\n{{previous}}", "first step"),
    ("## A\nhi\n## a\nho", "two steps are called"),
    ("## A\nmodel: sol-fast\n", "has no prompt"),
    ("## A\ncheck: be nice\nhi", "don't understand `check: be nice`"),
    ("no headings at all", "no steps"),
    ("## A\nfor each line in plain text:\nhi", "needs one step"),
])
def test_errors_are_plain_english_with_line_numbers(env, body, msg):
    chains, _ = env
    p = chains / "Bad.md"; p.write_text("---\nstatus: draft\n---\n" + body, encoding="utf-8")
    with pytest.raises(cn.ChainError, match=msg):
        cn.to_workflow(cn.load_chain(p))
    if "line" in msg or "runs later" in msg:
        with pytest.raises(cn.ChainError, match=r"line \d"):
            cn.to_workflow(cn.load_chain(p))


def test_run_writes_result_as_it_goes_and_updates_only_the_status_line(env):
    chains, store = env
    p = chains / "Essay.md"; p.write_bytes(NOTE.replace("\n", "\r\n").encode())
    client = Scripted(["- one\n- two", "the draft", "exp one", "exp two", "the index"])
    rid, snaps = _run(p, store, client)
    assert store.run(rid)["status"] == "succeeded"
    assert client.calls[0]["reasoning"] == "off" and client.calls[1]["model"] == "sol-smart"
    assert client.calls[0]["messages"][0]["content"] == "You are terse about rivers."
    assert "Expand one." in client.calls[2]["messages"][-1]["content"]   # bullets became loop items
    assert any("_running…_" in s or "_not run yet_" in s for s in snaps)   # it grew step by step
    result = (chains / "Results" / "Essay (result).md").read_text(encoding="utf-8")
    assert "status: done" in result and "chain: '[[Essay]]'" in result and "### one\nexp one" in result
    note = p.read_bytes().decode()
    assert "status: done\r\n" in note and f"last_run: {rid}" in note
    assert note.replace("status: done", "status: queued").replace(f"\r\nlast_run: {rid}", "") == NOTE.replace("\n", "\r\n")


def test_fix_and_requeue_resumes_from_the_edited_step(env):
    chains, store = env
    p = chains / "Two.md"
    p.write_text("---\nstatus: queued\n---\n## A\nSay a.\n\n## B\ncheck: at least 3 words\nSay b.\n\n## C\nSay c from {{B}}.\n", encoding="utf-8")
    rid, _ = _run(p, store, Scripted(["a!", "b", "b"]))     # B fails its check twice (retries: 1)
    assert store.run(rid)["status"] == "needs_user" and "status: needs-you" in p.read_text(encoding="utf-8")
    result = (chains / "Results" / "Two (result).md").read_text(encoding="utf-8")
    assert "Needs you" in result and "at least 3" in result
    p.write_text(p.read_text(encoding="utf-8").replace("Say b.", "Say b in three words."), encoding="utf-8")
    client = Scripted(["b b b", "c!"])
    rid2, _ = _run(p, store, client)
    assert rid2 == rid and store.run(rid)["status"] == "succeeded"
    assert [c["messages"][-1]["content"] for c in client.calls] == ["Say b in three words.", "Say c from b b b."]  # A reused


def test_changed_settings_start_a_new_run(env):
    chains, store = env
    p = chains / "S.md"
    p.write_text("---\nstatus: queued\nmodel: sol-fast\n---\n## A\ncheck: at least 3 words\nSay a.\n", encoding="utf-8")
    rid, _ = _run(p, store, Scripted(["x", "x"]))
    p.write_text(p.read_text(encoding="utf-8").replace("model: sol-fast", "model: sol-smart"), encoding="utf-8")
    rid2, _ = _run(p, store, Scripted(["a a a"]))
    assert rid2 != rid and store.run(rid2)["status"] == "succeeded"


def test_file_loop_and_save_to(env, tmp_path):
    chains, store = env
    notes = tmp_path / "notes"; notes.mkdir()
    (notes / "a.md").write_text("alpha text", encoding="utf-8"); (notes / "b.md").write_text("beta text", encoding="utf-8")
    p = chains / "Digest.md"
    p.write_text(f"---\nstatus: queued\ninputs:\n  folder: {notes}\nsave_to: 1Notebook/Digests/{{{{week}}}}.md\n---\n"
                 "## Each\nfor each file in {{folder}} matching *.md changed in the last 7 days:\nSum {{file.name}}: {{file.text}}\n\n"
                 "## Digest\nDigest of {{Each}}\n", encoding="utf-8")
    client = Scripted(["sum a", "sum b", "the digest"])
    rid, _ = _run(p, store, client)
    assert store.run(rid)["status"] == "succeeded"
    assert client.calls[0]["messages"][-1]["content"] == "Sum a.md: alpha text"
    assert "### a.md\nsum a" in client.calls[2]["messages"][-1]["content"]
    week = cn.builtins_now()["_week"]
    digest = (tmp_path / "1Notebook" / "Digests" / f"{week}.md").read_text(encoding="utf-8")
    assert "the digest" in digest and "made_by: sol-chain" in digest


def test_helpers():
    assert cn.to_list({"value": "# Title\n- one\n2. two\n\n* three"}) == ["one", "two", "three"]
    assert cn.to_list({"value": '["x", "y"]'}) == ["x", "y"]
    assert cn.join_items({"items": [{"name": "a.md"}, "b"], "outputs": ["A", {"error": "boom"}]}) == \
        "### a.md\nA\n\n### b\n_(skipped: boom)_"


def test_check_command_understands_chain_notes(env):
    chains, _ = env
    from sol_control_hud.chains.__main__ import check_file
    good = chains / "G.md"; good.write_text("## A\nhi\n", encoding="utf-8")
    bad = chains / "B.md"; bad.write_text("## A\nmodel: sol-nonexistent\nhi\n", encoding="utf-8")
    assert check_file(good) == []
    assert any("sol-nonexistent" in p for p in check_file(bad))
