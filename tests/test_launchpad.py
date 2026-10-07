"""The Projects and Media APIs cards: what a project is and whether it runs, how the media services are looked at
without waking them, and the launcher's guard rails (names in, nothing else; never start what runs; never stop a job).

Nothing real is started, stopped or opened here: processes, WSL, HTTP and Explorer are all fakes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sol_control_hud import hub, launcher
from sol_control_hud.data.collectors import media_apis, projects
from sol_control_hud.views.web.app import Cached

OURS = {"X-SOL-Control": "1"}


# ---------------------------------------------------------------- fixtures
def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "Workspace"
    for name in ("alpha", "alpha-old", "beta", "loose", "retired-one", "omni", "media"):
        (ws / name).mkdir(parents=True)
    for name in ("alpha", "alpha-old", "beta", "retired-one", "omni", "media"):
        (ws / name / ".git").mkdir()
    (ws / "alpha" / "README.md").write_text("# Alpha App\n\n![badge](x.svg)\n\nAlpha does a useful thing for the tests. Then more.\n",
                                            encoding="utf-8")
    (ws / "alpha" / ".claude").mkdir()
    (ws / "alpha" / ".claude" / "launch.json").write_text(json.dumps(
        {"configurations": [{"name": "alpha", "runtimeExecutable": "bun", "runtimeArgs": ["run", "dev"], "port": 5173}]}))
    (ws / "retired-one" / "README.md").write_text("# Old\n\n> **Retired on 2026-09-13.**\n", encoding="utf-8")
    (ws / "omni" / "index.html").write_text("<html></html>")
    return ws


def make_reg(ws: Path, **media) -> dict:
    return {
        "workspace": str(ws), "editor": r"C:\fake\Code.exe", "wsl_distro": "Ubuntu-24.04",
        "hidden": {"retired-one": "retired"},
        "projects": {"omni": {"start": {"wsl": "cd /mnt/x && npm run dev"}, "stop": {"wsl": "pkill -f /mnt/x"},
                              "port": 8082, "runs_in": "wsl", "open_file": "index.html"},
                     "media": {"media": "svc"},
                     "beta": {"self": True}},
        "media": media or {"svc": {"title": "Svc", "project": "media", "port": 8080, "runs_in": "wsl", "unit": "svc",
                                   "start": {"shortcut": str(ws / "media" / "go.vbs")}, "shutdown": "/api/shutdown",
                                   "health": "/health", "jobs": "/jobs"}},
    }


def no_git(folder):
    return {"last_commit": 1.0, "subject": "s", "dirty_files": 0, "dirty_days": None, "github": None}


class FakePopen:
    calls: list = []

    def __init__(self, cmd, **kw):
        FakePopen.calls.append((cmd, kw))
        self.pid = 4242


@pytest.fixture(autouse=True)
def fresh_popen():
    FakePopen.calls = []


# ---------------------------------------------------------------- README, launch.json, git remote
def test_readme_title_description_and_status():
    info = projects.readme_info("# My Tool\n\n<div>logo</div>\n[![b](x)](y)\n\nIt **cuts** [video](u) fast on every machine. Second.\n")
    assert info == {"title": "My Tool", "description": "It cuts video fast on every machine.", "status": ""}


def test_status_only_from_headings_callouts_and_the_status_section():
    # cognispan, 10-07: a features table row "Paused tabs" made the card call the project paused
    assert projects.readme_status("# X\n\n| **Paused tabs** | the clock stops |\n") == ""
    assert projects.readme_status("# Suno\n\n## Project status\n\n> **Feature-complete prototype, paused.**\n") == "paused"
    assert projects.readme_status("# HUD\n\n> **Finished (tag `final`).**\n") == "finished"
    assert projects.readme_status("# Old\n\n> [!IMPORTANT]\n> **Retired on 2026-09-13**\n") == "retired"
    assert projects.readme_status("# T\n\nDEPRECATED words in a paragraph\n") == ""


def test_launch_config_reads_unescaped_windows_paths(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "launch.json").write_text(
        '{"configurations": [{"name": "c", "runtimeExecutable": "C:\\Users\\me\\bun.exe", "runtimeArgs": ["run"], "port": 3000}]}')
    assert projects.launch_config(tmp_path) == {"cmd": [r"C:\Users\me\bun.exe", "run"], "port": 3000, "name": "c"}
    assert projects.launch_config(tmp_path / "nope") is None


@pytest.mark.parametrize("remote,url", [
    ("https://github.com/Solopass/speedman.git", "https://github.com/Solopass/speedman"),
    ("git@github.com:Solopass/kiiy2k.git\n", "https://github.com/Solopass/kiiy2k"),
    ("https://gitlab.com/a/b", None), (None, None)])
def test_github_url(remote, url):
    assert projects.github_url(remote) == url


# ---------------------------------------------------------------- what runs
def test_listener_matches_its_folder_not_a_sibling_with_the_same_prefix(tmp_path):
    ws = make_ws(tmp_path)
    folders = {"alpha": ws / "alpha", "alpha-old": ws / "alpha-old"}
    paths = {1: [projects._norm(ws / "alpha-old")], 2: [projects._norm(ws / "alpha" / "node_modules" / "vite.js")],
             3: [projects._norm(ws / "alpha")]}
    rows = [{"pid": 1, "port": 1, "kind": "dev"}, {"pid": 2, "port": 2, "kind": "dev"},
            {"pid": 3, "port": 3, "kind": "system"}]           # Windows services are never a project
    found = projects.match_listeners(folders, rows, paths=lambda pid: paths[pid])
    assert [r["pid"] for r in found["alpha-old"]] == [1]
    assert [r["pid"] for r in found["alpha"]] == [2]


def test_project_rows(tmp_path):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    listening = [{"pid": 7, "port": 5173, "kind": "dev", "name": "bun", "label": "vite · alpha", "up_seconds": 60}]
    rows = projects.project_rows(reg, listening, {8082: True}, git=no_git,
                                 paths=lambda pid: [projects._norm(ws / "alpha")])
    by = {r["name"]: r for r in rows}
    assert "loose" not in by                               # not a repo, not in the registry: not a project
    a = by["alpha"]
    assert (a["title"], a["description"], a["port"]) == ("Alpha App", "Alpha does a useful thing for the tests.", 5173)
    assert a["running"]["pid"] == 7 and a["url"] == "http://127.0.0.1:5173/" and a["can_stop"]
    assert by["omni"]["running"] == {"port": 8082, "pid": None, "where": "wsl"} and by["omni"]["can_stop"]
    assert by["retired-one"]["hidden"] and by["retired-one"]["status"] == "retired"
    assert by["beta"]["self"] and not by["beta"]["can_start"]
    assert by["media"]["can_start"] and by["media"]["port"] == 8080    # from its media entry
    assert rows[-1]["name"] == "retired-one"                # hidden last
    assert [r["name"] for r in rows[:2]] == ["alpha", "omni"]       # running first


def test_port_taken_by_someone_else(tmp_path):
    ws = make_ws(tmp_path)
    listening = [{"pid": 9, "port": 5173, "kind": "dev", "name": "node", "label": "vite · other"}]
    rows = projects.project_rows(make_reg(ws), listening, {}, git=no_git, paths=lambda pid: [r"c:\elsewhere"])
    a = next(r for r in rows if r["name"] == "alpha")
    assert a["running"] is None and a["busy_by"] == "node · vite · other"


# ---------------------------------------------------------------- media: looking must not wake anything
def test_wsl_script_never_connects_to_a_service():
    """2026-10-07: a /health read right after a Stop landed in the shutdown second and systemd started it again."""
    media = {"m": {"runs_in": "wsl", "unit": "media-api", "port": 8080, "health": "/api/v1/health"},
             "w": {"runs_in": "windows", "port": 8765}}
    s = media_apis.wsl_script(media, [8082])
    assert "systemctl is-active media-api.service" in s and 'sport = :8082' in s
    assert "curl" not in s and "8080" not in s and "8765" not in s


def test_parse_wsl():
    out = "U media-api active active 1791000000\nU speedman inactive failed 0\nP 8082 1\nP 9 0\njunk\n"
    p = media_apis.parse_wsl(out)
    assert p["units"]["media-api"] == {"service": "active", "socket": "active", "since": 1791000000.0}
    assert p["units"]["speedman"] == {"service": "inactive", "socket": "failed", "since": None}
    assert p["ports"] == {8082: True, 9: False}


def test_probe_never_boots_wsl():
    def run(*a, **k):
        raise AssertionError("wsl.exe must not run while WSL is stopped")
    assert media_apis.wsl_probe({}, [], "U", "Stopped", run=run) == {"running": False, "units": {}, "ports": {}}


def write_jobs(path: Path, statuses):
    path.write_text(json.dumps([{"job_id": str(i), "type": "transcribe", "source": f"https://www.youtube.com/watch?v={i}",
                                 "status": s, "created_at": f"2026-10-0{i + 1}T10:00:00", "error": "boom" if s == "failed" else None}
                                for i, s in enumerate(statuses)]), encoding="utf-8")


def test_tile_states_and_busy_only_while_up(tmp_path):
    jobs = tmp_path / "jobs.json"
    write_jobs(jobs, ["completed", "failed", "running"])
    m = {"title": "Media", "port": 8080, "runs_in": "wsl", "unit": "media-api", "jobs_file": str(jobs),
         "shutdown": "/x", "jobs": "/j", "start": {"shortcut": "x"}}
    now = time.mktime((2026, 10, 7, 0, 0, 0, 0, 0, -1))
    asleep = media_apis.tile("media-api", m, {"running": False}, [], now)
    assert asleep["state"] == "asleep" and asleep["can_start"] and not asleep["can_live"]
    ready = media_apis.tile("media-api", m, {"running": True, "units": {"media-api": {"service": "inactive", "socket": "active"}}}, [], now)
    assert ready["state"] == "ready" and ready["active_jobs"] == 0      # a stale "running" in the file doesn't count
    up = media_apis.tile("media-api", m, {"running": True, "units": {"media-api": {"service": "active", "socket": "active", "since": now - 60}}}, [], now)
    assert up["state"] == "busy" and up["active_jobs"] == 1 and up["can_stop"] and up["can_live"]
    assert round(up["up_seconds"]) == 60
    assert up["recent"][0]["title"] == "youtube.com · watch?v=2" and up["week"] == {"completed": 1, "failed": 1, "running": 1}
    down = media_apis.tile("media-api", m, {"running": True, "units": {"media-api": {"service": "inactive", "socket": "failed"}}}, [], now)
    assert down["state"] == "down" and "failed" in down["note"]


def test_windows_service_is_matched_by_command_line(monkeypatch):
    class P:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["pythonw.exe", "-m", "vocal_savior", "app"] if self.pid == 1 else ["node", "other.js"]
    monkeypatch.setattr(media_apis.psutil, "Process", P)
    m = {"port": 8765, "runs_in": "windows", "match": "vocal_savior"}
    ours = media_apis.tile("vs", m, {}, [{"pid": 1, "port": 8765, "started": 100.0}], 160.0)
    assert ours["state"] == "up" and ours["up_seconds"] == 60
    other = media_apis.tile("vs", m, {}, [{"pid": 2, "port": 8765, "name": "node"}], 160.0)
    assert other["state"] == "busy_port" and "node" in other["note"] and not other["can_start"]


def test_voice_jobs_reads_the_db_read_only(tmp_path):
    db = tmp_path / "jobs.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, song TEXT, preset TEXT, status TEXT, stage TEXT, key_root TEXT,"
                " key_mode TEXT, bpm REAL, message TEXT, created_at REAL)")
    con.executemany("INSERT INTO jobs (song, preset, status, stage, key_root, key_mode, bpm, message, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    [("a", "pop", "finished", "done", "A", "minor", 92.4, "", 1000.0),
                     ("b", "rnb", "processing", "tuning", None, None, None, "", 2000.0)])
    con.commit()
    con.close()
    j = media_apis.voice_jobs(db, now=3000.0)
    assert j["active"] == 1 and j["total"] == 2 and j["week"] == {"finished": 1, "processing": 1}
    assert j["recent"][0]["title"] == "b" and j["recent"][1]["detail"] == "A minor · 92 bpm"
    assert media_apis.voice_jobs(tmp_path / "missing.sqlite")["recent"] == []


def test_running_ports_counts_a_socket_service_only_while_it_runs():
    reg = {"media": {"m": {"runs_in": "wsl", "unit": "m", "port": 8080}}}
    assert media_apis.running_ports(reg, {"ports": {8082: True}, "units": {"m": {"service": "inactive"}}}) == {8082: True, 8080: False}
    assert media_apis.running_ports(reg, {"units": {"m": {"service": "active"}}}) == {8080: True}


# ---------------------------------------------------------------- launcher
def test_child_env_drops_what_belongs_to_the_hud():
    env = launcher.child_env({"PORT": "7911", "SOL_CONTROL_DATA": "x", "VIRTUAL_ENV": "v", "PATH": "p", "Port": "1"})
    assert env == {"PATH": "p"}


def rows_for(ws, reg, **kw):
    return projects.project_rows(reg, kw.get("listening", []), kw.get("wsl_ports", {}), git=no_git,
                                 paths=kw.get("paths", lambda pid: [projects._norm(ws / "alpha")]))


def test_start_project_from_launch_json(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launcher, "resolve_exe", lambda n: r"C:\bun\bun.exe")
    monkeypatch.setenv("PORT", "7911")
    r = launcher.start_project(reg, "alpha", rows_for(ws, reg), popen=FakePopen)
    assert r["ok"] and r["pid"] == 4242 and r["auto_open"] and r["port"] == 5173
    cmd, kw = FakePopen.calls[0]
    assert cmd == [r"C:\bun\bun.exe", "run", "dev"] and kw["cwd"] == str(ws / "alpha")
    assert "PORT" not in kw["env"] and kw["creationflags"] & launcher.NO_WINDOW
    assert "started by SOL Control HUD" in (tmp_path / "logs" / "alpha.log").read_text()


def test_start_refuses_or_short_circuits(tmp_path):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    running = rows_for(ws, reg, listening=[{"pid": 7, "port": 5173, "kind": "dev", "name": "bun"}])
    r = launcher.start_project(reg, "alpha", running, popen=FakePopen)
    assert r["ok"] and r["url"] == "http://127.0.0.1:5173/" and not FakePopen.calls      # already running: just open
    taken = rows_for(ws, reg, listening=[{"pid": 9, "port": 5173, "kind": "dev", "name": "node", "label": ""}],
                     paths=lambda pid: [r"c:\elsewhere"])
    r = launcher.start_project(reg, "alpha", taken, popen=FakePopen)
    assert not r["ok"] and "taken by node" in r["why"] and not FakePopen.calls
    assert not launcher.start_project(reg, "beta", rows_for(ws, reg), popen=FakePopen)["ok"]       # the HUD itself
    with pytest.raises(launcher.LaunchError):
        launcher.start_project(reg, "..\\Windows", rows_for(ws, reg), popen=FakePopen)


def test_wsl_project_start_and_stop(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path / "logs")
    r = launcher.start_project(reg, "omni", rows_for(ws, reg), popen=FakePopen)
    assert r["ok"] and FakePopen.calls[0][0] == ["wsl.exe", "-d", "Ubuntu-24.04", "--exec", "bash", "-lc", "cd /mnt/x && npm run dev"]
    ran = []
    r = launcher.stop_project(reg, "omni", rows_for(ws, reg, wsl_ports={8082: True}), run=lambda cmd, **k: ran.append(cmd))
    assert r["ok"] and ran[0][-1] == "pkill -f /mnt/x"


def test_stop_windows_project_goes_through_close_listener(tmp_path):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    rows = rows_for(ws, reg, listening=[{"pid": 7, "port": 5173, "kind": "dev", "name": "bun"}])
    asked = []
    r = launcher.stop_project(reg, "alpha", rows, close=lambda port, pid: asked.append((port, pid)) or {"ok": True, "why": "closed"})
    assert r["ok"] and asked == [(5173, 7)]


def test_media_start_uses_the_shortcut(tmp_path):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    (ws / "media" / "go.vbs").write_text("'")
    opened = []
    tiles = [media_apis.tile("svc", reg["media"]["svc"], {"running": True, "units": {"svc": {"service": "inactive", "socket": "active"}}}, [])]
    r = launcher.start_project(reg, "media", rows_for(ws, reg), tiles, popen=FakePopen, opener=opened.append)
    assert r["ok"] and opened == [ws / "media" / "go.vbs"] and not FakePopen.calls and not r.get("auto_open")


class FakeHttp:
    def __init__(self, jobs=(), status=200, detail=None):
        self.jobs, self.status, self.detail, self.calls = list(jobs), status, detail, []

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return type("R", (), {"json": lambda s: self.jobs if "job" in url else {"active_jobs": 0}, "status_code": 200})()

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url))
        return type("R", (), {"status_code": self.status, "json": lambda s: {"detail": self.detail}})()


def tiles_for(reg, service):
    return [media_apis.tile(n, m, {"running": True, "units": {n: {"service": service, "socket": "active"}}}, [])
            for n, m in reg["media"].items()]


def test_stop_media_never_talks_to_a_sleeping_service(tmp_path):
    reg = make_reg(make_ws(tmp_path))
    http = FakeHttp()
    r = launcher.stop_media(reg, "svc", tiles_for(reg, "inactive"), http=http)
    assert r["ok"] and http.calls == []
    r = launcher.live_media(reg, "svc", tiles_for(reg, "inactive"), http=http)
    assert not r["ok"] and http.calls == []


def test_stop_media_relays_a_busy_refusal_and_checks_jobs_first(tmp_path):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    r = launcher.stop_media(reg, "svc", tiles_for(reg, "active"), http=FakeHttp(status=409, detail="2 job(s) running"))
    assert not r["ok"] and r["why"] == "2 job(s) running"
    # a service without its own busy check (speedman): the HUD asks its job list first
    reg2 = make_reg(ws, sp={"title": "Sp", "port": 8081, "runs_in": "wsl", "unit": "sp", "jobs": "/api/v1/jobs",
                            "shutdown": "/api/v1/shutdown"})
    http = FakeHttp(jobs=[{"status": "processing"}, {"status": "completed"}])
    r = launcher.stop_media(reg2, "sp", tiles_for(reg2, "active"), http=http)
    assert not r["ok"] and "1 job(s)" in r["why"] and ("POST", "http://127.0.0.1:8081/api/v1/shutdown") not in http.calls
    http = FakeHttp(jobs=[{"status": "completed"}])
    assert launcher.stop_media(reg2, "sp", tiles_for(reg2, "active"), http=http)["ok"]
    assert http.calls[-1] == ("POST", "http://127.0.0.1:8081/api/v1/shutdown")


def test_live_media_reads_jobs_and_health(tmp_path):
    reg = make_reg(make_ws(tmp_path))
    http = FakeHttp(jobs=[{"source": "https://www.youtube.com/watch?v=x", "type": "download", "status": "running", "progress_percent": 40}])
    r = launcher.live_media(reg, "svc", tiles_for(reg, "active"), http=http)
    assert r["ok"] and r["health"] == {"active_jobs": 0} and r["busy"] == 1
    assert r["jobs"][0]["title"] == "youtube.com · watch?v=x" and r["jobs"][0]["progress"] == 40


def test_open_project(tmp_path):
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    opened = []
    assert launcher.open_project(reg, "alpha", "folder", opener=opened.append)["ok"] and opened == [ws / "alpha"]
    assert launcher.open_project(reg, "omni", "file", opener=opened.append)["ok"] and opened[-1] == (ws / "omni" / "index.html").resolve()
    reg["projects"]["omni"]["open_file"] = "..\\alpha\\README.md"
    assert not launcher.open_project(reg, "omni", "file", opener=opened.append)["ok"]
    assert not launcher.open_project(reg, "..", "folder", opener=opened.append)["ok"]
    calls = []
    reg["editor"] = str(tmp_path / "Code.exe")
    (tmp_path / "Code.exe").write_text("")
    assert launcher.open_project(reg, "alpha", "editor", popen=lambda cmd, **k: calls.append(cmd))["ok"]
    assert calls == [[str(tmp_path / "Code.exe"), str(ws / "alpha")]]


# ---------------------------------------------------------------- the hub's endpoints
class FakeCollector:
    def __init__(self):
        from sol_control_hud.data.snapshot import Snapshot
        self.snap = Snapshot(sampled_at=1.0)
        self._sampler = type("S", (), {"latest": {"available": False}})()

    def get_snapshot(self):
        return self.snap


@pytest.fixture
def client_and_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "SETTINGS_FILE", tmp_path / "hub-settings.json")
    ws = make_ws(tmp_path)
    reg = make_reg(ws)
    monkeypatch.setattr(projects, "load_registry", lambda path=None: json.loads(json.dumps(reg)))
    monkeypatch.setattr(projects, "cached_git", no_git)
    h = hub.Hub(dict(hub.DEFAULTS), False, False)
    h.collector = FakeCollector()
    h.guard = hub.LazyGuard(h.collector._sampler)
    app = h.build_app()
    h.collectors["wsl"] = Cached(lambda: {"state": "Stopped"}, 60)          # never run wsl.exe in tests
    h.collectors["ports"] = Cached(lambda: {"listening": []}, 3)
    return TestClient(app), h, ws


def test_projects_and_media_endpoints(client_and_hub):
    client, h, ws = client_and_hub
    d = client.get("/api/projects").json()
    assert d["ok"] and {p["name"] for p in d["projects"]} >= {"alpha", "omni", "retired-one"}
    assert d["counts"]["hidden"] == 1
    m = client.get("/api/media").json()
    assert m["ok"] and m["tiles"][0]["state"] == "asleep" and m["wsl"] == "Stopped"
    assert client.get("/api/media-live?name=svc").json()["ok"] is False


def test_actions_need_our_header_and_go_by_name(client_and_hub, monkeypatch):
    client, h, ws = client_and_hub
    opened = []
    monkeypatch.setattr(launcher, "_open_path", opened.append)
    monkeypatch.setattr(launcher.open_project, "__defaults__", (launcher.subprocess.Popen, opened.append))
    assert client.post("/api/action", json={"action": "project", "target": "alpha", "op": "folder"}).status_code == 403
    r = client.post("/api/action", json={"action": "project", "target": "alpha", "op": "folder"}, headers=OURS).json()
    assert r["ok"] and opened == [ws / "alpha"]
    r = client.post("/api/action", json={"action": "project", "target": "alpha", "op": "format-c"}, headers=OURS).json()
    assert not r["ok"]
