"""Localhost listeners: what serves on this PC right now, how long it has been up, and what closed recently.

Why: a dev server left running holds its port for days without anyone noticing - 10-04 a vite server on :5173 was
still listening two days later, beside the llama-servers that actually needed the machine. The dashboard card lists
what listens, remembers when each one appeared and closed, and closes the ones that are safe to close.

What may be closed: user-owned dev servers and apps. The local AI stack (llama-server, the ollama shim), Windows
services and the HUD's own process are listed read-only, so a click can never take a model down mid-run, stop a
system service, or kill the dashboard doing the clicking. `close_listener` resolves the port and re-checks the class
itself: what the page sends is only a request, never permission.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil

from ...paths import DATA_DIR

HISTORY_FILE = DATA_DIR / "ports-history.json"
KEEP_CLOSED = 40           # the card shows a handful; keep enough to answer "what was on :3000 yesterday?"
KEEP_DAYS = 7.0
STALE_GAP_S = 60.0         # no sample for longer than this: we only know it closed *some time* after the last one

LOCAL_ADDRS = ("127.0.0.1", "::1", "0.0.0.0", "::")
OPEN_ADDRS = ("0.0.0.0", "::")              # listening on every interface, not just loopback
NODE_MODULES = "\\node_modules\\"
SELF_MARKERS = ("sol_control_hud",)         # this app, whichever process serves the page (also its chain daemon)
AI_NAMES = {"llama-server", "llama-swap", "ollama"}
AI_MARKERS = ("ollama-shim", "llama-server", "llama-swap", "\\ai\\bin\\", "/ai/bin/")
AI_PORTS = range(11434, 11450)              # the router, the embedding server and the shim (D:\AI\Cache\llm\router.ini)
SERVICE_USERS = {"nt authority\\system", "nt authority\\local service", "nt authority\\network service"}
SYSTEM_NAMES = {"system", "svchost", "services", "lsass", "spoolsv", "vmms", "wininit", "smss", "msmpeng", "sshd"}
DEV_NAMES = {"node", "deno", "bun", "python", "pythonw", "uvicorn", "gunicorn", "dotnet", "php", "ruby", "java",
             "perl", "caddy", "nginx", "httpd", "esbuild", "webpack", "cargo", "go", "air", "tsx", "vite", "next"}
SKIP_DIRS = {"bin", "scripts", "src", "lib", "dist", "app", ".venv", "venv", "node_modules", "tools"}

CLASS_WORDS = {"dev": "dev server", "app": "app", "ai": "local AI", "system": "Windows", "self": "this dashboard"}
CLOSABLE = ("dev", "app")
KIND_ORDER = {"dev": 0, "app": 1, "ai": 2, "self": 3, "system": 4}   # what you might close, first


def _name(proc: psutil.Process) -> str:
    try:
        return (proc.name() or "").lower().removesuffix(".exe")
    except (psutil.Error, OSError):
        return ""


def _cmdline(proc: psutil.Process) -> list[str]:
    try:
        return list(proc.cmdline())
    except (psutil.Error, OSError):
        return []


def label(proc: psutil.Process) -> str:
    """A few words from the command line about what a port is serving: ":5173 node" alone doesn't say which app.
    A script under node_modules names the project folder above it (vite's own folder would say nothing useful)."""
    for arg in _cmdline(proc)[1:]:
        if arg.startswith("-") or arg.startswith("/"):
            continue                                     # a switch, not a path (`/prefetch:1` is not a script)
        if "\\" not in arg and "/" not in arg:
            return arg                                   # a module name, e.g. `-m vocal_savior watch`
        flat = str(Path(arg)).replace("/", "\\")
        low = flat.lower()
        tool = Path(flat).stem
        if NODE_MODULES in low:
            return f"{tool} \u00b7 {Path(flat[:low.index(NODE_MODULES)]).name}"
        parent = next((d.name for d in Path(flat).parents if d.name and d.name.lower() not in SKIP_DIRS), "")
        return f"{tool} \u00b7 {parent}" if parent and parent.lower() != tool.lower() else tool
    return ""


def classify(proc: psutil.Process, port: int, own_pid: int | None = None) -> str:
    """What a listener is, which decides whether the card may close it. The order matters: ours, then the AI stack,
    then Windows, then everything the user started."""
    if proc.pid == (os.getpid() if own_pid is None else own_pid):
        return "self"
    name = _name(proc)
    cmd = " ".join(_cmdline(proc)).lower()
    if any(m in cmd for m in SELF_MARKERS):
        return "self"              # another copy of this app (or its chain daemon): still never ours to close
    if name in AI_NAMES or port in AI_PORTS or any(m in cmd for m in AI_MARKERS):
        return "ai"
    if proc.pid in (0, 4) or name in SYSTEM_NAMES:
        return "system"
    try:
        if (proc.username() or "").lower() in SERVICE_USERS:
            return "system"
    except (psutil.Error, OSError):
        return "system"            # a process we may not even look at is not ours to close
    return "dev" if name in DEV_NAMES else "app"


def _rows(own_pid: int | None = None, now: float | None = None) -> list[dict]:
    """One row per (process, port) listening on this machine. IPv4 and IPv6 on the same port are one row."""
    now = time.time() if now is None else now
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.Error, OSError):
        return []
    by_key: dict[tuple[int, int], dict] = {}
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.pid or not c.laddr or c.laddr.ip not in LOCAL_ADDRS:
            continue
        key = (c.pid, c.laddr.port)
        if key in by_key:
            by_key[key]["addrs"].append(c.laddr.ip)
            continue
        try:
            proc = psutil.Process(c.pid)
            kind = classify(proc, c.laddr.port, own_pid)
            try:
                started = proc.create_time() or None     # the System process reports the epoch, not a start time
            except (psutil.Error, OSError):
                started = None
            by_key[key] = {"port": c.laddr.port, "pid": c.pid, "name": _name(proc), "label": label(proc),
                           "kind": kind, "kind_words": CLASS_WORDS[kind], "can_close": kind in CLOSABLE,
                           "started": started, "up_seconds": None if started is None else max(0.0, now - started),
                           "addrs": [c.laddr.ip]}
        except (psutil.NoSuchProcess, psutil.Error, OSError):
            continue
    rows = list(by_key.values())
    for r in rows:
        r["addrs"] = sorted(set(r["addrs"]))
        r["everyone"] = any(a in OPEN_ADDRS for a in r["addrs"])    # reachable from the network, not just this PC
    rows.sort(key=lambda r: (KIND_ORDER.get(r["kind"], 9), -(r["started"] or 0)))
    return rows


