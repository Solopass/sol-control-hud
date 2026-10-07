"""The local media services (media-api, speedman, Vocal Savior, OmniTools): up or not, and what they did lately.

The rule that shapes this file: **looking must not wake anything.** media-api and speedman sit behind systemd socket
activation in WSL, so any request to their port starts them (RAM, CPU, sometimes models) and most of their endpoints
also count as activity, which postpones their idle shutdown. And both speedman's and Vocal Savior's /health ping the
other services, so even "health" wakes something. So:

- **State** comes from systemd inside WSL (one `wsl.exe` call for every service, only while WSL already runs) and from
  the Windows listener sample the Localhost card already takes.
- **What they did lately** comes from their files on D: (media-api's job file, Vocal Savior's job database, the newest
  outputs), which needs no service at all.
- **No request is sent on its own**, not even to a service systemd calls active. A service takes about a second to
  shut down (its own idle timer, or Stop) and is still "active" meanwhile; a request that lands then waits in the
  systemd socket and starts it straight back up. (2026-10-07: an automatic /health read did exactly that ~7 s after a
  Stop.) So "active jobs" come from the job files and disk space from Windows.
- API data (job lists, health) is fetched only when you press "Live", and only if the service already runs.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

NO_WINDOW = 0x08000000
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".mp4", ".mkv", ".webm"}
RECENT = 5
WEEK_S = 7 * 86400          # the tally covers a week: a single day is usually empty
ACTIVE_STATUSES = {"queued", "running", "processing"}

STATE_WORDS = {"up": "running", "busy": "working", "ready": "ready · starts on first use", "asleep": "WSL is off",
               "down": "not available", "off": "not running", "busy_port": "port taken"}


# ---------------------------------------------------------------- WSL: one call for every service
def wsl_script(media: dict, wsl_ports: list[int]) -> str:
    """A bash script that prints one line per fact:  U <name> <service> <socket> <since>  and  P <port> 0|1.
    Only systemd and `ss` are asked: nothing here connects to a service's port."""
    lines = []
    for name, m in media.items():
        unit = m.get("unit")
        if m.get("runs_in") != "wsl" or not unit:
            continue
        lines.append(f'svc=$(systemctl is-active {unit}.service); sock=$(systemctl is-active {unit}.socket); '
                     f't=$(systemctl show -p ActiveEnterTimestamp --value {unit}.service); '
                     f'since=0; [ "$svc" = active ] && [ -n "$t" ] && since=$(date +%s -d "$t" 2>/dev/null || echo 0); '
                     f'echo "U {name} $svc $sock $since"')
    for port in sorted(set(wsl_ports)):
        lines.append(f'if ss -ltnH "sport = :{int(port)}" | grep -q .; then echo "P {int(port)} 1"; else echo "P {int(port)} 0"; fi')
    return "\n".join(lines) + "\ntrue\n"


def parse_wsl(out: str) -> dict:
    units, ports = {}, {}
    for line in (out or "").splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) < 3:
            continue
        tag, name, rest = parts
        if tag == "U":
            bits = rest.split()
            units[name] = {"service": bits[0] if bits else "unknown", "socket": bits[1] if len(bits) > 1 else "unknown",
                           "since": float(bits[2]) if len(bits) > 2 and bits[2].isdigit() and bits[2] != "0" else None}
        elif tag == "P" and name.isdigit():
            ports[int(name)] = rest.strip() == "1"
    return {"units": units, "ports": ports}


def wsl_probe(media: dict, wsl_ports: list[int], distro: str, wsl_state: str | None, run=subprocess.run) -> dict:
    """Ask systemd inside WSL. Never when WSL is stopped: `wsl.exe -d` would boot it just to look."""
    if wsl_state != "Running":
        return {"running": False, "units": {}, "ports": {}}
    try:
        r = run(["wsl.exe", "-d", distro, "--exec", "bash", "-c", wsl_script(media, wsl_ports)], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=10, creationflags=NO_WINDOW)
        out = r.stdout
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is None:
        return {"running": True, "error": "WSL didn't answer", "units": {}, "ports": {}}
    return {"running": True, **parse_wsl(out)}


