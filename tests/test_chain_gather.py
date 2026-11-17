"""Phase 4 (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md): gather steps, review markers, hide, save_steps."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from sol_control_hud.chains import chain_note as cn
from sol_control_hud.chains import file_tools as ft
from sol_control_hud.chains import gather_tools as gt
from sol_control_hud.chains.llm import ChatResult
from sol_control_hud.chains.runner import Runner
from sol_control_hud.chains.store import Store


class Scripted:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def chat(self, model, messages, **kw):
        self.calls.append({"model": model, "prompt": messages[-1]["content"]})
        a = self.answers.pop(0)
        return ChatResult(content=a, finish_reason="stop", completion_tokens=3)


@pytest.fixture
def env(tmp_path, monkeypatch):
    chains = tmp_path / "1Notebook" / "Chains"; chains.mkdir(parents=True)
    monkeypatch.setattr(cn, "CHAINS_DIR", chains)
    monkeypatch.setattr(cn, "VAULT", tmp_path)
    monkeypatch.setattr(ft, "POLICY", ft.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path / "1Notebook")],
                                                repo_roots=[str(tmp_path)]))
    monkeypatch.setattr(gt, "MARKERS", tmp_path / "markers.json")
    return chains, Store(tmp_path / "runs.sqlite"), tmp_path


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


def make_repo(root: Path, name: str) -> Path:
    repo = root / "ws" / name; repo.mkdir(parents=True)
    git(repo, "init", "-q"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("def f():\n    return 1\n"); git(repo, "add", "."); git(repo, "commit", "-qm", "first")
    (repo / "app.py").write_text("def f():\n    return 2\n")
    (repo / "package-lock.json").write_text("{}\n"); git(repo, "add", "."); git(repo, "commit", "-qm", "second")
    return repo


def run(path, store, client):
    return cn.run_chain(path, store, lambda on_step: Runner(client, store, tools={**ft.TOOLS, **cn.TOOLS}, on_step=on_step))


def test_gather_files_and_commits(env):
    chains, store, tmp = env
    notes = tmp / "notes"; notes.mkdir()
    (notes / "b.md").write_text("beta"); (notes / "a.md").write_text("alpha")
    old = notes / "old.md"; old.write_text("old"); os.utime(old, (1, 1))
    text = gt.gather_files({"folder": str(notes), "glob": "*.md", "since_days": 7})
    assert text == "### a.md\nalpha\n\n### b.md\nbeta\n"          # sorted, the old file left out
    make_repo(tmp, "proj")
    log = gt.gather_commits({"path": str(tmp / "ws"), "days": 7})
    assert log.startswith("### proj (2 commits)") and "second" in log


REVIEW = """---
type: chain
model: sol-coder
system: You review code.
save_to: 1Notebook/Reviews/{{today}} code review.md
save_steps: Top issues, Per file
---
## Changes
gather: changes in {{ws}} since last review

## Per file
hide: No issues
for each change in {{Changes}}:
Review {{change.file}} in {{change.repo}}:
{{change.diff}}