class History:
    """When each listener appeared, and the ones that have closed.

    The dashboard samples every few seconds while it is open; while it is closed nothing watches, so a port that
    goes away then is only known to have closed some time after the last sample taken. Those are marked `approx`.
    """

    def __init__(self, path: Path | str = HISTORY_FILE, keep: int = KEEP_CLOSED, days: float = KEEP_DAYS):
        self.path, self.keep, self.days = Path(path), keep, days
        self.state = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {"open": dict(data.get("open") or {}), "closed": list(data.get("closed") or [])}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"open": {}, "closed": []}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass            # the history is a nicety: a read-only disk must not break the card

    def update(self, rows: list[dict], now: float | None = None) -> list[dict]:
        """Fold one sample in and return the closed list, newest first. Written out only when the set of listeners
        changes, so an open dashboard doesn't rewrite this file every few seconds."""
        now = time.time() if now is None else now
        seen = {f"{r['pid']}:{r['port']}": {"port": r["port"], "pid": r["pid"], "name": r["name"],
                                            "label": r["label"], "kind": r["kind"], "started": r["started"],
                                            "last_seen": now} for r in rows}
        changed = set(seen) != set(self.state["open"])
        for key, was in self.state["open"].items():
            if key in seen:
                continue
            last = float(was.get("last_seen") or now)
            started = was.get("started")
            self.state["closed"].insert(0, {
                **{k: was.get(k) for k in ("port", "pid", "name", "label", "kind")},
                "started": started, "closed_at": last, "approx": (now - last) > STALE_GAP_S,
                "ran_seconds": None if started is None else max(0.0, last - float(started))})
        self.state["open"] = seen
        self.state["closed"] = [c for c in self.state["closed"][:self.keep]
                                if (now - float(c.get("closed_at") or 0)) <= self.days * 86400]
        if changed:
            self._save()
        return self.state["closed"]


_history: History | None = None


def _store() -> History:
    global _history
    if _history is None:
        _history = History()
    return _history


def listeners(own_pid: int | None = None, shown_closed: int = 8) -> dict:
    """The card's data: what listens now (what you might close first) and what closed recently."""
    rows = _rows(own_pid)
    closed = _store().update(rows)
    return {"available": True, "generated_at": time.time(), "listening": rows, "closed": closed[:shown_closed],
            "counts": {k: sum(1 for r in rows if r["kind"] == k) for k in CLASS_WORDS},
            "closable": sum(1 for r in rows if r["can_close"])}


def close_listener(port: int, pid: int | None = None, own_pid: int | None = None, timeout: float = 4.0) -> dict:
    """Close whatever listens on `port`: terminate first, kill only if it ignores that, then its leftover children
    (a dev server's esbuild/worker processes). The class is checked here against the live port - the page may ask
    for anything, but only a user-owned dev server or app is ever closed."""
    try:
        port = int(port)
        pid = None if pid in (None, "") else int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "why": "which port?"}
    row = next((r for r in _rows(own_pid) if r["port"] == port and (pid is None or r["pid"] == pid)), None)
    if row is None:
        return {"ok": False, "why": f"nothing listens on :{port} any more"}
    if not row["can_close"]:
        return {"ok": False, "why": f":{port} is {row['kind_words']} ({row['name']}) - the card keeps that one read-only"}
    what = row["label"] or row["name"]
    try:
        proc = psutil.Process(row["pid"])
        kids = proc.children(recursive=True)
        proc.terminate()
        if psutil.wait_procs([proc], timeout=timeout)[1]:
            proc.kill()
            psutil.wait_procs([proc], timeout=2.0)
        for kid in kids:                       # workers the dev server left behind
            try:
                kid.terminate()
            except (psutil.Error, OSError):
                continue
    except psutil.NoSuchProcess:
        return {"ok": True, "why": f":{port} is free ({what} had already stopped)"}
    except (psutil.AccessDenied, psutil.Error, OSError) as e:
        return {"ok": False, "why": f"could not close {what} on :{port}: {type(e).__name__}"}
    return {"ok": True, "why": f"closed :{port} ({what})"}
