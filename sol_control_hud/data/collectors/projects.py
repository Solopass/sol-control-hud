"""Projects in D:\\Workspace: what each one is, what state it's in, whether it runs right now, and how to start it.

Most of it is discovered from the folder (README, git, `.claude/launch.json`); `launchpad.yaml` only adds what can't
be (live sites, start commands for projects without a launch config, what to hide). A folder that isn't in the
registry still shows up.

Cost: git is three short processes per repo, so it only runs when the repo changed (data/gitcache.py; GIT_TTL_S at most); whether something runs comes from
the Localhost card's listener sample (no extra port scan) plus the media card's WSL probe for things that run in WSL.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import psutil
import yaml

from .. import gitcache

REGISTRY_FILE = Path(__file__).resolve().parents[2] / "launchpad.yaml"
NO_WINDOW = 0x08000000
GIT_TTL_S = gitcache.MAX_AGE_S      # safety net only: git re-runs sooner when the repo's files change
README_NAMES = ("README.md", "readme.md", "README.MD", "Readme.md")

# README words -> a status chip. First match wins, so the strongest word is first.
STATUS_WORDS = (
    ("deprecated", "deprecated"),
    ("retired", "retired"),
    ("archived", "retired"),
    ("finished (tag", "finished"),
    ("**finished", "finished"),
    ("paused", "paused"),
    ("on hold", "paused"),
)
GITHUB_RE = re.compile(r"github\.com[:/](?P<path>[^\s]+?)(?:\.git)?/?$")
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


# ---------------------------------------------------------------- registry
def load_registry(path: Path | str = REGISTRY_FILE, user_file: Path | str | None = None) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    data.setdefault("workspace", r"D:\Workspace")
    for key in ("hidden", "projects", "media"):
        data[key] = {str(k): (v if v is not None else {}) for k, v in (data.get(key) or {}).items()}
    data["user"] = load_user(user_file)
    return data


# ---------------------------------------------------------------- your own tags and hides (from the card)
# Machine state, not config: kept in data/ (git-ignored) and written only by the card's ⋯ menu. A choice here beats
# both the README's status words and launchpad.yaml's hidden list, so a project hidden there can be shown again.
TAGS = ("active", "paused", "idea", "finished", "archived", "deprecated")
FOLD_TAGS = {"archived", "deprecated"}      # tagging one of these also hides it (one click to show it again)


def user_file_path() -> Path:
    from ...paths import DATA_DIR
    return DATA_DIR / "launchpad-user.json"


def load_user(path: Path | str | None = None) -> dict:
    try:
        data = json.loads(Path(path or user_file_path()).read_text(encoding="utf-8"))
        return {str(k): v for k, v in (data.get("projects") or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_user(projects_: dict, path: Path | str | None = None) -> None:
    p = Path(path or user_file_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    clean = {k: v for k, v in sorted(projects_.items()) if v}
    tmp.write_text(json.dumps({"projects": clean}, indent=1), encoding="utf-8")
    tmp.replace(p)


def set_user(name: str, op: str, tag: str = "", path: Path | str | None = None,
             workspace: Path | str | None = None) -> dict:
    """The card's ⋯ menu: hide / show / tag / untag one project. `name` must be a folder in the workspace."""
    ws = Path(workspace or load_registry()["workspace"])
    if not name or name.startswith(".") or "/" in name or "\\" in name or not (ws / name).is_dir():
        return {"ok": False, "why": f"no project called {name!r}"}
    users = load_user(path)
    entry = dict(users.get(name) or {})
    if op == "hide":
        entry["hidden"], why = True, f"{name} hidden"
    elif op == "show":
        entry["hidden"], why = False, f"{name} shown again"
    elif op == "tag":
        if tag not in TAGS and tag != "":
            return {"ok": False, "why": f"unknown tag {tag!r}"}
        if tag:
            entry["tag"] = tag
        else:
            entry.pop("tag", None)
        why = f"{name}: {tag or 'tag cleared (README status again)'}"
        if tag in FOLD_TAGS:
            entry["hidden"], why = True, f"{name} tagged {tag} and hidden (+ hidden shows it)"
    else:
        return {"ok": False, "why": f"unknown change {op!r}"}
    users[name] = entry
    save_user(users, path)
    return {"ok": True, "why": why, "entry": entry}


# ---------------------------------------------------------------- README
def _readme(folder: Path) -> str:
    for name in README_NAMES:
        try:
            return (folder / name).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            continue
    return ""


def _plain(line: str) -> str:
    """Markdown -> plain words for a one-line description."""
    line = MD_LINK_RE.sub(r"\1", line)
    line = re.sub(r"[*_`]|<[^>]+>", "", line)
    return re.sub(r"\s+", " ", line).strip(" >-")


