"""Is the HUD itself OK, and is what it shows fresh? (plans/SOL_HUD_IMPROVEMENTS_PLAN.md E1). Pure functions over its own
logs and timestamps; the hub calls them at most once a minute for the logs, and every look for the cheap freshness part.

- **How the last run ended.** data\\app-running.json is written at start and removed by a clean Exit. Found at the next
  start, it means the last run never reached Exit: if it started before Windows booted, the PC restarted under it (not
  a HUD problem); otherwise it crashed or was killed. The hub logs one line either way, and the card counts them.
- **Real errors.** `crash:` / `tick error:` lines and tracebacks from hub.log and ticker.log, newest first, without the
  closed-connection noise (a browser closing the stream; see hub.ClosedConnectionFilter).
- **Freshness.** A background sampler that died quietly leaves its last numbers on screen forever; each one's age is
  compared with its own interval.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

ENDED_UNEXPECTEDLY = "previous run ended without Exit"
ENDED_WITH_PC = "previous run ended when the PC restarted"
TAIL_BYTES = 64 * 1024
SHOWN_ERRORS = 5
STALE_FACTOR = 3.0          # stale = older than this many of its own intervals (+ STALE_GRACE_S)
STALE_GRACE_S = 5.0

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) (.*)$")
_ERROR_MSG = ("crash:", "tick error:", "error:", "error ")
_NOISE = ("_call_connection_lost",)          # asyncio's closed-connection tracebacks (ConnectionResetError 10054)


def _ts(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


def previous_end(marker: dict | None, boot_time: float) -> str | None:
    """How the run that wrote `marker` ended: None = clean Exit (no marker), else one of the two log lines."""
    if not marker:
        return None
    started = _ts(marker.get("started"))
    if started is not None and started < boot_time:
        return ENDED_WITH_PC
    return ENDED_UNEXPECTEDLY


def tail(path: Path, nbytes: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def restarts(log_text: str, now: float | None = None, days: float = 7) -> dict:
    """Unexpected ends (crash / kill) in the last `days` and the last 24 h, from the hub's own start-time lines."""
    now = time.time() if now is None else now
    times = [t for line in log_text.splitlines()
             if (m := _TS.match(line)) and m.group(2).startswith(ENDED_UNEXPECTEDLY) and (t := _ts(m.group(1)))]
    week = [t for t in times if now - t <= days * 86400]
    return {"week": len(week), "day": sum(1 for t in week if now - t <= 86400), "last": max(week) if week else None}


def errors(sources: list[tuple[str, str]], n: int = SHOWN_ERRORS) -> list[dict]:
    """The newest `n` real errors across (source name, log text) pairs: {source, at, text, detail}."""
    found: list[dict] = []
    for source, text in sources:
        entries: list[dict] = []
        last_at = None
        for line in text.splitlines():
            m = _TS.match(line)
            if m:
                last_at = _ts(m.group(1))
                entries.append({"at": last_at, "lines": [m.group(2)], "stamped": True})
                continue
            if not line.strip():
                continue
            cur = entries[-1] if entries else None
            if cur is not None and cur["stamped"] and cur["lines"][0].startswith(_ERROR_MSG):
                cur["lines"].append(line)        # a `crash:` line's own traceback
            elif cur is not None and not cur["stamped"] and not (_ended(cur["lines"]) and not _chained(line)):
                cur["lines"].append(line)        # the rest of a stderr block
            else:
                entries.append({"at": last_at, "lines": [line], "stamped": False})
        for e in entries:
            body = "\n".join(e["lines"])
            if any(x in body for x in _NOISE):
                continue
            if e["stamped"]:
                if not e["lines"][0].lower().startswith(_ERROR_MSG):
                    continue
            elif "Traceback" not in body and "ERROR" not in body:
                continue                 # uvicorn warnings and other stderr chatter
            found.append({"source": source, "at": e["at"], "text": _headline(e["lines"])[:200], "detail": body[:4000]})
    found.sort(key=lambda e: e["at"] or 0, reverse=True)
    return found[:n]


def _ended(lines: list[str]) -> bool:
    """A stderr block is complete once its traceback reached the exception line (not indented, not a header)."""
    return (any(x.startswith("Traceback") for x in lines)
            and not lines[-1].startswith((" ", "\t", "Traceback", "During handling", "The above")))


def _chained(line: str) -> bool:
    """The lines that join a finished traceback to the next one (raise ... from / during handling)."""
    return line.startswith(("During handling", "The above exception"))


def _headline(lines: list[str]) -> str:
    """The line that says what went wrong: a traceback's exception line, else the message itself."""
    if any(x.startswith("Traceback") or "Traceback" in x for x in lines):
        for x in reversed(lines):
            if x and not x.startswith((" ", "\t")) and not x.startswith("Traceback"):
                return x.removeprefix("crash: ").strip()
    return lines[0]


def handled(text: str, now: float | None = None, hours: float = 24, n: int = SHOWN_ERRORS) -> dict:
    """data/swallowed.log (swallow.note): errors the code carried on after. Each place + kind is written at most once
    an hour, so this counts distinct problems, not how often they happened. Shown on the card; never sets the level."""
    now = time.time() if now is None else now
    recent = []
    for line in text.splitlines():
        m = _TS.match(line)
        t = _ts(m.group(1)) if m else None
        if t is not None and now - t <= hours * 3600:
            where, _, what = m.group(2).partition(": ")
            recent.append({"at": t, "where": where, "text": what[:200]})
    recent.sort(key=lambda e: e["at"], reverse=True)
    return {"day": len(recent), "places": len({e["where"].split(" (")[0] for e in recent}), "recent": recent[:n]}


def stale(checks: list[tuple[str, float | None, float, bool]], now: float | None = None) -> list[dict]:
    """checks: (name, last good time.time() or None, its interval s, its thread alive). The ones that need a look."""
    now = time.time() if now is None else now
    out = []
    for name, at, interval, alive in checks:
        if not alive:
            out.append({"name": name, "age_s": None if at is None else round(now - at), "why": "stopped"})
        elif at is not None and now - at > STALE_FACTOR * interval + STALE_GRACE_S:
            out.append({"name": name, "age_s": round(now - at), "why": "stale"})
    return out


def level(report: dict) -> str:
    """ok / warn / bad for the card's pill and the ticker: bad = it keeps crashing or a sampler stopped."""
    r = report.get("restarts") or {}
    if r.get("day", 0) >= 3 or any(s["why"] == "stopped" for s in report.get("stale") or []):
        return "bad"
    if r.get("week", 0) or report.get("stale") or report.get("broken"):
        return "warn"
    return "ok"
