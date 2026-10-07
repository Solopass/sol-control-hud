"""Start, stop and open projects and media services from the dashboard.

Same contract as the Localhost card's close button: the page sends a *name* and nothing else that matters. Commands,
folders and URLs are looked up here, in `launchpad.yaml` or the project's own `.claude/launch.json`, and the current
state is re-read before acting (already running -> say so; port taken by something else -> refuse).

Started processes are detached (no console window, a process group of their own, outside the HUD's job object if
Windows allows it), so they keep running when the HUD restarts. Their output goes to data/launch-logs/<name>.log.
Stopping a Windows dev server goes through `ports.close_listener`, which re-checks that it may be closed.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from .data.collectors import projects as projects_mod
from .paths import DATA_DIR

LOG_DIR = DATA_DIR / "launch-logs"
NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
BREAKAWAY = 0x01000000
BUN_FALLBACK = Path(os.environ.get("LOCALAPPDATA", "")) / ("Microsoft/WinGet/Packages/Oven-sh.Bun_Microsoft.Winget.Source_"
                                                           "8wekyb3d8bbwe/bun-windows-x64/bun.exe")
BUSY_STATUSES = {"queued", "running", "processing", "downloading", "transcribing", "pending"}
HTTP_TIMEOUT = 4.0


class LaunchError(Exception):
    pass


# ---------------------------------------------------------------- processes
def resolve_exe(name: str) -> str:
    """An absolute path for the first word of a command: as given if it exists, else from PATH, else bun's winget home
    (the HUD may run with a PATH that has no bun on it)."""
    if Path(name).is_absolute():
        if Path(name).exists():
            return name
        raise LaunchError(f"{name} doesn't exist")
    found = shutil.which(name)
    if found:
        return found
    if name.lower() in ("bun", "bun.exe") and BUN_FALLBACK.exists():
        return str(BUN_FALLBACK)
    raise LaunchError(f"can't find {name!r} on this PC")


LEAKY_ENV = {"PORT", "HOST", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"}


def child_env(env: dict | None = None) -> dict:
    """The HUD's environment minus what belongs to the HUD: an inherited PORT made a Bun dev server ignore its own
    --port (it took the HUD's), and the HUD's Python venv must not leak into another project's Python."""
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items() if k.upper() not in LEAKY_ENV and not k.upper().startswith("SOL_CONTROL")}


