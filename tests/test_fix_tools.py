"""The fix lane (LOCAL_AI_CHAINS_V2_PLAN phase 2), in temp git repos with a scripted "model". Every attempt test also asserts
the main checkout is unchanged [R1]. Windows-only pieces (junctions) run on this PC."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sol_control_hud.chains import fix_tools as fx

REAL_GITLEAKS = fx.gitleaks_clean
GOOD = [{"file": "pkg/mod.py", "old": "return a - b", "new": "return a + b", "target_symbol": "", "replacement": ""}]
BAD = [{"file": "pkg/mod.py", "old": "return a - b", "new": "return a * b", "target_symbol": "", "replacement": ""}]


def run_git(cwd, *a):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}     # the pre-commit hook exports GIT_INDEX_FILE
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout


def make_repo(path: Path, conftest: str = "") -> Path:
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "__init__.py").write_text("")
    (path / "pkg" / "mod.py").write_text("def add(a, b):\n    return a - b\n\n\ndef other():\n    return 1\n")
    (path / "tests" / "test_mod.py").write_text("from pkg.mod import add\n\n\ndef test_add():\n    assert add(2, 0) == 2\n")
    (path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (path / "notes.md").write_text("hi\n")
    if conftest:
        (path / "tests" / "conftest.py").write_text(conftest)
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "user.email", "t@t")
    run_git(path, "config", "user.name", "t")
    run_git(path, "config", "core.autocrlf", "false")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "WORKTREES", tmp_path / "wt")
    monkeypatch.setattr(fx, "gitleaks_clean", lambda wt: True)
    main = make_repo(tmp_path / "main")
    repos = {"proj": {"repo": str(main), "stack": "win-python", "python": sys.executable, "pkgs": ["pkg"]}}
    monkeypatch.setattr(fx, "load_repos", lambda: repos)         # the ledger check trusts only allowlisted repos
    return main, repos, tmp_path


def scripted(*replies):
    it = iter(replies)
    seen = []

    def propose(messages):
        seen.append(messages)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps({"edits": r})
    propose.seen = seen
    return propose


def attempt(env, propose, task="fix add in mod.py", **kw):
    main, repos, _ = env
    fp = fx.fingerprint(main)
    res = fx.run_attempt("proj", task, propose, repos=repos, **kw)
    return res, fp == fx.fingerprint(main)


def no_leftovers(env):
    main, _, tmp = env
    assert [p for p in (tmp / "wt").glob("proj-*")] == []
    assert fx.ledger_read() == []
    assert run_git(main, "worktree", "list").count("\n") == 1


def branches(main):
    return [b.strip().lstrip("*+ ") for b in run_git(main, "branch", "--list", "ai/*").splitlines()]


# ---------------------------------------------------------------- the happy path and the failures
def test_good_edit_with_passing_tests_leaves_a_green_branch(env):
    main, _, _ = env
    res, untouched = attempt(env, scripted(GOOD))
    assert res["status"] == "green" and res["reason"] == "no regression" and untouched
    assert branches(main) == [res["branch"]]
    assert "return a + b" in run_git(main, "show", f"{res['branch']}:pkg/mod.py")
    assert "ai fix:" in run_git(main, "log", "-1", "--format=%s", res["branch"])
    assert "return a - b" in (main / "pkg" / "mod.py").read_text()        # the main checkout is untouched
    no_leftovers(env)


def test_failing_edit_leaves_no_branch_and_no_worktree(env):
    main, _, _ = env
    res, untouched = attempt(env, scripted(BAD, BAD))
    assert res["status"] == "failed" and res["reason"] == "test-error" and untouched
    assert branches(main) == []
    no_leftovers(env)


def test_retry_gets_the_failure_feedback_and_can_go_green(env):
    p = scripted(BAD, GOOD)
    res, _ = attempt(env, p)
    assert res["status"] == "green"
    assert "They failed" in p.seen[1][1]["content"] and "assert" in p.seen[1][1]["content"]


def test_exception_mid_run_cleans_up(env):
    main, _, _ = env
    with pytest.raises(RuntimeError):
        attempt(env, scripted(RuntimeError("model crashed")))
    assert branches(main) == []
    no_leftovers(env)


def test_same_task_twice_in_a_row_does_not_collide(env):
    main, _, _ = env
    a, _ = attempt(env, scripted(GOOD))
    b, _ = attempt(env, scripted(GOOD))
    assert a["status"] == b["status"] == "green" and a["branch"] != b["branch"]
    assert len(branches(main)) == 2


def test_red_baseline_stops_with_needs_you(env):
    main, _, _ = env
    (main / "tests" / "test_ok.py").write_text("def test_ok():\n    assert False\n")
    run_git(main, "commit", "-qam", "red")
    res, untouched = attempt(env, scripted(GOOD))
    assert res["status"] == "needs-you" and res["reason"] == "baseline-red" and untouched
    assert branches(main) == []
    no_leftovers(env)


def test_unknown_repo_and_unnamed_file_are_refused(env):
    _, repos, _ = env
    assert fx.run_attempt("nope", "fix mod.py", scripted(), repos=repos)["reason"] == "denied"
    res, _ = attempt(env, scripted(GOOD), task="make add work")
    assert res["reason"] == "no-match"
    no_leftovers(env)


def test_import_probe_stops_a_run_that_imports_the_main_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "WORKTREES", tmp_path / "wt")
    monkeypatch.setattr(fx, "gitleaks_clean", lambda wt: True)
    main = tmp_path / "main"
    conf = f"import sys\nsys.path.insert(0, {str(main)!r})\nimport pkg.mod\n"
    make_repo(main, conftest=conf)
    repos = {"proj": {"repo": str(main), "stack": "win-python", "python": sys.executable, "pkgs": ["pkg"]}}
    monkeypatch.setattr(fx, "load_repos", lambda: repos)
    res = fx.run_attempt("proj", "fix add in mod.py", scripted(GOOD), repos=repos)
    assert res["status"] == "needs-you" and res["reason"] == "import-escape"
    assert (tmp_path / "wt" / "active.json").exists() and fx.ledger_read() == []


def test_main_checkout_touched_during_the_attempt_is_needs_you(env):
    main, _, _ = env

    def meddling(messages):
        (main / "notes.md").write_text("changed behind its back\n")
        return json.dumps({"edits": GOOD})
    res, untouched = attempt(env, meddling)
    assert res["status"] == "needs-you" and res["reason"] == "main-touched" and not untouched
    assert branches(main) == []


# ---------------------------------------------------------------- edit policy
def test_old_text_must_match_exactly_once(env):
    zero = [{**GOOD[0], "old": "return nothing here"}]
    two = [{**GOOD[0], "old": "return"}]
    for edits in (zero, two):
        res, _ = attempt(env, scripted(edits, edits))
        assert res["status"] == "failed" and res["reason"] == "no-match"
    no_leftovers(env)


def test_crlf_file_keeps_crlf(env):
    main, _, _ = env
    (main / "pkg" / "mod.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    run_git(main, "commit", "-qam", "crlf")
    res, _ = attempt(env, scripted(GOOD))
    assert res["status"] == "green"
    blob = subprocess.run(["git", "show", f"{res['branch']}:pkg/mod.py"], cwd=main, capture_output=True,
                          env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")}).stdout
    assert blob == b"def add(a, b):\r\n    return a + b\r\n"


def test_target_symbol_replaces_a_whole_function(env):
    sym = [{"file": "pkg/mod.py", "old": "", "new": "", "target_symbol": "add",
            "replacement": "def add(a, b):\n    return a + b"}]
    res, _ = attempt(env, scripted(sym))
    assert res["status"] == "green"


@pytest.mark.parametrize("bad", ["../x.py", "C:/x.py", "/abs.py", "pkg/mod.py:stream", "pkg/mod.py.", "pkg/mod.py ", "pkg/../pkg/mod.py",
                                 "PKG~1/MOD.PY", "pkg/new_file.py", ".git/config", "notes.md", ""])
def test_rejected_paths(env, bad):
    main, _, _ = env
    run_git(main, "worktree", "add", "-q", "-b", "ai/t", str(env[2] / "w"))
    with pytest.raises(fx.Rejected) as e:
        fx.check_edit_path(env[2] / "w", bad, "fix mod.py")
    assert e.value.code == "denied"


def test_case_change_resolves_to_the_tracked_file_and_still_hits_the_rules(env):
    main, _, tmp = env
    run_git(main, "worktree", "add", "-q", "-b", "ai/t", str(tmp / "w"))
    assert fx.check_edit_path(tmp / "w", "PKG/MOD.PY", "fix mod.py").lower() == "pkg/mod.py"
    with pytest.raises(fx.Rejected):                       # Tests\ vs tests/ is the same test file
        fx.check_edit_path(tmp / "w", "TESTS\\test_mod.py", "fix mod.py")


def test_test_and_config_files_are_rejected_unless_a_test_is_named(env):
    main, _, tmp = env
    run_git(main, "worktree", "add", "-q", "-b", "ai/t", str(tmp / "w"))
    wt = tmp / "w"
    with pytest.raises(fx.Rejected):
        fx.check_edit_path(wt, "tests/test_mod.py", "fix mod.py")
    assert fx.check_edit_path(wt, "tests/test_mod.py", "fix tests/test_mod.py")   # full path named
    with pytest.raises(fx.Rejected):
        fx.check_edit_path(wt, "tests/test_mod.py", "fix test_mod.py")          # a bare file name is not enough
    for cfg in ("pyproject.toml",):
        with pytest.raises(fx.Rejected):
            fx.check_edit_path(wt, cfg, f"fix {cfg}")                           # config is never allowed, even when named
    for rel in (".githooks/pre-commit", "x/.github/ci.yml", "run.ps1", "a/sitecustomize.py", "b/pytest.ini", "tox.ini", "tests/conftest.py".replace("tests/", "")):
        assert fx.denied_by_name(rel) or fx.is_test_path(rel)


def test_symlink_or_junction_in_the_path_is_rejected(env):
    main, _, tmp = env
    run_git(main, "worktree", "add", "-q", "-b", "ai/t", str(tmp / "w"))
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("x = 1\n")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp / "w" / "pkg" / "link"), str(outside)], check=True, capture_output=True)
    with pytest.raises(fx.Rejected):
        fx.check_edit_path(tmp / "w", "pkg/link/evil.py", "fix evil.py")


def test_code_screen_rejects_rmtree_unless_the_task_names_it(env):
    evil = [{**GOOD[0], "new": "import shutil; shutil.rmtree('D:/x'); return a + b"}]
    res, _ = attempt(env, scripted(evil, evil))
    assert res["status"] == "failed" and res["reason"] == "unsafe-code"
    fx.screen_code("x = 1", "subprocess.run(['a'], timeout=5)", "add a timeout to subprocess.run in mod.py")   # named: passes
    with pytest.raises(fx.Rejected):
        fx.screen_code("x = 1", "open('f', 'w')", "fix mod.py")
    no_leftovers(env)


def test_size_cap_rejects_big_changes(env):
    big = [{**GOOD[0], "new": "\n".join(f"    x{i} = {i}" for i in range(100)) + "\n    return a + b"}]
    res, _ = attempt(env, scripted(big, big))
    assert res["reason"] == "too-big"
    four = [{"file": f, "old": o, "new": o + " ", "target_symbol": "", "replacement": ""} for f, o in
            [("pkg/mod.py", "return a - b"), ("pkg/mod.py", "return 1"), ("pkg/__init__.py", "")]]
    assert fx.parse_edits(json.dumps({"edits": four}))


def test_edits_are_all_or_nothing(env):
    main, _, tmp = env
    run_git(main, "worktree", "add", "-q", "-b", "ai/t", str(tmp / "w"))
    edits = [GOOD[0], {**GOOD[0], "old": "not there"}]
    with pytest.raises(fx.Rejected):
        fx.apply_edits(tmp / "w", edits, "fix mod.py")
    assert "return a - b" in (tmp / "w" / "pkg" / "mod.py").read_text()


def test_bad_json_is_rejected():
    for raw in ("not json", '{"edits": []}', '{"edits": [{"file": "a"}]}'):
        with pytest.raises(fx.Rejected):
            fx.parse_edits(raw)


# ---------------------------------------------------------------- "green" and the test runner
def test_green_means_no_regression():
    base = {"rc": 0, "has_summary": True, "passed": 5, "failed": 0, "errors": 0, "skipped": 1, "deselected": 0, "xfailed": 0}
    assert fx.is_green(base, dict(base))
    assert fx.is_green(base, {**base, "passed": 6})
    assert not fx.is_green(base, {**base, "passed": 4})
    assert not fx.is_green(base, {**base, "skipped": 2})
    assert not fx.is_green(base, {**base, "deselected": 1})
    assert not fx.is_green(base, {**base, "xfailed": 1})
    assert not fx.is_green(base, {**base, "has_summary": False})
    assert not fx.is_green(base, {**base, "rc": 1})


def test_parse_summary():
    s = fx.parse_summary("....\n===== 5 passed, 1 skipped, 2 deselected in 0.12s =====\nSOL_FIX_PROBE OK\n")
    assert (s["passed"], s["skipped"], s["deselected"], s["probe"], s["has_summary"]) == (5, 1, 2, "ok", True)
    assert not fx.parse_summary("collected 0 items\n")["has_summary"]


def test_child_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "x")
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_PAT", "x")
    monkeypatch.setenv("SOME_SECRET", "x")
    monkeypatch.setenv("GIT_INDEX_FILE", "x")
    out = fx.scrub_env(os.environ)
    assert not [k for k in out if k in ("MY_API_KEY", "GH_TOKEN", "GITHUB_PAT", "SOME_SECRET", "GIT_INDEX_FILE")]
    assert "PATH" in out or "Path" in out


def test_test_commands_come_from_the_stack_allowlist(env, tmp_path):
    wt = tmp_path / "wt1"
    wt.mkdir()
    argv, e = fx.build_test_command({"repo": r"D:\Workspace\media-api", "stack": "wsl-python", "pkgs": ["app"]}, wt, "r1")
    assert argv[0] == "wsl.exe" and "pkill" not in argv and "timeout" in argv and "solfix-r1" in " ".join(argv)
    assert "PYTHONPATH/p" in e["WSLENV"] and "SOL_FIX_WORKTREE/p" in e["WSLENV"]
    assert argv[-1].startswith("cache_dir=solfix-")
    argv, e = fx.build_test_command({"repo": r"D:\Workspace\edit-api", "stack": "uv", "pkgs": ["edit_api"]}, wt, "r2")
    assert argv[:2] == ["uv", "run"] and "--frozen" in argv and "--no-sync" in argv
    with pytest.raises(fx.FixError):
        fx.build_test_command({"repo": "x", "stack": "powershell -c evil"}, wt, "r3")


def test_abort_mid_test_kills_the_test_removes_the_worktree_and_gives_the_attempt_back(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "WORKTREES", tmp_path / "wt")
    monkeypatch.setattr(fx, "gitleaks_clean", lambda wt: True)
    main = make_repo(tmp_path / "main")
    (main / "tests" / "test_slow.py").write_text("import time\n\n\ndef test_slow():\n    time.sleep(60)\n")
    run_git(main, "add", "-A"); run_git(main, "commit", "-qm", "slow")
    repos = {"proj": {"repo": str(main), "stack": "win-python", "python": sys.executable, "pkgs": ["pkg"]}}
    monkeypatch.setattr(fx, "load_repos", lambda: repos)
    queue = tmp_path / "q.md"
    queue.write_text("- [ ] [attempts: 0/2] proj: fix add in mod.py\n", encoding="utf-8")
    t0 = time.time()
    task = fx.parse_queue(queue.read_text())[0]
    counted = fx.bump_attempt(queue, task)
    assert "attempts: 1/2" in queue.read_text()                 # counted BEFORE it starts
    res = fx.run_attempt("proj", task["task"], scripted(GOOD), abort=lambda: time.time() - t0 > 4, repos=repos)
    assert res["status"] == "aborted" and time.time() - t0 < 40
    fx.settle(queue, counted, res)
    assert "attempts: 0/2" in queue.read_text()                 # an interruption is not a failure
    assert run_git(main, "branch", "--list", "ai/*") == ""
    assert list((tmp_path / "wt").glob("proj-*")) == [] and fx.ledger_read() == []


def test_junction_is_unlinked_before_remove_and_main_node_modules_survives(env):
    main, _, tmp = env
    nm = main / "node_modules"
    nm.mkdir()
    (nm / "keep.txt").write_text("precious")
    entry = fx.open_worktree("proj", env[1]["proj"], "junction test")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(Path(entry["path"]) / "node_modules"), str(nm)], check=True, capture_output=True)
    fx.discard_worktree(entry)
    assert (nm / "keep.txt").read_text() == "precious"
    assert not Path(entry["path"]).exists()


@pytest.mark.skipif(not __import__("shutil").which("gitleaks"), reason="gitleaks not installed")
def test_gitleaks_blocks_a_planted_secret(env, monkeypatch):
    main, repos, tmp = env
    monkeypatch.setattr(fx, "gitleaks_clean", REAL_GITLEAKS)      # the env fixture stubs it out
    secret = [{**GOOD[0], "new": 'return a + b  # ghp_' + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"}]
    res = fx.run_attempt("proj", "fix add in mod.py", scripted(secret), repos=repos)
    assert res["status"] == "failed" and res["reason"] == "gitleaks"
    assert run_git(main, "branch", "--list", "ai/*") == ""


# ---------------------------------------------------------------- ledger, scrub, queue
def test_ledger_is_written_before_the_worktree_exists(env, monkeypatch):
    seen = {}
    orig = fx.git

    def spy(cwd, *a, **k):
        if a[:2] == ("worktree", "add"):
            seen["ledger"] = [e["path"] for e in fx.ledger_read()]
        return orig(cwd, *a, **k)
    monkeypatch.setattr(fx, "git", spy)
    entry = fx.open_worktree("proj", env[1]["proj"], "ledger first")
    assert entry["path"] in seen["ledger"]
    fx.discard_worktree(entry)


def test_scrub_removes_old_ledger_worktrees_and_only_their_branches(env):
    main, repos, tmp = env
    old = fx.open_worktree("proj", repos["proj"], "orphan one")
    fresh = fx.open_worktree("proj", repos["proj"], "fresh one")
    run_git(main, "branch", "ai/someone-elses")                      # not in the ledger: must survive
    fx.ledger_update(old["path"], started=time.time() - 13 * 3600, test_pid=os.getpid(), test_pid_created=1.0)  # PID reused: wrong creation time
    removed = fx.scrub_orphans()
    assert removed == [old["path"]]
    assert not Path(old["path"]).exists() and Path(fresh["path"]).exists()
    names = branches(main)
    assert "ai/someone-elses" in names and old["branch"] not in names and fresh["branch"] in names
    fx.discard_worktree(fresh)


def test_pid_reuse_is_guarded():
    assert not fx._proc_matches(os.getpid(), 1.0)
    import psutil
    assert fx._proc_matches(os.getpid(), psutil.Process(os.getpid()).create_time())


def test_queue_counter_moves_to_unresolved_after_two_failures(tmp_path):
    q = tmp_path / "q.md"
    q.write_text("# Q\n\n## Open\n- [ ] [attempts: 0/2] proj: fix add in mod.py\n\n## Unresolved\n", encoding="utf-8")
    t = fx.parse_queue(q.read_text())[0]
    c1 = fx.bump_attempt(q, t)
    fx.settle(q, c1, {"status": "failed", "reason": "test-error"})
    assert "[attempts: 1/2]" in q.read_text()
    t = fx.parse_queue(q.read_text())[0]
    c2 = fx.bump_attempt(q, t)
    fx.settle(q, c2, {"status": "failed", "reason": "no-match"})
    text = q.read_text()
    assert "[attempts" not in text and "[failed: no-match]" in text.split("## Unresolved")[1]
    assert fx.parse_queue(text) == []


def test_queue_green_line_and_counter_survives_a_crash(tmp_path):
    q = tmp_path / "q.md"
    q.write_text("- [ ] [attempts: 0/2] proj: fix add in mod.py\n", encoding="utf-8")
    t = fx.parse_queue(q.read_text())[0]
    fx.bump_attempt(q, t)                                            # ...and the daemon dies here: nothing settles
    assert fx.parse_queue(q.read_text())[0]["attempts"] == 1
    t = fx.parse_queue(q.read_text())[0]
    fx.settle(q, fx.bump_attempt(q, t), {"status": "green", "branch": "ai/x"})
    assert q.read_text().startswith("- [x] proj: fix add in mod.py -> branch ai/x")


def test_queue_never_overwrites_a_newer_edit(tmp_path):
    q = tmp_path / "q.md"
    q.write_text("- [ ] [attempts: 0/2] proj: a.py\n", encoding="utf-8")

    def racing(text):
        q.write_text(text + "- [ ] [attempts: 0/2] proj: you added this in Obsidian\n", encoding="utf-8")
        os.utime(q, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        return text.replace("0/2", "1/2")
    assert fx.edit_queue(q, racing, retries=1) is False
    assert "you added this" in q.read_text()


# ---------------------------------------------------------------- the chain tool end to end
def test_fix_attempts_tool_runs_the_queue_and_writes_a_report(env, monkeypatch):
    main, repos, tmp = env
    monkeypatch.setattr(fx, "load_repos", lambda: repos)
    saved = {}
    monkeypatch.setattr(fx.file_tools, "save_note", lambda a: saved.update(a) or {"path": "x"})
    q = tmp / "queue.md"
    q.write_text("- [ ] [attempts: 0/2] proj: fix add in mod.py\n- [ ] [attempts: 0/2] proj: fix other in mod.py\n", encoding="utf-8")
    out = fx.fix_attempts({"queue": str(q), "max_tasks": 1}, None, propose=scripted(GOOD))
    assert [a["status"] for a in out["attempts"]] == ["green"]
    text = q.read_text()
    assert text.splitlines()[0].startswith("- [x] proj: fix add in mod.py -> branch ai/")
    assert "[attempts: 0/2] proj: fix other" in text.splitlines()[1]              # max_tasks respected
    assert "GREEN" in saved["text"] and "no regression" in saved["text"].lower()
    assert "fix attempts" in saved["name"]


def test_tool_is_registered_and_the_note_parses():
    from sol_control_hud.chains import chain_daemon
    assert "fix_attempts" in chain_daemon.CHAIN_TOOLS()


def test_abort_flag_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "WORKTREES", tmp_path / "w")
    assert not fx.abort_requested()
    fx.request_abort()
    assert fx.abort_requested()
    fx.clear_abort()
    assert not fx.abort_requested()


def branches_all(main):
    return run_git(main, "branch", "--list").replace("*", " ").split()


def test_scrub_refuses_a_tampered_ledger_entry(env):
    main, repos, tmp = env
    victim = tmp / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    (tmp / "wt").mkdir(exist_ok=True)
    fx.ledger_write([{"path": str(victim), "branch": "ai/x", "repo": str(main), "started": 0},
                     {"path": str(tmp / "wt" / "proj-a"), "branch": "main", "repo": str(main), "started": 0},
                     {"path": str(tmp / "wt" / "proj-b"), "branch": "ai/x", "repo": str(tmp), "started": 0}])
    fx.scrub_orphans()
    assert (victim / "keep.txt").exists() and "main" in branches_all(main)
    assert fx.ledger_read() == []


def test_fingerprint_sees_tags_config_and_hooks(env):
    main, _, _ = env
    fp = fx.fingerprint(main)
    run_git(main, "tag", "sneaky")
    assert fx.fingerprint(main) != fp
    run_git(main, "tag", "-d", "sneaky")
    assert fx.fingerprint(main) == fp
    run_git(main, "config", "core.hooksPath", "evil")
    assert fx.fingerprint(main) != fp
    run_git(main, "config", "--unset", "core.hooksPath")
    (main / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    assert fx.fingerprint(main) != fp


@pytest.mark.parametrize("why, aborted", [("an Away queue job is running", False), ("the game guard has the AI off", True),
                                          ("waits until you've been idle 10 min (or Away mode)", True)])
def test_daemon_aborts_the_fix_lane_only_when_you_come_back(tmp_path, monkeypatch, why, aborted):
    from sol_control_hud.chains import chain_daemon
    monkeypatch.setattr(fx, "WORKTREES", tmp_path / "w")
    fake = type("D", (), {"current": {"lane": "away", "path": str(tmp_path / "n.md")},
                          "lane_open": lambda self, lane, force=False: (False, why)})()
    chain_daemon.Daemon._signal_fix_abort(fake)
    assert fx.abort_requested() is aborted


def test_an_interrupted_fix_lane_asks_the_runner_to_retry(env, monkeypatch):
    from sol_control_hud.chains.router import ModelUnavailable
    main, repos, tmp = env
    saved = {}
    monkeypatch.setattr(fx.file_tools, "save_note", lambda a: saved.update(a) or {"path": "x"})
    q = tmp / "queue.md"
    q.write_text("- [ ] [attempts: 0/2] proj: fix add in mod.py\n", encoding="utf-8")

    def interrupted(messages):
        raise fx.Aborted("you came back")
    with pytest.raises(ModelUnavailable):
        fx.fix_attempts({"queue": str(q), "max_tasks": 1}, None, propose=interrupted)
    assert "[attempts: 0/2]" in q.read_text()                     # the attempt was given back
    assert "interrupted" in saved["text"]                         # and the report still says what happened


def _wt_copy(env):
    main, repos, tmp = env
    wt = tmp / "wtcopy"
    shutil.copytree(main, wt)
    (wt / ".env").write_text("SECRET=1\n")
    run_git(wt, "add", "-f", ".env")                      # tracked but denied by name
    (wt / "pkg" / "mod.py").write_text("def add(a, b):\n    return a - b  # worktree copy\n")
    return main, wt


def _hit(path):
    return {"path": str(path), "start": 1, "end": 5, "score": 0.9, "snippet": "x", "mode": "hybrid"}


def test_task_files_falls_back_to_search_when_no_direct_name_match(env, monkeypatch):
    main, wt = _wt_copy(env)
    from sol_control_hud.chains import index_tools
    calls, notes = [], []
    monkeypatch.setattr(index_tools, "search", lambda q, project=None, k=3: calls.append((q, project, k)) or [
        _hit(str(main).upper() + "\\pkg\\mod.py")])      # absolute, repo folder in a different case
    files = fx.task_files(wt, "fix arithmetic addition in calculate", repo_key="proj", main=str(main), note=notes.append)
    assert list(files) == ["pkg/mod.py"]
    assert "worktree copy" in files["pkg/mod.py"]               # read from the worktree, not main
    assert calls == [("fix arithmetic addition in calculate", "proj", 3)]
    assert len(notes) == 1 and "pkg/mod.py" in notes[0]


def test_task_files_search_skips_outside_untracked_and_denied(env, monkeypatch, tmp_path):
    main, wt = _wt_copy(env)
    (wt / "scratch.py").write_text("x = 1\n")              # exists but untracked
    from sol_control_hud.chains import index_tools
    monkeypatch.setattr(index_tools, "search", lambda q, project=None, k=3: [
        _hit(tmp_path / "elsewhere" / "pkg" / "mod.py"),
        _hit(main / "scratch.py"),
        _hit(main / ".env"),
        _hit(str(main) + "-other/pkg/mod.py"),                 # sibling folder sharing the prefix
    ])
    assert fx.task_files(wt, "fix arithmetic", repo_key="proj", main=str(main)) == {}


def test_task_files_search_failure_means_no_fallback(env, monkeypatch):
    main, wt = _wt_copy(env)
    from sol_control_hud.chains import index_tools

    def boom(q, project=None, k=3):
        raise index_tools.IndexError_("no index at x")
    monkeypatch.setattr(index_tools, "search", boom)
    assert fx.task_files(wt, "fix arithmetic", repo_key="proj", main=str(main)) == {}


def test_task_files_direct_name_match_wins_and_skips_search(env, monkeypatch):
    main, wt = _wt_copy(env)
    from sol_control_hud.chains import index_tools
    called = []
    monkeypatch.setattr(index_tools, "search", lambda *a, **k: called.append(1) or [])
    files = fx.task_files(wt, "fix add in mod.py", repo_key="proj", main=str(main))
    assert list(files) == ["pkg/mod.py"] and called == []


def test_queue_status_and_add_and_reset_queue_task(tmp_path, monkeypatch):
    q = tmp_path / "Fix queue.md"
    q.write_text("""---
