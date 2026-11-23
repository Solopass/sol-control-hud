"""Part B speed fixes (2026-11-22): each one keeps its behaviour while doing less work."""
from pathlib import Path
from types import SimpleNamespace

from sol_control_hud.data import gitcache
from sol_control_hud.data.collectors import network, projects


def test_one_process_scan_serves_every_folder(monkeypatch, tmp_path):
    calls = []

    class P:
        def __init__(self, pid, cwd, cmd):
            self.pid, self._cwd, self._cmd = pid, cwd, cmd
            self.info = {"name": "bun.exe"}

        def cwd(self): return self._cwd
        def cmdline(self): return self._cmd
        def create_time(self): return 0.0
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    procs = [P(1, str(a), ["bun", "run", "dev"]), P(2, str(b / "sub"), ["bun", "watch"])]
    monkeypatch.setattr(projects.psutil, "process_iter", lambda attrs=None: calls.append(1) or iter(procs))
    monkeypatch.setattr(projects, "_cand", (-1e9, []))
    assert projects.find_project_proc(a, "a-none")["pid"] == 1
    assert projects.find_project_proc(b, "b-none")["pid"] == 2
    assert projects.find_project_proc(tmp_path / "c", "c-none") is None
    assert len(calls) == 1                      # three folders, one scan


def test_readme_is_reread_only_when_it_changes(tmp_path, monkeypatch):
    f = tmp_path / "README.md"
    f.write_text("# One\n\nFirst description here for the card.\n")
    reads = []
    real = projects._readme_file
    monkeypatch.setattr(projects, "_readme_file", lambda p: reads.append(p) or real(p))
    assert projects.readme_info_for(tmp_path)["title"] == "One"
    assert projects.readme_info_for(tmp_path)["title"] == "One" and len(reads) == 1
    f.write_text("# Two!\n\nA longer description so the size changes too.\n")
    assert projects.readme_info_for(tmp_path)["title"] == "Two!" and len(reads) == 2


def test_origin_url_from_config_without_git(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "upstream"]\n\turl = https://x/up.git\n'
                                             '[remote "origin"]\n\turl = https://github.com/Solopass/x.git\n\tfetch = +refs\n')
    monkeypatch.setattr(projects, "_git", lambda *a: (_ for _ in ()).throw(AssertionError("no git process")))
    assert projects.origin_url(tmp_path) == "https://github.com/Solopass/x.git"
    (tmp_path / ".git" / "config").write_text("[include]\n\tpath = other\n")
    monkeypatch.setattr(projects, "_git", lambda *a: "https://via/git")
    assert projects.origin_url(tmp_path) == "https://via/git"       # includes: ask git


def _repo(tmp_path, name="r"):
    r = tmp_path / name
    (r / ".git" / "refs" / "heads").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (r / ".git" / "index").write_text("i")
    return r


def test_git_cache_survives_a_restart(tmp_path):
    r = _repo(tmp_path)
    f = tmp_path / "gitcache.json"
    one = gitcache.RepoCache(300)
    one.persist = f
    n = []
    assert one.get(r, lambda: n.append(1) or {"subject": "hi"}) == {"subject": "hi"}
    one.save()
    two = gitcache.RepoCache(300)
    two.persist = f
    assert two.get(r, lambda: n.append(1) or {"subject": "new"}) == {"subject": "hi"}    # no git after restart
    assert len(n) == 1
    (r / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
    three = gitcache.RepoCache(300)
    three.persist = f
    assert three.get(r, lambda: {"subject": "changed"}) == {"subject": "changed"}       # repo moved on: re-read


def test_windows_project_click_keeps_the_wsl_probes():
    from sol_control_hud.hub import Hub
    h = Hub.__new__(Hub)
    h.collectors = {k: SimpleNamespace(value="x") for k in ("ports", "media_probe", "projects", "media", "wsl")}
    h._refresh_launchpad(wsl=False)
    assert {k for k, c in h.collectors.items() if c.value is None} == {"ports", "projects", "media"}
    h._refresh_launchpad()
    assert all(c.value is None for c in h.collectors.values())


def test_open_folder_needs_no_card_rebuild(monkeypatch):
    from sol_control_hud import launcher
    from sol_control_hud.hub import Hub
    h = Hub.__new__(Hub)
    h.projects_payload = lambda: (_ for _ in ()).throw(AssertionError("no rebuild for opening"))
    monkeypatch.setattr(projects, "load_registry", lambda: {"projects": {}, "media": {}, "workspace": "x"})
    monkeypatch.setattr(launcher, "open_project", lambda reg, name, op: {"ok": True, "why": f"{op} {name}"})
    assert h._launch("project", "alpha", "folder") == {"ok": True, "why": "folder alpha"}


def test_tailscale_readers_share_one_answer(monkeypatch):
    t = {"now": 0.0}
    n = []
    monkeypatch.setattr(network, "_ts_status_cache", {"at": -1e9, "value": None})

    def fresh():
        n.append(1)
        v = {"state": "stopped", "n": len(n)}
        network._ts_status_cache.update(at=t["now"], value=v)
        return v
    monkeypatch.setattr(network, "tailscale_status", fresh)
    clock = lambda: t["now"]
    assert network.tailscale_status_cached(clock=clock)["n"] == 1
    t["now"] = 9
    assert network.tailscale_status_cached(clock=clock)["n"] == 1
    t["now"] = 11
    assert network.tailscale_status_cached(clock=clock)["n"] == 2


def test_entries_after_a_save_go_out_later(tmp_path):
    clock = [0.0]
    cache = gitcache.RepoCache(300, lambda: clock[0])
    cache.persist = tmp_path / "g.json"
    a, b = _repo(tmp_path, "a"), _repo(tmp_path, "b")
    cache.get(a, lambda: 1)                      # saved at once
    cache.get(b, lambda: 2)                      # inside the 30 s window: waits
    import json
    assert len(json.loads(cache.persist.read_text())["items"]) == 1
    clock[0] = 31
    cache.get(a, lambda: 1)                      # any later read flushes it
    assert len(json.loads(cache.persist.read_text())["items"]) == 2


def test_unreadable_readme_never_breaks_the_card(tmp_path):
    (tmp_path / "README.md").mkdir()                     # stats fine, can't be read (names are one file on Windows)
    assert projects.readme_info_for(tmp_path) == projects.readme_info("")


def test_a_stop_forgets_the_watcher_list(monkeypatch):
    monkeypatch.setattr(projects, "_cand", (10**12, [(1, "x", [], 0.0)]))
    projects.reset_candidates()
    assert projects._cand[1] == []