def spawn(cmd: list[str], cwd: Path | str | None, log_name: str, popen=subprocess.Popen) -> int:
    """Start a detached process with no window; stdout and stderr go to its log. Returns the pid."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{log_name}.log", "ab")       # noqa: SIM115 - handed to the child, closed below
    try:
        log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} started by SOL Control HUD: {' '.join(cmd)}\n".encode())
        log.flush()
        kw = dict(cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  close_fds=True, env=child_env())
        try:
            p = popen(cmd, creationflags=NO_WINDOW | NEW_GROUP | BREAKAWAY, **kw)
        except OSError:
            p = popen(cmd, creationflags=NO_WINDOW | NEW_GROUP, **kw)    # the job doesn't allow breakaway
        return p.pid
    finally:
        log.close()


def _wsl(script: str, distro: str) -> list[str]:
    return ["wsl.exe", "-d", distro, "--exec", "bash", "-lc", script]


def _open_path(path: Path) -> None:
    os.startfile(str(path))   # noqa: S606 - a path from the registry, never from the request


# ---------------------------------------------------------------- lookups
def _folder(reg: dict, name: str) -> Path:
    ws = Path(reg["workspace"])
    folder = (ws / name)
    try:
        folder.resolve().relative_to(ws.resolve())
    except ValueError as e:
        raise LaunchError("not a project folder") from e
    if not name or not folder.is_dir() or name.startswith("."):
        raise LaunchError(f"no project called {name!r}")
    return folder


def _row(rows: list[dict], name: str) -> dict:
    row = next((r for r in rows if r["name"] == name), None)
    if row is None:
        raise LaunchError(f"no project called {name!r}")
    return row


def _tile(tiles: list[dict], name: str) -> dict:
    t = next((t for t in tiles if t["name"] == name), None)
    if t is None:
        raise LaunchError(f"no media service called {name!r}")
    return t


def project_start_spec(reg: dict, name: str) -> dict | None:
    spec = reg["projects"].get(name) or {}
    if spec.get("start"):
        return spec["start"]
    launch = projects_mod.launch_config(_folder(reg, name))
    return {"cmd": launch["cmd"]} if launch else None


# ---------------------------------------------------------------- projects
def start_project(reg: dict, name: str, rows: list[dict], tiles: list[dict] | None = None, popen=subprocess.Popen,
                  opener=_open_path) -> dict:
    row = _row(rows, name)
    if row["self"]:
        return {"ok": False, "why": "that's this dashboard: it's already running"}
    if row["running"]:
        return {"ok": True, "why": f"{name} is already running on :{row['running']['port']}", "url": row["url"]}
    if row.get("media"):
        return start_media(reg, row["media"], tiles or [], rows, popen=popen, opener=opener)
    if row["busy_by"]:
        return {"ok": False, "why": f":{row['port']} is taken by {row['busy_by']} - close it first (Localhost card)"}
    spec = project_start_spec(reg, name)
    if not spec:
        return {"ok": False, "why": f"{name} has no start command (no .claude/launch.json and nothing in launchpad.yaml)"}
    return _run_spec(reg, name, spec, _folder(reg, name), popen, opener, port=row["port"])


def _run_spec(reg: dict, name: str, spec: dict, cwd: Path | None, popen, opener, port: int | None = None) -> dict:
    where = f" on :{port}" if port else ""
    if spec.get("shortcut"):
        target = Path(spec["shortcut"])
        if not target.exists():
            return {"ok": False, "why": f"{target} doesn't exist"}
        opener(target)
        return {"ok": True, "why": f"starting {name}{where}", "port": port}
    try:
        if spec.get("wsl"):
            cmd = _wsl(str(spec["wsl"]), reg.get("wsl_distro", "Ubuntu-24.04"))
        else:
            cmd = [resolve_exe(str(spec["cmd"][0])), *[str(a) for a in spec["cmd"][1:]]]
        pid = spawn(cmd, cwd, name, popen=popen)
    except LaunchError as e:
        return {"ok": False, "why": str(e)}
    except OSError as e:
        return {"ok": False, "why": f"couldn't start {name}: {e}"}
    return {"ok": True, "why": f"starting {name}{where} (log: data\\launch-logs\\{name}.log)", "pid": pid, "port": port,
            "auto_open": bool(port and not spec.get("opens_itself"))}     # the page opens it once the port answers


def stop_project(reg: dict, name: str, rows: list[dict], tiles: list[dict] | None = None, close=None,
                 run=subprocess.run, http=None) -> dict:
    row = _row(rows, name)
    if row["self"]:
        return {"ok": False, "why": "the dashboard can't stop itself from here (use Exit in the tray)"}
    if row.get("media"):
        return stop_media(reg, row["media"], tiles or [], rows, close=close, run=run, http=http)
    if not row["running"]:
        return {"ok": True, "why": f"{name} isn't running"}
    spec = reg["projects"].get(name) or {}
    if row["running"].get("pid"):
        if close is None:
            from .data.collectors.ports import close_listener as close
        return close(row["running"]["port"], row["running"]["pid"])
    if spec.get("stop", {}).get("wsl"):
        try:
            run(_wsl(str(spec["stop"]["wsl"]), reg.get("wsl_distro", "Ubuntu-24.04")), capture_output=True, timeout=10,
                creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "why": f"couldn't stop {name}: {e}"}
        return {"ok": True, "why": f"stopping {name}"}
    return {"ok": False, "why": f"don't know how to stop {name}"}


def open_project(reg: dict, name: str, how: str, popen=subprocess.Popen, opener=_open_path) -> dict:
    """Open the folder in Explorer, the project in VS Code, or its standalone file (`open_file`)."""
    try:
        folder = _folder(reg, name)
    except LaunchError as e:
        return {"ok": False, "why": str(e)}
    if how == "folder":
        opener(folder)
        return {"ok": True, "why": f"opened {folder}"}
    if how == "editor":
        editor = reg.get("editor") or "code"
        try:
            exe = resolve_exe(editor)
        except LaunchError as e:
            return {"ok": False, "why": str(e)}
        popen([exe, str(folder)], creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
        return {"ok": True, "why": f"opening {name} in VS Code"}
    if how == "file":
        rel = (reg["projects"].get(name) or {}).get("open_file")
        if not rel:
            return {"ok": False, "why": f"{name} has no file to open"}
        target = (folder / rel).resolve()
        try:
            target.relative_to(folder.resolve())
        except ValueError:
            return {"ok": False, "why": "that file is outside the project"}
        if not target.exists():
            return {"ok": False, "why": f"{rel} doesn't exist"}
        opener(target)
        return {"ok": True, "why": f"opened {rel}"}
    return {"ok": False, "why": f"can't open {how!r}"}


# ---------------------------------------------------------------- media services
def start_media(reg: dict, name: str, tiles: list[dict], rows: list[dict] | None = None, popen=subprocess.Popen,
                opener=_open_path) -> dict:
    m = (reg.get("media") or {}).get(name)
    if not m:
        return {"ok": False, "why": f"no media service called {name!r}"}
    t = next((t for t in tiles if t["name"] == name), None)
    if t and t["state"] in ("up", "busy"):
        return {"ok": True, "why": f"{t['title']} is already running", "url": t["url"]}
    if t and t["state"] == "busy_port":
        return {"ok": False, "why": t["note"]}
    if m.get("start"):
        cwd = _folder(reg, m["project"]) if m.get("project") else None
        return _run_spec(reg, name, m["start"], cwd, popen, opener, port=m.get("port"))
    if m.get("project"):
        spec = project_start_spec(reg, m["project"])
        if spec:
            return _run_spec(reg, m["project"], spec, _folder(reg, m["project"]), popen, opener, port=m.get("port"))
    return {"ok": False, "why": f"{name} has no start command"}


def _http():
    import httpx
    return httpx


def _busy(jobs) -> int:
    items = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    return sum(1 for j in items or [] if isinstance(j, dict) and str(j.get("status", "")).lower() in BUSY_STATUSES)


def stop_media(reg: dict, name: str, tiles: list[dict], rows: list[dict] | None = None, close=None,
               run=subprocess.run, http=None) -> dict:
    """Ask the service to shut down - only if it runs (a request to a sleeping socket would start it first) and
    never in the middle of a job. A WSL service's socket stays, so it starts again on first use."""
    m = (reg.get("media") or {}).get(name)
    if not m:
        return {"ok": False, "why": f"no media service called {name!r}"}
    t = _tile(tiles, name)
    if t["state"] not in ("up", "busy"):
        return {"ok": True, "why": f"{t['title']} isn't running"}
    if not m.get("shutdown") and m.get("project"):
        return stop_project(reg, m["project"], rows or [], tiles, close=close, run=run, http=http)
    if not m.get("shutdown"):
        return {"ok": False, "why": f"don't know how to stop {name}"}
    if m.get("jobs_db"):
        try:
            con = sqlite3.connect(f"file:{Path(m['jobs_db']).as_posix()}?mode=ro", uri=True, timeout=2)
            busy = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]
            con.close()
        except sqlite3.Error:
            busy = 0
        if busy:
            return {"ok": False, "why": f"{t['title']} has {busy} job(s) queued or running - not stopping it"}
    http = http or _http()
    base = f"http://127.0.0.1:{int(m['port'])}"
    try:
        if m.get("jobs") and not m.get("jobs_db") and not m.get("health"):
            busy = _busy(http.get(base + m["jobs"], timeout=HTTP_TIMEOUT).json())
            if busy:
                return {"ok": False, "why": f"{t['title']} has {busy} job(s) queued or running - not stopping it"}
        r = http.post(base + m["shutdown"], json={}, timeout=HTTP_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - any network failure is a message on the card
        return {"ok": False, "why": f"{t['title']} didn't answer: {type(e).__name__}"}
    if r.status_code == 409:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        return {"ok": False, "why": detail or f"{t['title']} is busy"}
    if r.status_code >= 400:
        return {"ok": False, "why": f"{t['title']} answered HTTP {r.status_code}"}
    return {"ok": True, "why": f"stopping {t['title']}" + (" (it starts again on first use)" if m.get("unit") else "")}


def _job_row(j: dict) -> dict:
    from .data.collectors.media_apis import _short_source
    title = j.get("source") or j.get("song") or j.get("input_name") or j.get("filename") or j.get("input") or j.get("id") or ""
    return {"title": _short_source(str(title))[:80], "what": str(j.get("type") or j.get("preset") or j.get("mode") or ""),
            "status": str(j.get("status") or ""),
            "progress": j.get("progress_percent", j.get("progress")),
            "detail": str(j.get("stage") or j.get("eta") or j.get("message") or "")[:80],
            "error": (str(j.get("error"))[:160] if j.get("error") else None)}


def live_media(reg: dict, name: str, tiles: list[dict], http=None) -> dict:
    """What the running service says right now (its job list, and media-api's health). Only for a service that's
    already up: this is the one place the card talks to the APIs on purpose, and it counts as activity for them."""
    m = (reg.get("media") or {}).get(name)
    if not m:
        return {"ok": False, "why": f"no media service called {name!r}"}
    t = _tile(tiles, name)
    if t["state"] not in ("up", "busy"):
        return {"ok": False, "why": f"{t['title']} isn't running: start it first (looking would start it anyway)"}
    http = http or _http()
    base = f"http://127.0.0.1:{int(m['port'])}"
    out: dict = {"ok": True, "name": name, "at": time.time()}
    try:
        if m.get("health"):
            out["health"] = http.get(base + m["health"], timeout=HTTP_TIMEOUT).json()
        if m.get("jobs"):
            jobs = http.get(base + m["jobs"], timeout=HTTP_TIMEOUT).json()
            items = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
            out["jobs"] = [_job_row(j) for j in (items or [])[:5] if isinstance(j, dict)]
            out["busy"] = _busy(jobs)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"{t['title']} didn't answer: {type(e).__name__}"}
    return out