def readme_info(text: str) -> dict:
    """Title (first H1), a one-sentence description and a status word, from the top of a README."""
    title, desc = "", ""
    for raw in text.splitlines():
        line = raw.strip()
        if not title and line.startswith("# "):
            title = _plain(line[2:])
            continue
        if not title or desc or not line or line.startswith(("#", "!", "|", "<", "```", "---", "[!", "> [!")):
            continue
        words = _plain(line)
        if len(words) < 20 or words.lower().startswith(("try it", "live site")):
            continue
        cut = re.search(r"(?<=[a-z0-9)])[.!?](\s|$)", words)
        desc = words[:cut.end()].strip() if cut and cut.end() > 30 else words
        if len(desc) > 150:
            desc = desc[:147].rsplit(" ", 1)[0] + "…"
    return {"title": title, "description": desc, "status": readme_status(text)}


def readme_status(text: str) -> str:
    """A status word, read only where READMEs state status: headings, callouts (`> ...`) and the few lines under a
    "Project status" heading. Anywhere else "paused" is usually a feature ("paused tabs"), not the project."""
    lines, picked, under = text[:6000].lower().splitlines(), [], 0
    for line in lines:
        s = line.strip()
        if s.startswith("#"):
            under = 6 if "status" in s else 0
            picked.append(s)
        elif s.startswith(">") or under > 0:
            picked.append(s)
        under = max(0, under - 1) if not s.startswith("#") else under
    blob = "\n".join(picked)
    return next((st for word, st in STATUS_WORDS if word in blob), "")


