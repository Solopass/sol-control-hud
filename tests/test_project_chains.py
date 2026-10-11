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
    # Review tasks parsing (- [ ] vs - [x])
    rev_file = rev_dir / "2026-10-05 bug hunt speedman.md"
    rev_file.write_text(
        "# Findings\n- [ ] **[high]** bug in api\n- [x] **[medium]** fixed crash\n- [ ] **[low]** typo",
        encoding="utf-8",
    )
    res_speedman = project_chains.find_latest_review("speedman", reviews_dir=rev_dir)
    assert res_speedman is not None
    assert res_speedman["tasks"]["total"] == 3
    assert res_speedman["tasks"]["open"] == 2
    assert res_speedman["tasks"]["resolved"] == 1
    items = res_speedman["tasks"]["items"]
    assert len(items) == 3
    assert items[0] == {"text": "**[high]** bug in api", "resolved": False, "severity": "high"}
    assert items[1] == {"text": "**[medium]** fixed crash", "resolved": True, "severity": "medium"}
    assert items[2] == {"text": "**[low]** typo", "resolved": False, "severity": "low"}


def test_toggle_review_task(tmp_path):
    f = tmp_path / "review.md"
    f.write_text(
        "# Findings\n- [ ] **[high]** bug in api\n- [x] **[medium]** fixed crash\n- [ ] **[low]** typo\n",
        encoding="utf-8",
    )
    # Toggle item 0 from open to resolved
    r = project_chains.toggle_review_task(f, 0, True)
    assert r["ok"] is True
    assert r["tasks"]["resolved"] == 2
    assert r["tasks"]["open"] == 1
    assert "- [x] **[high]** bug in api" in f.read_text(encoding="utf-8")

    # Toggle item 0 back to open
    r2 = project_chains.toggle_review_task(f, 0, False)
    assert r2["ok"] is True
    assert r2["tasks"]["resolved"] == 1
    assert r2["tasks"]["open"] == 2
    assert "- [ ] **[high]** bug in api" in f.read_text(encoding="utf-8")

    # Out of range index
    r_bad = project_chains.toggle_review_task(f, 99, True)
    assert r_bad["ok"] is False


def test_toggle_review_task_bullet_findings(tmp_path):
    f = tmp_path / "review_bullets.md"
    f.write_text(
        "# Findings\n- **[critical]** memory leak in parser\n- **[medium]** unhandled None\n",
        encoding="utf-8",
    )
    tasks = project_chains.parse_review_tasks(f)
    assert tasks["total"] == 2
    assert tasks["open"] == 2
    assert tasks["items"][0]["severity"] == "critical"

    # Toggle bullet item 0 to resolved
    r = project_chains.toggle_review_task(f, 0, True)
    assert r["ok"] is True
    assert "- [x] **[critical]** memory leak in parser" in f.read_text(encoding="utf-8")
    assert r["tasks"]["resolved"] == 1
    assert r["tasks"]["open"] == 1


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

    # Now test toggle_review_task via hub
    rev_dir = tmp_path / "Reviews"
    rev_dir.mkdir()
    (rev_dir / "2026-10-06 bug hunt mytestproj.md").write_text(
        "# Findings\n- [ ] **[high]** bug\n", encoding="utf-8"
    )
    r3 = h.do_action("toggle_review_task", "mytestproj", {"index": 0, "resolved": True})
    assert r3["ok"] is True
    assert r3["tasks"]["resolved"] == 1
    assert "- [x] **[high]** bug" in (rev_dir / "2026-10-06 bug hunt mytestproj.md").read_text(encoding="utf-8")


def test_parse_and_toggle_numbered_real_issues(tmp_path):
    f = tmp_path / "deep_review.md"
    f.write_text("""---
title: Deep Review
---
## Real issues
1.  **File: `metrics.py`**
    *   **Problem:** db.commit() sync issue
    *   **Fix:** Use a worker thread
2.  **File: `notify.py`**
    *   **Problem:** memory growth
""", encoding="utf-8")

    tasks = project_chains.parse_review_tasks(f)
    assert tasks["total"] == 2
    assert tasks["open"] == 2
    assert "metrics.py" in tasks["items"][0]["text"]
    assert "db.commit() sync issue" in tasks["items"][0]["text"]

    # Toggle item 0
    r = project_chains.toggle_review_task(f, 0, True)
    assert r["ok"] is True
    assert r["tasks"]["resolved"] == 1
    assert r["tasks"]["open"] == 1
    assert "1.  [x] **File: `metrics.py`**" in f.read_text(encoding="utf-8")

    # Toggle item 0 back
    r2 = project_chains.toggle_review_task(f, 0, False)
    assert r2["ok"] is True
    assert r2["tasks"]["resolved"] == 0
    assert "1.  [ ] **File: `metrics.py`**" in f.read_text(encoding="utf-8")