def open_media_folder(reg: dict, name: str, opener=_open_path) -> dict:
    m = (reg.get("media") or {}).get(name) or {}
    if not m.get("folder"):
        return {"ok": False, "why": f"{name} has no output folder"}
    folder = Path(m["folder"])
    if not folder.exists():
        return {"ok": False, "why": f"{folder} doesn't exist yet"}
    opener(folder)
    return {"ok": True, "why": f"opened {folder}"}


# ---------------------------------------------------------------- recording or URL -> transcript note
# The Media card's box runs the existing transcript-note workflow (media-api transcribes, the local model summarizes,
# the note lands in D:\AI\Vault\Transcripts) as its own detached process, the same as
# `python -m sol_control_hud.chains note <source>`. Its progress is read back from the run database.
NOTE_WORKFLOW = "transcript-note"
NOTE_MEDIA_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mkv", ".webm", ".mov", ".wma"}


def check_note_source(source: str) -> str:
    """A web address or an audio/video file that exists. Raises LaunchError with a plain reason otherwise."""
    src = (source or "").strip().strip('"')
    if not src:
        raise LaunchError("paste a link or a file path first")
    if src.startswith("-") or "\n" in src or len(src) > 2000:
        raise LaunchError("that doesn't look like a link or a file path")
    if src.startswith(("http://", "https://")):
        if any(c.isspace() for c in src):
            raise LaunchError("a link can't contain spaces")
        return src
    p = Path(src)
    if not p.is_absolute() or not p.is_file():
        raise LaunchError(f"no such file: {src}")
    if p.suffix.lower() not in NOTE_MEDIA_EXTS:
        raise LaunchError(f"{p.suffix or 'that file'} isn't an audio or video file")
    return str(p)