## Top issues
Top issues from: {{Per file}}
"""


def test_review_chain_marks_repos_only_after_success(env):
    chains, store, tmp = env
    make_repo(tmp, "proj")
    p = chains / "Code review.md"
    p.write_text(REVIEW.replace("{{ws}}", str(tmp / "ws")), encoding="utf-8")
    doc = cn.to_workflow(cn.load_chain(p))
    assert doc["steps"][-1] == {"id": "changes__mark", "tool": "mark_reviewed", "args": {"changes": "{{ steps.changes.output }}"}}
    assert "long_model" not in [s for s in doc["steps"] if s["id"] == "per_file"][0]   # Away model: no sol-long fallback
    assert cn.lane(cn.load_chain(p)) == "away"

    client = Scripted(["- bug: f() changed its return value", "1. proj/app.py return value"])
    rid = run(p, store, client)
    assert store.run(rid)["status"] == "succeeded"
    assert "return 2" in client.calls[0]["prompt"] and "package-lock" not in json.dumps(client.calls)   # lock file skipped
    markers = json.loads((tmp / "markers.json").read_text())
    assert list(markers) == [str(tmp / "ws" / "proj")]
    digest = next((tmp / "1Notebook" / "Reviews").glob("*code review.md")).read_text(encoding="utf-8")
    assert digest.index("## Top issues") < digest.index("## Per file") and "### proj: app.py" in digest

    client2 = Scripted(["nothing to report"])                          # no new commits: nothing to review
    run(p, store, client2)
    assert len(client2.calls) == 1 and "Top issues from: " in client2.calls[0]["prompt"]


def test_failed_review_marks_nothing(env):
    chains, store, tmp = env
    make_repo(tmp, "proj")
    p = chains / "R.md"
    p.write_text(REVIEW.replace("{{ws}}", str(tmp / "ws")).replace("Top issues from:", "check: mentions ZZZ\nTop issues from:"),
                 encoding="utf-8")
    rid = run(p, store, Scripted(["No issues.", "meh", "meh"]))
    assert store.run(rid)["status"] == "needs_user" and not (tmp / "markers.json").exists()


def test_hide_collapses_boring_answers():
    out = cn.join_items({"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
                         "outputs": ["No issues.", "- real bug", "no issues"], "hide": "No issues"})
    assert out == "### b\n- real bug\n\n_(2 more: No issues.)_"
    assert cn.join_items({"items": ["x"], "outputs": ["No issues."], "hide": "No issues"}) == "_(1 more: No issues.)_"


def test_group_items_splits_big_groups_into_parts():
    items = [{"name": f"a: f{i}", "repo": "a"} for i in range(3)] + [{"name": "b: g", "repo": "b"}, {"name": "b: h", "repo": "b"}]
    outs = ["x" * 3000, "y" * 3000, "No issues.", "- bug in g", "no issues"]
    got = cn.group_items({"items": items, "outputs": outs, "key": "repo", "hide": "No issues", "chunk_tokens": 1000})
    assert [g["name"] for g in got] == ["a (1/2)", "a (2/2)", "b"]                     # 4000 chars a part
    assert got[0]["text"].startswith("### a: f0\nxxx") and "yyy" not in got[0]["text"]
    assert got[1]["text"].endswith("_(1 more: No issues.)_") and got[1]["part"] == 2 and got[1]["parts"] == 2
    assert got[2] == {"name": "b", "repo": "b", "part": 1, "parts": 1, "files": 1,
                      "text": "### b: g\n- bug in g\n\n_(1 more: No issues.)_"}


def test_review_with_a_per_project_step(env):
    chains, store, tmp = env
    make_repo(tmp, "proj")
    p = chains / "Code review.md"
    grouped = REVIEW.replace("## Top issues", "## Per project\nfor each part in {{Per file}} grouped by repo:\n"
                                              "Sum up {{part.name}}:\n{{part.text}}\n\n## Top issues")
    grouped = grouped.replace("Top issues from: {{Per file}}", "Top issues from: {{Per project}}")
    p.write_text(grouped.replace("{{ws}}", str(tmp / "ws")), encoding="utf-8")
    doc = cn.to_workflow(cn.load_chain(p))
    assert [s for s in doc["steps"] if s["id"] == "per_project__items"][0]["tool"] == "group_items"
    client = Scripted(["- bug: f() changed its return value", "proj: one bug in app.py", "1. proj/app.py"])
    rid = run(p, store, client)
    assert store.run(rid)["status"] == "succeeded"
    assert "Sum up proj:" in client.calls[1]["prompt"] and "- bug: f() changed" in client.calls[1]["prompt"]
    assert "### proj\nproj: one bug in app.py" in client.calls[2]["prompt"]
    with pytest.raises(cn.ChainError, match="needs an earlier `for each` step"):
        p.write_text(REVIEW.replace("{{ws}}", str(tmp / "ws")).replace(
            "## Top issues\n", "## Top issues\nfor each part in {{Changes}} grouped by repo:\n"), encoding="utf-8")
        cn.to_workflow(cn.load_chain(p))


@pytest.mark.parametrize("body, msg", [
    ("## A\ngather: the internet\n", "don't understand `gather: the internet`"),
    ("## A\ngather: commits in D:\\x\nSummarize it.\n", "only collects"),
    ("## A\nhide: x\nSay a.\n", "only works on a `for each`"),
])
def test_gather_errors(env, body, msg):
    chains, store, tmp = env
    p = chains / "E.md"; p.write_text(body, encoding="utf-8")
    with pytest.raises(cn.ChainError, match=msg):
        cn.to_workflow(cn.load_chain(p))


def test_save_steps_must_name_real_steps(env):
    chains, store, tmp = env
    p = chains / "S.md"
    p.write_text("---\nsave_to: 1Notebook/x.md\nsave_steps: A, Nope\n---\n## A\nSay a.\n", encoding="utf-8")
    with pytest.raises(cn.ChainError, match="no step called 'Nope'"):
        cn.to_workflow(cn.load_chain(p))


def test_review_changes_caps_diffs_files_and_repos(env, monkeypatch):
    chains, store, tmp = env
    for name in ("a-repo", "b-repo", "c-repo"):
        repo = make_repo(tmp, name)
        for i in range(5):                       # 5 more changed files per repo; one has a huge diff
            (repo / f"f{i}.py").write_text("x = 1\n" * (400 if i == 4 else 3))
        git(repo, "add", "."); git(repo, "commit", "-qm", "more")
    out = gt.review_changes({"path": str(tmp / "ws"), "max_diff_chars": 500, "max_per_repo": 4, "max_total": 9})
    assert len(out) == 8 and {i["repo"] for i in out} == {"a-repo", "b-repo"}   # c-repo would go over 9: left for next time
    assert all(len(i["diff"]) < 700 for i in out)
    assert any("diff cut" in i["diff"] for i in out) is False                    # the biggest file is dropped by the per-repo cap
    out = gt.review_changes({"path": str(tmp / "ws"), "max_diff_chars": 100, "max_per_repo": 99, "max_total": 99})
    assert any("diff cut" in i["diff"] for i in out)
    gt.mark_reviewed({"changes": out})                                        # all three repos fit: all marked
    assert len(json.loads((tmp / "markers.json").read_text())) == 3


INGEST = "---\nstatus: queued\nkind: ingest\n---\n## Ingest\ntool: fake_ingest\n"


def _ingest_run(env, outputs):
    chains, store, tmp = env
    p = chains / "Inbox.md"
    if not p.exists():
        p.write_text(INGEST, encoding="utf-8")
    tools = {**ft.TOOLS, **cn.TOOLS, "fake_ingest": lambda args, ctx=None: outputs}
    rid = cn.run_chain(p, store, lambda on_step: Runner(Scripted([]), store, tools=tools, on_step=on_step))
    return rid, chains / "Results" / "Inbox (result).md"


def test_ingest_chain_with_an_empty_inbox_leaves_the_result_note_alone(env):
    idle = {"processed": [], "count": 0, "failed": [], "failed_count": 0, "given_up": 0, ft.IDLE_KEY: True}
    rid, result = _ingest_run(env, idle)
    assert env[1].run(rid)["status"] == "succeeded" and cn.run_was_idle(env[1], rid)
    assert not result.exists()                                    # never written: not in the briefing as "Done"
    work = {"processed": [{"note": "x.md"}], "count": 1, "failed": [], "failed_count": 0, "given_up": 0}
    rid2, _ = _ingest_run(env, work)
    assert not cn.run_was_idle(env[1], rid2)
    text = result.read_text(encoding="utf-8")                     # a run that did work writes it as before
    assert "status: done" in text and '"count": 1' in text
    before = result.stat().st_mtime_ns, text
    rid3, _ = _ingest_run(env, idle)                              # a later empty run doesn't touch it
    assert cn.run_was_idle(env[1], rid3) and (result.stat().st_mtime_ns, result.read_text(encoding="utf-8")) == before


def test_empty_batch_is_marked_idle_only_when_nothing_happened(tmp_path):
    from sol_control_hud.chains import ingest_tools
    inbox = tmp_path / "pdf"; inbox.mkdir()
    args = {"inbox": str(inbox), "ledger_file": str(tmp_path / "ledger.json")}
    res = ingest_tools._ingest_batch("pdf", lambda *a, **k: {}, args, None, {}, None, lambda: False)
    assert res[ft.IDLE_KEY] is True and res["count"] == 0