# ---------------------------------------------------------------- launch.json
def _read_launch(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            data = json.loads(raw.replace("\\", "\\\\").replace("\\\\\\\\", "\\\\"))   # unescaped Windows paths
        except ValueError:
            return []
    return [c for c in (data.get("configurations") or []) if isinstance(c, dict)]


def _entry(c: dict) -> dict | None:
    exe, port = c.get("runtimeExecutable"), c.get("port")
    if exe and port:
        return {"cmd": [str(exe), *[str(a) for a in c.get("runtimeArgs") or []]], "port": int(port),
                "name": c.get("name", "")}
    return None


def _points_into(c: dict, folder: Path) -> bool:
    """An entry of the workspace launch.json belongs to `folder` when one of its arguments is that folder or a path
    inside it (`--cwd=D:/Workspace/x`, `--manifest-path D:/Workspace/x/Cargo.toml`)."""
    root = _norm(folder)
    for a in c.get("runtimeArgs") or []:
        v = str(a).split("=", 1)[1] if str(a).startswith("--") and "=" in str(a) else str(a)
        n = _norm(v)
        if n == root or n.startswith(root + "\\"):
            return True
    return False


def launch_config(folder: Path) -> dict | None:
    """The first dev-server entry of the project's own `.claude/launch.json` (what preview_start uses); else the first
    entry of the workspace's `.claude/launch.json` that points into the folder (most preview configs live there, so
    chibi-mountain-rush, undergrid, temaricad... had no Start, 2026-11-19)."""
    for c in _read_launch(folder / ".claude" / "launch.json"):
        e = _entry(c)
        if e:
            return e
    for c in _read_launch(folder.parent / ".claude" / "launch.json"):
        e = _entry(c)
        if e and _points_into(c, folder):
            return e
    return None


# ---------------------------------------------------------------- git
def _git(folder: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(folder), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def github_url(remote: str | None) -> str | None:
    m = GITHUB_RE.search((remote or "").strip())
    return f"https://github.com/{m.group('path')}" if m else None


def git_info(folder: Path, now: float | None = None) -> dict | None:
    """Last commit, uncommitted files and how old the oldest of them is, the GitHub link. None if not a repo."""
    return _git_info(folder, now)[0]


def _git_info(folder: Path, now: float | None = None) -> tuple[dict | None, str]:
    """(info, the `git status --porcelain` text it was built from)."""
    if not (folder / ".git").exists():
        return None, ""
    from ..snapshot import dirty_age_days
    now = time.time() if now is None else now
    log = (_git(folder, "log", "-1", "--format=%ct%x09%s") or "").strip()
    porcelain = _git(folder, "status", "--porcelain") or ""
    remote = _git(folder, "config", "--get", "remote.origin.url")
    commit_t, _, subject = log.partition("\t")
    files = [ln for ln in porcelain.splitlines() if ln.strip()]
    return ({"last_commit": float(commit_t) if commit_t.isdigit() else None, "subject": subject[:120],
             "dirty_files": len(files), "dirty_days": dirty_age_days(folder, porcelain, now) if files else None,
             "github": github_url(remote)}, porcelain)


_git_cache = gitcache.RepoCache(GIT_TTL_S, background=True)   # an old entry never makes a click wait for git


def cached_git(folder: Path, max_age: float = GIT_TTL_S) -> dict | None:
    """git_info(), re-run only when the repo changed (data/gitcache.py) or after `max_age` seconds."""
    _git_cache.max_age = max_age
    box: dict = {}

    def run():
        box["info"], box["porcelain"] = _git_info(folder)
        return box["info"]
    return _git_cache.get(folder, run, paths=lambda _v: gitcache.porcelain_paths(box.get("porcelain", "")),
                          keep=lambda v: v is not None)


def forget_git(name: str | None = None) -> None:
    _git_cache.forget(name)


# ---------------------------------------------------------------- what runs
def url_host(addrs) -> str:
    """The host to open a running server at. Vite's default `localhost` binds only IPv6 [::1] on this PC, where
    http://127.0.0.1 gets no answer (speed-reading's Open did nothing, 2026-11-19); `localhost` reaches either."""
    a = set(addrs or [])
    if not a or a & {"127.0.0.1", "0.0.0.0"}:
        return "127.0.0.1"
    return "localhost"


def _norm(p: str) -> str:
    return str(p).replace("/", "\\").rstrip("\\").lower()


def _proc_paths(pid: int) -> list[str]:
    """The process's working folder and command-line arguments, for matching it to a project folder."""
    out: list[str] = []
    try:
        p = psutil.Process(pid)
    except (psutil.Error, OSError):
        return out
    for get in (lambda: [p.cwd()], p.cmdline):
        try:
            out += [_norm(x) for x in get() if x]
        except (psutil.Error, OSError):
            continue
    return out


def match_listeners(folders: dict[str, Path], listening: list[dict], paths=_proc_paths) -> dict[str, list[dict]]:
    """Listeners whose process works in (or runs a file from) a project folder -> {project: [rows]}.
    `D:\\Workspace\\kiiy2k` matches itself and what's under it, never `D:\\Workspace\\kiiy2k-old`."""
    found: dict[str, list[dict]] = {}
    markers = {name: _norm(folder) for name, folder in folders.items()}
    for row in listening:
        if row.get("kind") not in ("dev", "app", "self"):
            continue
        where = paths(row["pid"])
        for name, marker in markers.items():
            if any(w == marker or w.startswith(marker + "\\") for w in where):
                found.setdefault(name, []).append(row)
                break
    return found


def find_project_proc(folder: Path, name: str) -> dict | None:
    """Check if a background watcher or runner is active for a project without a port."""
    from ...paths import DATA_DIR
    pid_file = DATA_DIR / "launch-pids" / f"{name}.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            p = psutil.Process(pid)
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                up_s = max(1, int(time.time() - p.create_time()))
                return {"port": None, "pid": pid, "where": "windows", "up_seconds": up_s, "kind": "watcher"}
        except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
            try:
                pid_file.unlink()
            except OSError:
                pass

    marker = _norm(str(folder))
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p_name = (p.name() or "").lower()
            if not any(k in p_name for k in ("bun", "node", "powershell", "cmd", "python")):
                continue
            cwd = _norm(p.cwd()) if p.cwd() else ""
            if cwd == marker or cwd.startswith(marker + "\\"):
                cmd = [str(x).lower() for x in p.cmdline() if x]
                if any("build.ts" in x or "dev" in x or "watch" in x for x in cmd):
                    up_s = max(1, int(time.time() - p.create_time()))
                    return {"port": None, "pid": p.pid, "where": "windows", "up_seconds": up_s, "kind": "watcher"}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


# ---------------------------------------------------------------- the card
def _media_entry(reg: dict, name: str) -> dict | None:
    m = (reg["projects"].get(name) or {}).get("media")
    return reg["media"].get(m) if m else None


def project_rows(reg: dict, listening: list[dict] | None = None, wsl_ports: dict[int, bool] | None = None,
                 workspace: Path | None = None, git=cached_git, paths=_proc_paths,
                 find_proc=find_project_proc) -> list[dict]:
    """One row per folder in the workspace, most recently worked on first."""
    ws = Path(workspace or reg["workspace"])
    try:
        folders = {d.name: d for d in ws.iterdir() if d.is_dir() and not d.name.startswith(".")}
    except OSError:
        return []
    listening = listening or []
    wsl_ports = wsl_ports or {}
    running = match_listeners(folders, listening, paths)
    by_port = {r["port"]: r for r in listening}
    rows = []
    for name, folder in folders.items():
        spec = reg["projects"].get(name) or {}
        media = _media_entry(reg, name)
        is_repo = (folder / ".git").exists()
        if not is_repo and name not in reg["projects"] and name not in reg["hidden"]:
            continue                                   # a loose folder, not a project
        info = readme_info(_readme(folder))
        launch = launch_config(folder)
        start = spec.get("start") or ({"cmd": launch["cmd"]} if launch else None)
        port = spec.get("port") or (launch or {}).get("port") or (media or {}).get("port")
        runs_in = spec.get("runs_in") or (media or {}).get("runs_in") or "windows"
        g = git(folder) if is_repo else None

        # Sidecars: extra helper processes of a project with their own Start / Stop on its row (e.g. GatKam's lag
        # logger). Their ports never count as the project itself running.
        sidecar_specs = spec.get("sidecars") or {}
        sidecar_ports = {int(s["port"]) for s in sidecar_specs.values() if s.get("port")}
        own_listeners = running.get(name) or []
        sidecars = []
        for sc_name, sc in sidecar_specs.items():
            sc_port = int(sc["port"]) if sc.get("port") else None
            own = next((r for r in own_listeners if sc_port and r["port"] == sc_port), None)
            other = by_port.get(sc_port) if (sc_port and not own) else None
            sidecars.append({"name": sc_name, "title": sc.get("title") or sc_name, "port": sc_port,
                             "running": {"pid": own["pid"], "port": sc_port} if own else None,
                             "busy_by": other.get("name", "?") if other else None})

        run = None
        if runs_in == "wsl":
            if port and wsl_ports.get(int(port)):
                run = {"port": int(port), "pid": None, "where": "wsl"}
        else:
            mine = [r for r in own_listeners if r["port"] not in sidecar_ports]
            pick = next((r for r in mine if r["port"] == port), mine[0] if mine else None)
            if pick:
                run = {"port": pick["port"], "pid": pick["pid"], "where": "windows", "up_seconds": pick.get("up_seconds"),
                       "host": url_host(pick.get("addrs"))}
            elif (not port or spec.get("watcher")) and find_proc:
                run = find_proc(folder, name)
        busy = None
        if not run and port and runs_in != "wsl" and int(port) in by_port:
            other = by_port[int(port)]
            busy = f"{other.get('name', '?')}{' · ' + other['label'] if other.get('label') else ''}"

        rev = None
        try:
            from ...chains.project_chains import find_latest_review
            r_info = find_latest_review(name)
            if r_info:
                rev = {
                    "name": r_info["name"],
                    "path": r_info["path"],
                    "mtime": r_info["mtime"],
                    "unreviewed_commits": bool(g and g.get("last_commit") and g["last_commit"] > r_info["mtime"]),
                    "tasks": r_info.get("tasks") or {"total": 0, "open": 0, "resolved": 0},
                }
        except Exception:
            pass

        auto_hunt = False
        if is_repo:
            try:
                from ...chains.project_chains import is_project_hook_installed
                auto_hunt = is_project_hook_installed(folder)
            except Exception:
                pass

        mine = (reg.get("user") or {}).get(name) or {}
        hidden = mine["hidden"] if isinstance(mine.get("hidden"), bool) else name in reg["hidden"]
        rows.append({
            "name": name,
            "title": spec.get("title") or info["title"] or name,
            "description": spec.get("description") or info["description"],
            "status": mine.get("tag") or info["status"],
            "status_from": "you" if mine.get("tag") else ("readme" if info["status"] else ""),
            "hidden": hidden,
            "why_hidden": ("you hid it" if mine.get("hidden") is True else reg["hidden"].get(name) or "") if hidden else "",
            "self": bool(spec.get("self")),
            "autostart": bool(spec.get("autostart")),
            "media": spec.get("media"),
            "git": g,
            "github": (g or {}).get("github"),
            "review": rev,
            "auto_hunt": auto_hunt,
            "live": spec.get("live"),
            "open_file": spec.get("open_file"),
            "port": int(port) if port else None,
            "runs_in": runs_in,
            "running": run,
            "url": spec.get("url") or (f"http://{run.get('host') or '127.0.0.1'}:{run['port']}/" if run and run.get("port") else None),
            "busy_by": busy,
            "can_start": bool((start or (media or {}).get("start")) and not spec.get("self")),
            "can_stop": bool(run and not spec.get("self") and (run.get("pid") or spec.get("stop") or media)),
            "watcher": bool(spec.get("watcher") or not port),
            "sidecars": sidecars,
        })
    rows.sort(key=lambda r: (r["hidden"], not r["running"], -((r["git"] or {}).get("last_commit") or 0), r["name"].lower()))
    return rows


def collect(listening: list[dict] | None = None, wsl_ports: dict[int, bool] | None = None,
            registry: dict | None = None) -> dict:
    reg = registry or load_registry()
    rows = project_rows(reg, listening, wsl_ports)
    shown = [r for r in rows if not r["hidden"]]
    return {"available": True, "generated_at": time.time(), "projects": rows,
            "counts": {"shown": len(shown), "hidden": len(rows) - len(shown),
                       "running": sum(1 for r in shown if r["running"]),
                       "dirty": sum(1 for r in shown if (r["git"] or {}).get("dirty_files"))}}
