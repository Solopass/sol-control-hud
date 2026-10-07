"""Projects in D:\\Workspace: what each one is, what state it's in, whether it runs right now, and how to start it.

Most of it is discovered from the folder (README, git, `.claude/launch.json`); `launchpad.yaml` only adds what can't
be (live sites, start commands for projects without a launch config, what to hide). A folder that isn't in the
registry still shows up.

Cost: git is two short processes per repo, so it is cached per repo for GIT_TTL_S; whether something runs comes from
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

REGISTRY_FILE = Path(__file__).resolve().parents[2] / "launchpad.yaml"
NO_WINDOW = 0x08000000
GIT_TTL_S = 60.0
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
def launch_config(folder: Path) -> dict | None:
    """The first dev-server entry of the project's own `.claude/launch.json` (what preview_start uses)."""
    try:
        raw = (folder / ".claude" / "launch.json").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            data = json.loads(raw.replace("\\", "\\\\").replace("\\\\\\\\", "\\\\"))   # unescaped Windows paths
        except ValueError:
            return None
    for c in data.get("configurations") or []:
        exe, port = c.get("runtimeExecutable"), c.get("port")
        if exe and port:
            return {"cmd": [str(exe), *[str(a) for a in c.get("runtimeArgs") or []]], "port": int(port),
                    "name": c.get("name", "")}
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
    if not (folder / ".git").exists():
        return None
    from ..snapshot import dirty_age_days
    now = time.time() if now is None else now
    log = (_git(folder, "log", "-1", "--format=%ct%x09%s") or "").strip()
    porcelain = _git(folder, "status", "--porcelain") or ""
    remote = _git(folder, "config", "--get", "remote.origin.url")
    commit_t, _, subject = log.partition("\t")
    files = [ln for ln in porcelain.splitlines() if ln.strip()]
    return {"last_commit": float(commit_t) if commit_t.isdigit() else None, "subject": subject[:120],
            "dirty_files": len(files), "dirty_days": dirty_age_days(folder, porcelain, now) if files else None,
            "github": github_url(remote)}


_git_cache: dict[str, tuple[float, dict | None]] = {}


def cached_git(folder: Path, ttl: float = GIT_TTL_S) -> dict | None:
    key, now = str(folder).lower(), time.monotonic()
    hit = _git_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    info = git_info(folder)
    _git_cache[key] = (now, info)
    return info


def forget_git(name: str | None = None) -> None:
    if name is None:
        _git_cache.clear()
    else:
        for k in [k for k in _git_cache if k.endswith("\\" + name.lower())]:
            _git_cache.pop(k, None)


# ---------------------------------------------------------------- what runs
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


# ---------------------------------------------------------------- the card
def _media_entry(reg: dict, name: str) -> dict | None:
    m = (reg["projects"].get(name) or {}).get("media")
    return reg["media"].get(m) if m else None


def project_rows(reg: dict, listening: list[dict] | None = None, wsl_ports: dict[int, bool] | None = None,
                 workspace: Path | None = None, git=cached_git, paths=_proc_paths) -> list[dict]:
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

        run = None
        if runs_in == "wsl":
            if port and wsl_ports.get(int(port)):
                run = {"port": int(port), "pid": None, "where": "wsl"}
        else:
            mine = running.get(name) or []
            pick = next((r for r in mine if r["port"] == port), mine[0] if mine else None)
            if pick:
                run = {"port": pick["port"], "pid": pick["pid"], "where": "windows", "up_seconds": pick.get("up_seconds")}
        busy = None
        if not run and port and runs_in != "wsl" and int(port) in by_port:
            other = by_port[int(port)]
            busy = f"{other.get('name', '?')}{' · ' + other['label'] if other.get('label') else ''}"

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
            "media": spec.get("media"),
            "git": g,
            "github": (g or {}).get("github"),
            "live": spec.get("live"),
            "open_file": spec.get("open_file"),
            "port": int(port) if port else None,
            "runs_in": runs_in,
            "running": run,
            "url": spec.get("url") or (f"http://127.0.0.1:{run['port']}/" if run else None),
            "busy_by": busy,
            "can_start": bool((start or (media or {}).get("start")) and not spec.get("self")),
            "can_stop": bool(run and not spec.get("self") and (run.get("pid") or spec.get("stop") or media)),
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
