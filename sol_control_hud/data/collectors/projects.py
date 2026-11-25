"""Projects in D:\\Workspace: what each one is, what state it's in, whether it runs right now, and how to start it.

Most of it is discovered from the folder (README, git, `.claude/launch.json`); `launchpad.yaml` only adds what can't
be (live sites, start commands for projects without a launch config, what to hide). A folder that isn't in the
registry still shows up (a folder that isn't a git repo is listed under "hidden").

Cost (measured with py-spy, 2026-11-22): git is one `git log` per repo plus a `git status` shared with the ticker
(remote URL read from .git/config), run only when the repo changed or every GIT_TTL_S in the background
(data/gitcache.py); the results are kept in data/gitcache.json across restarts. README / launch.json / launchpad.yaml
are parsed again only when the file changes. Whether something runs comes from the Localhost card's listener sample
plus ONE shared process scan for portless watchers (2 s), and the media card's WSL probe for things in WSL.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
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
    data = copy.deepcopy(_by_stat("yaml", Path(path), _parse_yaml) or {})    # parsed once per change; callers edit it
    data.setdefault("workspace", r"D:\Workspace")
    for key in ("hidden", "projects", "media"):
        data[key] = {str(k): (v if v is not None else {}) for k, v in (data.get(key) or {}).items()}
    data["user"] = load_user(user_file)
    return data


def outside_folders(reg: dict) -> dict[str, Path]:
    """Projects that live outside the workspace: a registry entry with `path:` (e.g. D:/Meta Scrub, 2026-11-25).
    Only folders that exist; the path comes from launchpad.yaml, never from the page."""
    out = {}
    for name, spec in reg.get("projects", {}).items():
        p = (spec or {}).get("path")
        if p and not str(name).startswith(".") and Path(p).is_dir():
            out[str(name)] = Path(p)
    return out


def _parse_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


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
    reg = load_registry()
    ws = Path(workspace or reg["workspace"])
    if not name or name.startswith(".") or "/" in name or "\\" in name or not (
            (ws / name).is_dir() or name in outside_folders(reg)):
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


_file_cache: dict[tuple, tuple] = {}       # (kind, path) -> (mtime_ns, size, value)
_file_lock = threading.Lock()


def _by_stat(kind: str, path: Path, load):
    """load(path), re-run only when the file's modified time or size changed (or it appeared / went away).
    README and launch.json reads were 79 ms of every 200 ms Projects card build (2026-11-22)."""
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    key = (kind, str(path))
    with _file_lock:
        hit = _file_cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    value = load(path) if sig else None
    with _file_lock:
        _file_cache[key] = (sig, value)
    return value


def readme_info_for(folder: Path) -> dict:
    """readme_info() of the folder's README, cached until that file changes."""
    for name in README_NAMES:
        info = _by_stat("readme", folder / name, lambda p: (lambda t: None if t is None else readme_info(t))(_readme_file(p)))
        if info is not None:
            return info
    return readme_info("")


def _readme_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:                    # locked, or a folder called README.md: the next name, as before
        return None


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


def open_file_for(folder: Path, spec: dict, has_start: bool | None = None) -> str | None:
    """The file the row's Open button opens: `open_file` from launchpad.yaml, else a plain web page (index.html with
    no package.json, e.g. sologo, Icon Generator) when the project has no start command."""
    if spec.get("open_file"):
        return spec["open_file"]
    if has_start is None:
        has_start = bool(spec.get("start") or launch_config(folder))
    if not has_start and (folder / "index.html").is_file() and not (folder / "package.json").exists():
        return "index.html"
    return None


# ---------------------------------------------------------------- launch.json
def _read_launch(path: Path) -> list[dict]:
    return _by_stat("launch", path, _parse_launch) or []


def _parse_launch(path: Path) -> list[dict]:
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


REMOTE_RE = re.compile(r'^\s*\[remote\s+"origin"\]\s*$(?P<body>.*?)(?=^\s*\[|\Z)', re.M | re.S)


