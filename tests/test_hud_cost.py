"""The HUD's own CPU cost: one-call process snapshot, git only when a repo changed, router log only when it changed,
and the warning when the HUD itself burns CPU. All with fakes: no real git, psutil or process table."""
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

from sol_control_hud import health
from sol_control_hud.data import gitcache
from sol_control_hud.data.collectors import ai_ops, procsnap, projects


# ---------------------------------------------------------------- process snapshot
def _table(rows):
    """A fake SYSTEM_PROCESS_INFORMATION table in a buffer: rows of (pid, name, user_100ns, kernel_100ns, ws)."""
    size = ctypes.sizeof(procsnap._SPI)
    names = [ctypes.create_unicode_buffer(r[1]) for r in rows]
    buf = ctypes.create_string_buffer(size * len(rows))
    for i, (pid, name, user, kern, ws) in enumerate(rows):
        e = procsnap._SPI.from_address(ctypes.addressof(buf) + i * size)
        e.NextEntryOffset = size if i < len(rows) - 1 else 0
        e.UniqueProcessId, e.UserTime, e.KernelTime, e.WorkingSetSize = pid, user, kern, ws
        e.ImageName.Length = len(name) * 2
        e.ImageName.Buffer = ctypes.addressof(names[i])
    return buf, names, size * len(rows)


def test_procsnap_parse_reads_every_entry():
    buf, keep, total = _table([(4, "System", 10_000_000, 20_000_000, 1000), (77, "a.exe", 5_000_000, 0, 5 * 1024 * 1024)])
    assert procsnap.parse(buf, total) == [(4, "System", 3.0, 1000), (77, "a.exe", 0.5, 5 * 1024 * 1024)]


def test_top_processes_uses_snapshot_once_and_computes_cpu(monkeypatch):
    from sol_control_hud import hub
    calls = []
    t = {"cpu": 1.0}

    def fake():
        calls.append(1)
        return [(0, "Idle", 99.0, 8), (5, "a.exe", t["cpu"], 50 * 1024 * 1024), (6, "b.exe", 2.0, 5 * 1024 * 1024)]
    monkeypatch.setattr(procsnap, "snapshot", fake)
    clock = [100.0]
    monkeypatch.setattr(hub.time, "monotonic", lambda: clock[0])
    h = hub.Hub.__new__(hub.Hub)
    h.collectors = {"gpu": SimpleNamespace(get=lambda: {"pid_chips": {6: "GPU 0"}})}
    first = h._top_processes_payload()
    assert first["top_mem"][0]["pid"] == 5 and all(p["pid"] != 0 for p in first["top_cpu"])
    t["cpu"], clock[0] = 3.0, 104.0                       # 2 cpu-seconds in 4 s = 50 %
    second = h._top_processes_payload()
    assert second["top_cpu"][0] == {"pid": 5, "name": "a.exe", "cpu": 50.0, "mem_mb": 50.0, "chip": None}
    assert second["top_gpu"][0]["pid"] == 6 and len(calls) == 2


def test_top_processes_falls_back_to_psutil(monkeypatch):
    from sol_control_hud import hub

    def boom():
        raise OSError("blocked")
    monkeypatch.setattr(procsnap, "snapshot", boom)
    monkeypatch.setattr("sol_control_hud.hub._swallowed", lambda where: None)
    p = SimpleNamespace(info={"pid": 9, "name": "z.exe", "memory_info": SimpleNamespace(rss=1024 * 1024),
                              "cpu_times": SimpleNamespace(user=1.0, system=0.5)})
    monkeypatch.setattr("psutil.process_iter", lambda attrs=None: iter([p]))
    assert hub.Hub._process_rows() == [(9, "z.exe", 1.5, 1024 * 1024)]


# ---------------------------------------------------------------- git cache
def _repo(tmp_path, name="r"):
    r = tmp_path / name
    (r / ".git" / "refs" / "heads").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (r / ".git" / "index").write_bytes(b"x")
    (r / ".git" / "refs" / "heads" / "main").write_text("abc\n")
    return r


def _touch(p: Path, step=5):
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + step * 10**9))


def test_repo_cache_runs_only_when_repo_changes(tmp_path):
    r = _repo(tmp_path)
    cache = gitcache.RepoCache(300, lambda: 100.0)
    runs = []

    def compute():
        runs.append(1)
        return len(runs)
    assert cache.get(r, compute) == 1
    assert cache.get(r, compute) == 1 and len(runs) == 1
    _touch(r / ".git" / "index")                         # git add / status refresh
    assert cache.get(r, compute) == 2
    _touch(r / ".git" / "refs" / "heads" / "main")       # a commit moves the branch ref
    assert cache.get(r, compute) == 3
    _touch(r / ".git" / "HEAD")                          # checkout
    assert cache.get(r, compute) == 4


def test_repo_cache_safety_max_age(tmp_path):
    r = _repo(tmp_path)
    clock = [0.0]
    cache = gitcache.RepoCache(300, lambda: clock[0])
    n = []

    def compute():
        n.append(1)
        return len(n)
    cache.get(r, compute)
    clock[0] = 299
    assert cache.get(r, compute) == 1
    clock[0] = 301
    assert cache.get(r, compute) == 2