def _pythonw() -> str:
    import sys
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def start_transcript_note(source: str, title: str = "", popen=subprocess.Popen) -> dict:
    try:
        src = check_note_source(source)
    except LaunchError as e:
        return {"ok": False, "why": str(e)}
    title = " ".join((title or "").split())[:120]
    from .paths import ROOT
    cmd = [_pythonw(), "-m", "sol_control_hud.chains", "note", src] + (["--title", title] if title else [])
    try:
        pid = spawn(cmd, ROOT, NOTE_WORKFLOW, popen=popen)
    except OSError as e:
        return {"ok": False, "why": f"couldn't start it: {e}"}
    return {"ok": True, "pid": pid,
            "why": "transcribing: the note lands in AI\\Vault\\Transcripts (progress below; a long video takes a while)"}


def _runs_db() -> Path:
    from .paths import ROOT
    return Path(os.environ.get("SOL_RUNS_DB", ROOT / "data" / "runs.sqlite"))


def note_runs(limit: int = 4, db: Path | None = None) -> list[dict]:
    """The latest transcript-note runs: what, how far, and the note it wrote. Read-only."""
    import json as _json
    p = Path(db or _runs_db())
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT id, status, inputs, outputs, error, created_at, finished_at FROM runs "
                               "WHERE workflow = ? ORDER BY id DESC LIMIT ?", (NOTE_WORKFLOW, limit)).fetchall()
            steps = {rid: con.execute("SELECT step, data FROM events WHERE run_id = ? AND kind = 'step_started' ORDER BY id DESC LIMIT 1",
                                      (rid,)).fetchone() for rid, *_ in rows}
        finally:
            con.close()
    except sqlite3.Error:
        return []
    from .data.collectors.media_apis import _short_source
    out = []
    for rid, status, inputs, outputs, error, created, finished in rows:
        try:
            i = _json.loads(inputs or "{}")
        except ValueError:
            i = {}
        try:
            o = _json.loads(outputs or "{}") or {}
        except ValueError:
            o = {}
        note = (o.get("note") or {}).get("path") if isinstance(o.get("note"), dict) else None
        tr = o.get("transcribe")
        title = i.get("title") or (tr.get("title") if isinstance(tr, dict) else None)
        step, item = None, None
        if status in ("queued", "running", "waiting") and steps.get(rid):
            step = steps[rid][0]
            try:
                item = (_json.loads(steps[rid][1] or "{}") or {}).get("item")   # "3/5" while it takes notes on part 3
            except (ValueError, AttributeError):
                item = None
        out.append({"id": rid, "status": status, "title": title or _short_source(i.get("source", "")),
                    "step": step, "item": item,
                    "note": Path(note).stem if note else None, "has_note": bool(note and Path(note).exists()),
                    "error": (error or "")[:200] or None, "at": created, "finished_at": finished})
    return out


def open_note_run(run_id, opener=_open_path, db: Path | None = None) -> dict:
    """Open the note a run wrote. The path comes from the run database, never from the page."""
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "which run?"}
    import json as _json
    p = Path(db or _runs_db())
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=2)
        row = con.execute("SELECT outputs FROM runs WHERE id = ? AND workflow = ?", (rid, NOTE_WORKFLOW)).fetchone()
        con.close()
    except sqlite3.Error:
        row = None
    try:
        note = ((_json.loads(row[0] or "{}") or {}).get("note") or {}).get("path") if row else None
    except (ValueError, AttributeError):
        note = None
    if not note or not Path(note).exists():
        return {"ok": False, "why": "that run has no note (yet)"}
    opener(Path(note))
    return {"ok": True, "why": f"opened {Path(note).name}"}
