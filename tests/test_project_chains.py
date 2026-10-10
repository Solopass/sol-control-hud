from pathlib import Path
import pytest

from sol_control_hud.chains import project_chains
from sol_control_hud.chains import chain_note as cn


def test_find_chain_for_project(tmp_path):
    chains = tmp_path / "Chains"
    chains.mkdir()

    (chains / "Bug hunt speed-reading.md").write_text("---\ntype: chain\nstatus: done\n---\n## F\nx\n", encoding="utf-8")
    (chains / "Bug hunt.md").write_text("---\ntype: chain\nstatus: done\n---\n## F\nx\n", encoding="utf-8")
    (chains / "Deep review textdiff.md").write_text("---\ntype: chain\nstatus: done\n---\n## F\nx\n", encoding="utf-8")

    assert project_chains.find_chain_for_project("speed-reading", chains_dir=chains) == chains / "Bug hunt speed-reading.md"
    assert project_chains.find_chain_for_project("media-api", chains_dir=chains) == chains / "Bug hunt.md"
    assert project_chains.find_chain_for_project("textdiff", chains_dir=chains) == chains / "Deep review textdiff.md"
    assert project_chains.find_chain_for_project("unknown-proj", chains_dir=chains) is None


def test_detect_globs_for_project(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "main.py").write_text("print(1)")
    (proj / "app.ts").write_text("console.log(1)")

    globs = project_chains.detect_globs_for_project(proj)
    assert "*.py" in globs
    assert "*.ts" in globs


def test_ensure_bug_hunt_chain_generates_valid_chain(tmp_path):
    chains = tmp_path / "Chains"
    workspace = tmp_path / "Workspace"
    proj = workspace / "cognispan"
    proj.mkdir(parents=True)
    (proj / "index.js").write_text("console.log('test');")

    note = project_chains.ensure_bug_hunt_chain("cognispan", chains_dir=chains, workspace=workspace)
    assert note.exists()
    assert note.name == "Bug hunt cognispan.md"

    # Verify that the generated note is a valid chain
    chain = cn.load_chain(note)
    assert chain.name == "Bug hunt cognispan"
    assert len(chain.steps) == 3   # Files (gather), First pass, Real issues
    wf = cn.to_workflow(chain)
    assert len(wf["steps"]) >= 3


def test_queue_bug_hunt(tmp_path):
    chains = tmp_path / "Chains"
    workspace = tmp_path / "Workspace"
    proj = workspace / "cognispan"
    proj.mkdir(parents=True)

    r = project_chains.queue_bug_hunt("cognispan", chains_dir=chains, workspace=workspace)
    assert r["ok"] is True
    assert "Queued" in r["why"]

    note = Path(r["note"])
    meta = cn.load_chain(note).meta
    assert meta.get("status") == "queued"
    assert meta.get("skip_wait") in (True, "true")


def test_find_latest_review(tmp_path):
    rev_dir = tmp_path / "Reviews"
    rev_dir.mkdir()
    (rev_dir / "2026-10-02 bug hunt speed-reading.md").write_text("Review 1")
    (rev_dir / "2026-10-04 bug hunt speed-reading.md").write_text("Review 2")

    res = project_chains.find_latest_review("speed-reading", reviews_dir=rev_dir)
    assert res is not None
    assert "2026-10-04" in res["name"]
    assert res["path"] == f"1Notebook/Reviews/{res['name']}"

    assert project_chains.find_latest_review("nonexistent", reviews_dir=rev_dir) is None


def test_hub_project_bughunt_action(tmp_path, monkeypatch):
    from sol_control_hud import hub
    monkeypatch.setattr(project_chains, "CHAINS_DIR", tmp_path / "Chains")
    monkeypatch.setattr(project_chains, "WORKSPACE_DIR", tmp_path / "Workspace")
    monkeypatch.setattr(project_chains, "REVIEWS_DIR", tmp_path / "Reviews")
    h = hub.Hub(dict(hub.DEFAULTS), False, False)
    r = h.do_action("project_bughunt", "mytestproj", {})
    assert r["ok"] is True
    assert "Queued" in r["why"]

    r2 = h.do_action("open_project_review", "mytestproj", {})
    assert r2["ok"] is False
    assert "No review note" in r2["why"]