# ---------------------------------------------------------------- files on D: (no service needed)
def _iso(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _short_source(src: str) -> str:
    src = str(src or "")
    if "/" in src or "\\" in src:
        tail = src.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if src.startswith("http"):
            host = src.split("/")[2] if src.count("/") >= 2 else src
            return f"{host.removeprefix('www.')} · {tail[:40]}" if tail and tail != host else host
        return tail
    return src


def media_api_jobs(path: Path | str, now: float | None = None) -> dict:
    """media-api's own job file (it writes it on every status change): the latest jobs and the last week's tally."""
    now = time.time() if now is None else now
    try:
        jobs = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"recent": [], "week": {}, "active": 0}
    if not isinstance(jobs, list):
        return {"recent": [], "week": {}, "active": 0}
    for j in jobs:
        j["_t"] = _iso(j.get("created_at")) or 0.0
    jobs.sort(key=lambda j: j["_t"], reverse=True)
    week: dict[str, int] = {}
    for j in jobs:
        if now - j["_t"] <= WEEK_S:
            week[str(j.get("status"))] = week.get(str(j.get("status")), 0) + 1
    recent = [{"what": j.get("type", ""), "title": _short_source(j.get("source", "")), "status": j.get("status", ""),
               "at": j["_t"] or None, "progress": j.get("progress_percent"),
               "error": (j.get("error") or "")[:160] or None} for j in jobs[:RECENT]]
    active = sum(1 for j in jobs if str(j.get("status")) in ACTIVE_STATUSES)
    return {"recent": recent, "week": week, "total": len(jobs), "active": active}


def voice_jobs(db: Path | str, now: float | None = None) -> dict:
    """Vocal Savior's job database, opened read-only (it may be writing to it right now)."""
    now = time.time() if now is None else now
    p = Path(db)
    if not p.exists():
        return {"recent": [], "week": {}, "active": 0}
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT song, preset, status, stage, key_root, key_mode, bpm, message, created_at "
                               "FROM jobs ORDER BY id DESC LIMIT ?", (RECENT,)).fetchall()
            tally = con.execute("SELECT status, COUNT(*) FROM jobs WHERE created_at > ? GROUP BY status",
                                (now - WEEK_S,)).fetchall()
            total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            active = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing')").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return {"recent": [], "week": {}, "active": 0}
    recent = [{"what": preset, "title": song, "status": status,
               "detail": " · ".join(x for x in (f"{kr} {km}" if kr else "", f"{bpm:.0f} bpm" if bpm else "", stage if status != "finished" else "") if x),
               "at": created, "error": (msg or "")[:160] if status == "failed" else None}
              for song, preset, status, stage, kr, km, bpm, msg, created in rows]
    return {"recent": recent, "week": dict(tally), "total": total, "active": active}


def newest_files(folder: Path | str, exts=AUDIO_EXTS, n: int = RECENT, suffix: str | None = None) -> list[dict]:
    """The newest outputs in a folder (one level, no recursion: these folders can be big)."""
    try:
        files = [e for e in Path(folder).iterdir() if e.is_file() and (suffix is None and e.suffix.lower() in exts
                                                                       or suffix and e.name.lower().endswith(suffix))]
    except OSError:
        return []
    out = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({"title": f.stem[:80], "at": st.st_mtime, "size_mb": round(st.st_size / 1048576, 1)})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:n]


# ---------------------------------------------------------------- Windows-side services
def windows_listener(port: int, match: str | None, listening: list[dict]) -> tuple[dict | None, dict | None]:
    """(ours, someone_else) on that port. `match` is a piece of the command line that says it's really ours."""
    for row in listening:
        if row.get("port") != port:
            continue
        if not match:
            return row, None
        try:
            cmd = " ".join(psutil.Process(row["pid"]).cmdline()).lower()
        except (psutil.Error, OSError):
            cmd = ""
        return (row, None) if match.lower() in cmd or match.lower() in (row.get("label") or "").lower() else (None, row)
    return None, None