def origin_url(folder: Path) -> str | None:
    """remote.origin.url, read from .git/config (no git process); asks git when the config includes other files."""
    try:
        text = (folder / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _git(folder, "config", "--get", "remote.origin.url")
    if re.search(r"^\s*\[include", text, re.M | re.I):
        return _git(folder, "config", "--get", "remote.origin.url")
    m = REMOTE_RE.search(text)
    if not m:
        return None
    u = re.search(r"^\s*url\s*=\s*(.+?)\s*$", m.group("body"), re.M)
    return u.group(1) if u else None


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
    from ..snapshot import repo_porcelain          # the ticker's dirty check runs the same `git status`: share it
    gitcache.PORCELAIN.forget(folder.name)         # ...but fresh: this runs because the repo changed or got old
    porcelain = repo_porcelain(folder) or ""
    remote = origin_url(folder)
    commit_t, _, subject = log.partition("\t")
    files = [ln for ln in porcelain.splitlines() if ln.strip()]
    return ({"last_commit": float(commit_t) if commit_t.isdigit() else None, "subject": subject[:120],
             "dirty_files": len(files), "dirty_days": dirty_age_days(folder, porcelain, now) if files else None,
             "github": github_url(remote)}, porcelain)


_git_cache = gitcache.RepoCache(GIT_TTL_S, background=True)   # an old entry never makes a click wait for git


def cached_git(folder: Path, max_age: float = GIT_TTL_S) -> dict | None:
    """git_info(), re-run only when the repo changed (data/gitcache.py) or after `max_age` seconds."""
    _git_cache.max_age = max_age
    if _git_cache.persist is None and not _git_cache._loaded:
        from ...paths import DATA_DIR
        _git_cache.persist = DATA_DIR / "gitcache.json"      # git-ignored machine state; tests use their own DATA_DIR
    box: dict = {}

    def run():
        box["info"], box["porcelain"] = _git_info(folder)
        return box["info"]
    return _git_cache.get(folder, run, paths=lambda _v: gitcache.porcelain_paths(box.get("porcelain", "")),
                          keep=lambda v: v is not None)


def forget_git(name: str | None = None) -> None:
    _git_cache.forget(name)


def save_git_cache() -> None:
    """At exit: keep what git said for the next start."""
    _git_cache.save()


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
    for pid, cwd, cmd, created in watcher_candidates():
        if cwd == marker or cwd.startswith(marker + "\\"):
            if any("build.ts" in x or "dev" in x or "watch" in x for x in cmd):
                up_s = max(1, int(time.time() - created))
                return {"port": None, "pid": pid, "where": "windows", "up_seconds": up_s, "kind": "watcher"}
    return None


WATCHER_NAMES = ("bun", "node", "powershell", "cmd", "python")
CANDIDATES_TTL_S = 2.0
_cand_lock = threading.Lock()
_cand: tuple[float, list] = (-1e9, [])


def reset_candidates() -> None:
    """A start / stop just happened: the next lookup scans again."""
    global _cand
    with _cand_lock:
        _cand = (-1e9, [])


def watcher_candidates(clock=time.monotonic) -> list[tuple[int, str, list[str], float]]:
    """(pid, working folder, lower-case command line, start time) of every process that could be a project's watcher,
    from ONE process scan shared by all folders for CANDIDATES_TTL_S. It used to be one scan (reading every matching
    process's cwd) per portless project: 20% of the HUD's busy time with the dashboard open (py-spy, 2026-11-22)."""
    global _cand
    with _cand_lock:
        if clock() - _cand[0] < CANDIDATES_TTL_S:
            return _cand[1]
        out = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if not any(k in (p.info.get("name") or "").lower() for k in WATCHER_NAMES):
                    continue
                cwd = p.cwd()
                out.append((p.pid, _norm(cwd) if cwd else "", [str(x).lower() for x in p.cmdline() if x], p.create_time()))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        _cand = (clock(), out)
        return out


# ---------------------------------------------------------------- the card
def _media_entry(reg: dict, name: str) -> dict | None:
    m = (reg["projects"].get(name) or {}).get("media")
    return reg["media"].get(m) if m else None


def project_rows(reg: dict, listening: list[dict] | None = None, wsl_ports: dict[int, bool] | None = None,
                 workspace: Path | None = None, git=cached_git, paths=_proc_paths,
                 find_proc=find_project_proc, handoffs: dict[str, dict] | None = None,
                 sessions: dict[str, dict] | None = None) -> list[dict]:
    """One row per folder in the workspace, most recently worked on first."""
    ws = Path(workspace or reg["workspace"])
    try:
        folders = {d.name: d for d in ws.iterdir() if d.is_dir() and not d.name.startswith(".")}
    except OSError:
        return []
    for name, folder in outside_folders(reg).items():
        folders.setdefault(name, folder)
    listening = listening or []
    wsl_ports = wsl_ports or {}
    running = match_listeners(folders, listening, paths)
    by_port = {r["port"]: r for r in listening}
    rows = []
    for name, folder in folders.items():
        spec = reg["projects"].get(name) or {}
        media = _media_entry(reg, name)
        is_repo = (folder / ".git").exists()
        # a loose folder (no git, not in the registry): listed under "hidden" so it can still be found and shown
        loose = not is_repo and name not in reg["projects"] and name not in reg["hidden"]
        info = readme_info_for(folder)
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
        hidden = mine["hidden"] if isinstance(mine.get("hidden"), bool) else (name in reg["hidden"] or loose)
        rows.append({
            "name": name,
            "title": spec.get("title") or info["title"] or name,
            "description": spec.get("description") or info["description"],
            "status": mine.get("tag") or info["status"],
            "status_from": "you" if mine.get("tag") else ("readme" if info["status"] else ""),
            "hidden": hidden,
            "why_hidden": ("you hid it" if mine.get("hidden") is True
                           else reg["hidden"].get(name) or ("not a git repo" if loose else "")) if hidden else "",
            "self": bool(spec.get("self")),
            "autostart": bool(spec.get("autostart")),
            "media": spec.get("media"),
            "git": g,
            "github": (g or {}).get("github"),
            "review": rev,
            "auto_hunt": auto_hunt,
            "live": spec.get("live"),
            "open_file": open_file_for(folder, spec, has_start=bool(start or (media or {}).get("start"))),
            "port": int(port) if port else None,
            "runs_in": runs_in,
            "running": run,
            "url": spec.get("url") or (f"http://{run.get('host') or '127.0.0.1'}:{run['port']}/" if run and run.get("port") else None),
            "busy_by": busy,
            "can_start": bool((start or (media or {}).get("start")) and not spec.get("self")),
            "can_stop": bool(run and not spec.get("self") and (run.get("pid") or spec.get("stop") or media)),
            "watcher": bool(spec.get("watcher") or not port),
            "sidecars": sidecars,
            "session": (sessions or {}).get(name),
            "handoff": (handoffs or {}).get(name),
        })
    rows.sort(key=lambda r: (r["hidden"], not r["running"], -((r["git"] or {}).get("last_commit") or 0), r["name"].lower()))
    return rows


def collect(listening: list[dict] | None = None, wsl_ports: dict[int, bool] | None = None,
            registry: dict | None = None) -> dict:
    from . import sessions as _sessions
    reg = registry or load_registry()
    handoffs, sess = _sessions.collect_for_projects()
    rows = project_rows(reg, listening, wsl_ports, handoffs=handoffs, sessions=sess)
    shown = [r for r in rows if not r["hidden"]]
    return {"available": True, "generated_at": time.time(), "projects": rows,
            "counts": {"shown": len(shown), "hidden": len(rows) - len(shown),
                       "running": sum(1 for r in shown if r["running"]),
                       "dirty": sum(1 for r in shown if (r["git"] or {}).get("dirty_files"))}}