def test_repo_cache_watches_known_dirty_files(tmp_path):
    r = _repo(tmp_path)
    f = r / "a.py"
    f.write_text("1")
    cache = gitcache.RepoCache(300, lambda: 0.0)
    n = []

    def compute():
        n.append(1)
        return " M a.py\n"
    assert cache.get(r, compute, paths=gitcache.porcelain_paths) == " M a.py\n"
    cache.get(r, compute, paths=gitcache.porcelain_paths)
    assert len(n) == 1
    _touch(f)                                            # edited again
    cache.get(r, compute, paths=gitcache.porcelain_paths)
    assert len(n) == 2


def test_repo_cache_does_not_cache_odd_repos_or_failures(tmp_path):
    bare = tmp_path / "x"
    (bare / ".git").mkdir(parents=True)                  # empty .git: can't be fingerprinted
    cache = gitcache.RepoCache(300, lambda: 0.0)
    n = []
    cache.get(bare, lambda: n.append(1))
    cache.get(bare, lambda: n.append(1))
    assert len(n) == 2
    r = _repo(tmp_path, "ok")
    cache.get(r, lambda: n.append(1), keep=lambda v: False)      # git failed: not remembered
    cache.get(r, lambda: n.append(1), keep=lambda v: False)
    assert len(n) == 4


def test_cached_git_calls_git_info_once_until_changed(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    monkeypatch.setattr(projects, "_git_cache", gitcache.RepoCache(300, lambda: 0.0))
    calls = []

    def fake(folder, now=None):
        calls.append(1)
        return {"dirty_files": 0}, ""
    monkeypatch.setattr(projects, "_git_info", fake)
    assert projects.cached_git(r) == {"dirty_files": 0}
    assert projects.cached_git(r) == {"dirty_files": 0} and len(calls) == 1
    projects.forget_git("r")
    projects.cached_git(r)
    assert len(calls) == 2


def test_workspace_dirty_check_shares_one_git_run(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from sol_control_hud.data import snapshot
    r = _repo(tmp_path, "w")
    monkeypatch.setattr(snapshot.gitcache, "PORCELAIN", gitcache.RepoCache(300, lambda: 0.0))
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        return MagicMock(returncode=0, stdout=" M a.py\n")
    with patch("subprocess.run", side_effect=fake_run):
        assert snapshot.check_workspace_git(tmp_path)[:2] == (1, ["w"])
        assert snapshot.repo_porcelain(r) == " M a.py\n"       # the age lookup after it
        snapshot.check_workspace_git(tmp_path)
    assert len(runs) == 1


# ---------------------------------------------------------------- router log
def test_router_log_parsed_only_when_file_changes(tmp_path, monkeypatch):
    log = tmp_path / "router.log"
    log.write_text("proxying request to model sol-fast on port 8001\n")
    parses = []
    real = ai_ops.parse_router_events

    def counting(text):
        parses.append(1)
        return real(text)
    monkeypatch.setattr(ai_ops, "parse_router_events", counting)
    ai_ops._parsed.clear()
    a = ai_ops._router_events(log)
    b = ai_ops._router_events(log)
    assert a is b and len(parses) == 1 and a["proxied_model"] == "sol-fast"
    log.write_text("proxying request to model sol-smart on port 8002\n")   # new size
    assert ai_ops._router_events(log)["proxied_model"] == "sol-smart" and len(parses) == 2
    log.unlink()                                          # gone: empty parse, and no crash
    assert ai_ops._router_events(log)["active"] is False


# ---------------------------------------------------------------- over-target CPU warning
def test_cpu_watch_one_warning_per_episode():
    w = health.CpuWatch()
    T = 1.5
    for t in range(0, 600, 10):                          # hot (> 4.5 %) for just under 10 min: nothing
        assert not w.step(10.0, T, t)
    assert w.step(10.0, T, 600) is True                  # the 10th minute: one warning
    assert not any(w.step(10.0, T, t) for t in range(610, 4000, 10))     # staying hot: no repeat
    for t in range(4000, 4500, 10):                      # under target for only 8 min: not re-armed
        w.step(1.0, T, t)
    assert not w.armed
    assert not any(w.step(10.0, T, t) for t in range(4500, 6000, 10))
    for t in range(6000, 6700, 10):                      # 10+ min under target: re-armed
        w.step(1.0, T, t)
    assert w.armed
    assert not any(w.step(10.0, T, t) for t in range(6700, 7300, 10))
    assert w.step(10.0, T, 7300) is True                 # a new episode warns again


def test_cpu_watch_interrupted_hot_stretch_restarts_the_clock():
    w = health.CpuWatch()
    for t in range(0, 500, 10):
        assert not w.step(10.0, 1.5, t)
    assert not w.step(3.0, 1.5, 500)            # between 1x and 3x target: not hot, the 10 min start over
    for t in range(510, 1110, 10):
        assert not w.step(10.0, 1.5, t)
    assert w.step(10.0, 1.5, 1110) is True
    assert not health.CpuWatch().step(None, 1.5, 0)