# ---------------------------------------------------------------- the card
def tile(name: str, m: dict, probe: dict, listening: list[dict], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    port = int(m.get("port") or 0)
    state, since, note = "off", None, ""
    if m.get("runs_in") == "wsl":
        if not probe.get("running"):
            state = "asleep"
        elif m.get("unit"):
            u = probe.get("units", {}).get(name) or {}
            if u.get("service") == "active":
                state, since = "up", u.get("since")
            elif u.get("socket") == "active":
                state = "ready"
            else:
                state = "down"
                note = f"its systemd socket is {u.get('socket', 'unknown')}" if u else probe.get("error", "")
        else:
            state = "up" if probe.get("ports", {}).get(port) else "off"
    else:
        ours, other = windows_listener(port, m.get("match"), listening)
        if ours:
            state, since = "up", ours.get("started")
        elif other:
            state, note = "busy_port", f":{port} is held by {other.get('name')}"

    recent, week, active = [], {}, 0
    if m.get("jobs_file") or m.get("jobs_db"):
        j = media_api_jobs(m["jobs_file"], now) if m.get("jobs_file") else voice_jobs(m["jobs_db"], now)
        recent, week = j["recent"], j["week"]
        # a job file says "running" forever after a crash: it only counts while the service is up
        active = j["active"] if state == "up" else 0
        if active:
            state = "busy"
    files = newest_files(m["folder"]) if m.get("folder") and not m.get("jobs_db") and not m.get("jobs_file") else []
    latest_note = newest_files(m["notes_dir"], n=1, suffix=".md") if m.get("notes_dir") else []

    facts = [["active jobs", str(active)]] if active else []
    if m.get("folder"):
        try:
            facts.append([f"{Path(m['folder']).drive or 'disk'} free", f"{shutil.disk_usage(m['folder']).free / 1024 ** 3:.0f} GB"])
        except OSError:
            pass
    return {
        "name": name, "title": m.get("title") or name, "what": m.get("what", ""), "project": m.get("project"),
        "port": port or None, "runs_in": m.get("runs_in", "windows"), "state": state,
        "state_words": STATE_WORDS.get(state, state), "note": note,
        "since": since, "up_seconds": None if not since else max(0.0, now - since),
        "url": f"http://127.0.0.1:{port}/" if port else None,
        "docs": f"http://127.0.0.1:{port}{m['docs']}" if port and m.get("docs") else None,
        "active_jobs": active,
        "facts": facts, "recent": recent, "week": week, "files": files, "latest_note": (latest_note or [None])[0],
        "has_folder": bool(m.get("folder")),
        "can_start": bool(m.get("start") or m.get("project")) and state in ("off", "asleep", "ready", "down"),
        "can_stop": state in ("up", "busy") and bool(m.get("shutdown") or m.get("project")),
        "can_live": state in ("up", "busy") and bool(m.get("jobs") or m.get("health")),
    }


def collect(registry: dict, listening: list[dict], wsl_state: str | None, probe: dict | None = None) -> dict:
    """The card's data. `probe` (the WSL answer) is passed in so the hub can cache it; computed here if not."""
    media = registry.get("media") or {}
    if probe is None:
        probe = wsl_probe(media, wsl_port_list(registry), registry.get("wsl_distro", "Ubuntu-24.04"), wsl_state)
    tiles = [tile(name, m, probe, listening) for name, m in media.items()]
    return {"available": True, "generated_at": time.time(), "wsl": wsl_state, "tiles": tiles,
            "counts": {"up": sum(1 for t in tiles if t["state"] in ("up", "busy")), "total": len(tiles)}}


def running_ports(registry: dict, probe: dict) -> dict[int, bool]:
    """WSL ports that really serve right now: a socket-activated service counts only while its service runs (its
    socket listens all the time), anything else when `ss` saw the port."""
    ports = dict(probe.get("ports") or {})
    for name, m in (registry.get("media") or {}).items():
        if m.get("runs_in") == "wsl" and m.get("unit") and m.get("port"):
            ports[int(m["port"])] = (probe.get("units", {}).get(name) or {}).get("service") == "active"
    return ports


def wsl_port_list(registry: dict) -> list[int]:
    """Ports of things that run in WSL without a systemd unit (omni-tools' dev server): checked with `ss`."""
    ports = [int(m["port"]) for m in (registry.get("media") or {}).values()
             if m.get("runs_in") == "wsl" and not m.get("unit") and m.get("port")]
    ports += [int(p["port"]) for p in (registry.get("projects") or {}).values()
              if (p or {}).get("runs_in") == "wsl" and (p or {}).get("port")]
    return sorted(set(ports))
