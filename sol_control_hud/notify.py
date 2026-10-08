"""Notifications: what's worth a Windows notification, and when (phase B of plans/SOL_CONTROL_HUD_NEXT_PLAN.md).

Pure logic; the hub shows the result through the tray icon (tray.Tray.notify).
- Sources: two snapshots (crash counts, disk trends, VRAM spill) and the new lines of away.jsonl / chains.log since the
  last check (read from where the previous check stopped: old events never pop up).
- During a game (or anything fullscreen) notices wait; afterwards they come out as one summary.
- Quiet hours (default 23:00-08:00): only crashes; the rest is dropped (the morning briefing and the dashboard's last
  Away summary cover the night).
- No repeats: one notice per kind (and chain) per 10 minutes.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path

from .data.snapshot import RUN_RESULT, RUN_WORDS, clean_log_line

KINDS = {  # kind -> label for the settings
    "away": "Away finished",
    "chain_failed": "A chain failed",
    "chain_done": "A chain finished",
    "crash": "New crash event",
    "disk": "A disk filling up fast",
    "vram": "A model spilled out of VRAM",
    "answer": "An overnight answer is ready",
    "media_stuck": "A media job got stuck",
    "digest": "The weekly machine digest is ready",
}
DEFAULTS = {"enabled": True, "kinds": {k: True for k in KINDS}, "quiet": "23:00-08:00"}
REPEAT_S = 600.0
DISK_FAST_GB = 5.0


@dataclass
class Notice:
    kind: str
    title: str
    text: str
    level: str = "info"        # info | warning | error (the icon Windows shows)
    key: str = ""              # for the no-repeat rule (kind + what it's about)


class LogTail:
    """New lines of a log since the last read. Starts at the end of the file: old events never pop up."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.pos = path.stat().st_size
        except OSError:
            self.pos = 0

    def new_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
            if size < self.pos:          # the log was rotated / rewritten: start over from its beginning
                self.pos = 0
            if size == self.pos:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
            cut = data.rfind(b"\n") + 1  # only whole lines; a half-written one waits for the next check
            self.pos += cut
            return data[:cut].decode("utf-8", "replace").splitlines()
        except OSError:
            return []


def in_quiet_hours(quiet: str, now: datetime) -> bool:
    try:
        a, b = (dtime.fromisoformat(x.strip()) for x in quiet.split("-"))
    except ValueError:
        return False
    t = now.time()
    return (a <= t < b) if a <= b else (t >= a or t < b)


def from_snapshots(prev, curr) -> list[Notice]:
    if prev is None or not prev.sampled_at:
        return []
    out = []
    crashes, before = curr.unexpected_reboots + curr.gpu_resets, prev.unexpected_reboots + prev.gpu_resets
    if crashes > before:
        out.append(Notice("crash", "New crash event", f"{crashes} since the last review: Away is blocked until it's reviewed "
                                                      "(right-click the ticker > Quick Actions after checking reports).",
                          "error", "crash"))
    # Only a spill that slowed a real answer: the shared-memory counter alone also trips while nothing is answering
    # (10-06: every ~35 min all day) and when the model still runs at its usual speed.
    if curr.vram_spill_impact == "slow" and prev.vram_spill_impact != "slow":
        speed = f" ({curr.ai_tps:.0f} tok/s)" if curr.ai_tps else ""
        out.append(Notice("vram", "Model spilled out of VRAM", f"{curr.ai_model or 'The model'} answers slow{speed}: "
                                                               f"{curr.vram_spilled_gb:.1f} GB of it is in system RAM until "
                                                               "VRAM frees up (close a game/browser tab).",
                          "warning", "vram"))
    for drive, delta in (curr.disk_trends or {}).items():
        if delta <= -DISK_FAST_GB and (prev.disk_trends or {}).get(drive, 0) > -DISK_FAST_GB:
            out.append(Notice("disk", f"{drive}: filling up fast", f"{-delta:.0f} GB used up in the last hour.", "warning",
                              f"disk:{drive}"))
    return out