type: fix-queue
---
## Open
- [ ] [attempts: 1/2] media-api: timeout fix in chunk_service.py

## Green
- [x] speedman: handle missing file -> branch ai/20261028-handle-missing-120000

## Unresolved
- edit-api: add feature [failed: no-match]
""", encoding="utf-8")

    st = fx.queue_status(q)
    assert st["total_open"] == 1
    assert len(st["open"]) == 1
    assert st["open"][0]["repo"] == "media-api"
    assert len(st["green"]) == 1
    assert st["green"][0]["branch"] == "ai/20261028-handle-missing-120000"
    assert len(st["unresolved"]) == 1
    assert st["unresolved"][0]["reason"] == "no-match"

    # Add task
    monkeypatch.setattr(fx, "load_repos", lambda: {"media-api": {}, "speedman": {}})
    ok = fx.add_queue_task("media-api", "add timeout to second call", q)
    assert ok is True
    st2 = fx.queue_status(q)
    assert st2["total_open"] == 2
    assert st2["open"][0]["task"] == "add timeout to second call"

    # Reset unresolved task
    ok_reset = fx.reset_queue_task("edit-api", "add feature", q)
    assert ok_reset is True
    st3 = fx.queue_status(q)
    assert st3["total_open"] == 3
    assert len(st3["unresolved"]) == 0