def from_media(tiles: list[dict], seen: set[str]) -> list[Notice]:
    """One notice per media job that got stuck (media_apis.stuck_jobs: the file says queued/processing, the service
    isn't running). `seen` remembers which were already told, so a stuck job is told once, not every check."""
    out = []
    for t in tiles or []:
        for s in t.get("stuck") or []:
            key = f"media:{t.get('name')}:{s.get('id')}"
            if s.get("why") != "stuck" or key in seen:
                continue
            seen.add(key)
            out.append(Notice("media_stuck", f"{t.get('title') or t.get('name')}: a job got stuck",
                              f"{s.get('title') or 'A job'} still says {s.get('status')}, but the service isn't running. "
                              "Start it again or clear the job.", "warning", key))
    return out


def from_away_lines(lines: list[str], session: dict | None) -> list[Notice]:
    """away.jsonl: Away ended -> a summary (the session numbers come from snapshot.night_summary); answers ready."""
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "away-end":
            s = session or {}
            parts = [f"{s['done']} job{'s' if s.get('done') != 1 else ''} done"] if s.get("done") is not None else []
            if s.get("failed"):
                parts.append(f"{len(s['failed'])} failed")
            parts += s.get("chains") or []
            reason = e.get("reason") or ""
            out.append(Notice("away", "Away finished" + (f" ({reason})" if reason else ""),
                              ", ".join(parts) or f"after {e.get('minutes', '?')} min", "info", "away"))
        elif e.get("event") == "job-done" and str(e.get("job", "")).startswith("ask-"):
            out.append(Notice("answer", "Answer ready", f"{e['job'][4:]}: in 1Notebook\\Answers", "info", f"answer:{e['job']}"))
    return out


def from_chain_lines(lines: list[str]) -> list[Notice]:
    out = []
    for line in lines:
        m = RUN_RESULT.match(clean_log_line(line))
        if not m:
            continue
        name, word = m["name"].removesuffix(" request"), RUN_WORDS[m["status"]]
        if word == "failed":
            rest = clean_log_line(line)      # "Weekly digest.md: run 3 failed: model gone" -> "model gone"
            why = rest.split(" failed: ", 1)[1] if " failed: " in rest else ""
            out.append(Notice("chain_failed", f"Chain failed: {name}", why or "see the Chains card", "error", f"chain:{name}"))
        elif word == "finished":
            out.append(Notice("chain_done", f"Chain finished: {name}", "result in 1Notebook\\Chains\\Results", "info",
                              f"chain:{name}"))
    return out


class Notifier:
    def __init__(self, settings: dict, away_log: Path, chains_log: Path, clock=time.time, session_fn=None):
        self.settings = settings
        self.clock = clock
        self.session_fn = session_fn     # the last Away session's numbers; asked only when an Away run ended
        self.away, self.chains = LogTail(away_log), LogTail(chains_log)
        self.last_sent: dict[str, float] = {}
        self.held: list[Notice] = []

    def _allowed(self, n: Notice, now: float) -> bool:
        s = {**DEFAULTS, **self.settings}
        if not s.get("enabled", True) or not s.get("kinds", {}).get(n.kind, True):
            return False
        if in_quiet_hours(s.get("quiet", ""), datetime.fromtimestamp(now)) and n.kind != "crash":
            return False
        key = n.key or n.kind
        if now - self.last_sent.get(key, -1e9) < REPEAT_S:
            return False
        return True

    def check(self, prev, curr, busy: bool, extra: list[Notice] | None = None) -> list[Notice]:
        """Everything new since the last check that should be shown now. busy = a game / fullscreen app is in front:
        keep them for later and show one summary when it's over."""
        now = self.clock()
        away_lines = self.away.new_lines()
        session = self.session_fn() if self.session_fn and any('"away-end"' in ln for ln in away_lines) else None
        found = (from_snapshots(prev, curr) + from_away_lines(away_lines, session) + from_chain_lines(self.chains.new_lines())
                 + list(extra or []))
        fresh = [n for n in found if self._allowed(n, now)]
        for n in fresh:
            self.last_sent[n.key or n.kind] = now
        if busy:
            self.held += fresh
            return []
        if self.held:
            held, self.held = self.held + fresh, []
            if len(held) == 1:
                return held
            level = "error" if any(h.level == "error" for h in held) else "warning" if any(h.level == "warning" for h in held) else "info"
            return [Notice("summary", f"While you were busy: {len(held)} things", " · ".join(h.title for h in held)[:250],
                           level, "summary")]
        return fresh
