"""Longer history (phase D of plans/SOL_CONTROL_HUD_NEXT_PLAN.md): data\\metrics.sqlite.

- One row a minute (averages and peaks) from the samples the collector takes anyway: no extra sampling.
- Events as they happen: Away start/end, jobs done/failed, chain results (new lines of away.jsonl / chains.log), and
  new crash events (the snapshot's counts rising).
- Kept 90 days (a few MB). Charts get 24 h (1-minute rows) and 7 d (15-minute buckets); the "this week" card reads it.
The hub writes from its Tk thread; the web server reads with its own short-lived connections (WAL: no blocking).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .data.snapshot import RUN_RESULT, RUN_WORDS, clean_log_line
from .notify import LogTail

KEEP_DAYS = 90
COLS = ("gpu", "vram", "ram", "cpu", "temp", "hotspot", "down", "up")   # the chart series (same names as the 1 h history)
SCHEMA = """
CREATE TABLE IF NOT EXISTS minutes (t INTEGER PRIMARY KEY, gpu REAL, gpu_max REAL, vram REAL, vram_max REAL, ram REAL,
  ram_max REAL, cpu REAL, temp REAL, temp_max REAL, hotspot REAL, hotspot_max REAL, down REAL, up REAL, disks TEXT);
CREATE TABLE IF NOT EXISTS events (t REAL, kind TEXT, name TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
"""
# Added 10-08 (plans/SOL_HUD_IMPROVEMENTS_PLAN.md B): is the spill alarm real? spilled = the AI runner's peak GB in system
# RAM that minute, tps = the last answer's speed seen. Old rows stay NULL.
NEW_COLS = (("spilled", "REAL"), ("tps", "REAL"))
SPILL_RANK = {"unknown": 0, "fine": 1, "slow": 2}   # an episode's verdict is the worst one it reached


class Metrics:
    def __init__(self, path: Path, away_log: Path | None = None, chains_log: Path | None = None, clock=time.time):
        self.path, self.clock = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(minutes)")}
        for name, kind in NEW_COLS:
            if name not in have:
                self.db.execute(f"ALTER TABLE minutes ADD COLUMN {name} {kind}")
        self.db.commit()
        self.spill: dict | None = None     # the spill going on now: {"start", "verdict", "gb"}
        self.minute: int | None = None
        self.acc: list[tuple] = []
        self.disks: dict[str, float] = {}
        self.last_sample = 0.0
        self.crashes: int | None = None
        self.away = LogTail(away_log) if away_log else None
        self.chains = LogTail(chains_log) if chains_log else None
        self.last_prune = 0.0
        if self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0:
            self._backfill()               # first start: the week card counts what the logs already have

    def _backfill(self) -> None:
        """Read the whole Away / chain logs once (last KEEP_DAYS days) so "this week" isn't empty on day one."""
        cut = self.clock() - KEEP_DAYS * 86400
        for tail in (self.away, self.chains):
            if tail is None:
                continue
            end = tail.pos
            tail.pos = 0
            self.read_logs_from(tail, cut)
            tail.pos = max(tail.pos, end)

    def read_logs_from(self, tail: LogTail, cut: float = 0.0) -> None:
        self._events_from(tail.new_lines(), tail is self.away, cut)

    # ---- writing (hub, every tick)
    def add(self, s) -> None:
        if not s.sampled_at or s.sampled_at == self.last_sample:
            return
        self.last_sample = s.sampled_at
        minute = int(s.sampled_at // 60 * 60)
        if self.minute is not None and minute != self.minute:
            self._flush()
        self.minute = minute
        self.acc.append((s.gpu_load, s.vram_used_gb, s.ram_used_gb, s.cpu_percent, s.gpu_temp, s.gpu_hotspot,
                         s.net_down_kb, s.net_up_kb, getattr(s, "vram_spilled_gb", None), getattr(s, "ai_tps", None)))
        self._track_spill(s)
        self.disks = {d["drive"]: d["free_gb"] for d in (s.disks or []) if "drive" in d and "free_gb" in d}
        crashes = s.unexpected_reboots + s.gpu_resets
        if self.crashes is not None and crashes > self.crashes:
            self.event("crash", "crash", f"{crashes} since the last review", s.sampled_at)
        self.crashes = crashes

    def _flush(self) -> None:
        if not self.acc:
            return
        cols = list(zip(*self.acc))
        avg = lambda xs: (round(sum(v for v in xs if v is not None) / n, 2) if (n := sum(v is not None for v in xs)) else None)  # noqa: E731
        top = lambda xs: max((v for v in xs if v is not None), default=None)  # noqa: E731
        gpu, vram, ram, cpu, temp, hot, down, up, spilled, tps = cols
        last = lambda xs: next((v for v in reversed(xs) if v is not None), None)  # noqa: E731
        self.db.execute("INSERT OR REPLACE INTO minutes (t, gpu, gpu_max, vram, vram_max, ram, ram_max, cpu, temp, temp_max,"
                        " hotspot, hotspot_max, down, up, disks, spilled, tps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (self.minute, avg(gpu), top(gpu), avg(vram), top(vram), avg(ram), top(ram), avg(cpu), avg(temp),
                         top(temp), avg(hot), top(hot), avg(down), avg(up), json.dumps(self.disks), top(spilled), last(tps)))
        self.db.commit()
        self.acc = []
        if self.clock() - self.last_prune > 86400:
            self.last_prune = self.clock()
            cut = self.clock() - KEEP_DAYS * 86400
            self.db.execute("DELETE FROM minutes WHERE t < ?", (cut,))
            self.db.execute("DELETE FROM events WHERE t < ?", (cut,))
            self.db.commit()

    def _track_spill(self, s) -> None:
        """One `spill` event per spill, written when it ends: name = its verdict (slow / fine / unknown = nothing
        answered during it), detail = peak GB in system RAM and how long it lasted."""
        impact = getattr(s, "vram_spill_impact", "") or ""
        if impact:
            if self.spill is None:
                self.spill = {"start": s.sampled_at, "verdict": impact, "gb": 0.0}
            if SPILL_RANK[impact] > SPILL_RANK[self.spill["verdict"]]:
                self.spill["verdict"] = impact
            self.spill["gb"] = max(self.spill["gb"], getattr(s, "vram_spilled_gb", 0.0) or 0.0)
        elif self.spill is not None:
            sp, self.spill = self.spill, None
            minutes = (s.sampled_at - sp["start"]) / 60
            self.event("spill", sp["verdict"], f"{sp['gb']:.2f} GB, {minutes:.0f} min", sp["start"])

    def event(self, kind: str, name: str, detail: str = "", t: float | None = None) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (t or self.clock(), kind, name, detail))
        self.db.commit()

    def read_logs(self) -> None:
        """New Away / chain lines -> events (the kinds the week card counts)."""
        if self.away:
            self._events_from(self.away.new_lines(), True)
        if self.chains:
            self._events_from(self.chains.new_lines(), False)

    def _events_from(self, lines: list[str], away: bool, cut: float = 0.0) -> None:
        rows = []
        for line in lines:
            if away:
                try:
                    e = json.loads(line)
                    t = time.mktime(time.strptime(e["time"][:19], "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, KeyError):
                    continue
                kind = {"away-start": "away_start", "away-end": "away_end", "job-done": "job_done",
                        "job-failed": "job_failed"}.get(e.get("event"))
                if kind and t >= cut:
                    rows.append((t, kind, str(e.get("job") or e.get("model") or ""), str(e.get("reason") or e.get("why") or "")))
            else:
                m = RUN_RESULT.match(clean_log_line(line))
                if not m:
                    continue
                try:
                    t = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    t = self.clock()
                if t >= cut:
                    rows.append((t, "chain", m["name"].removesuffix(" request"), RUN_WORDS[m["status"]]))
        if rows:
            self.db.executemany("INSERT INTO events VALUES (?,?,?,?)", rows)
            self.db.commit()

    def close(self) -> None:
        self._flush()
        self.db.close()


# ---- reading (web requests: their own connection)
def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def series(path: Path, hours: float, now: float | None = None, max_points: int = 720) -> dict:
    """Chart columns for the last `hours` (same shape as the 1 h history: t + one list per series)."""
    now = now or time.time()
    if not path.exists():
        return {"t": [], **{c: [] for c in COLS}}
    span = hours * 3600
    bucket = max(60, int(span / max_points // 60 * 60))          # 24 h -> 2-min buckets, 7 d -> 15-min buckets
    with _connect(path) as con:
        rows = con.execute(
            f"SELECT (t / {bucket}) * {bucket} AS b, AVG(gpu), AVG(vram), AVG(ram), AVG(cpu), AVG(temp), AVG(hotspot), "
            f"AVG(down), AVG(up) FROM minutes WHERE t >= ? GROUP BY b ORDER BY b", (now - span,)).fetchall()
    out = {"t": [r[0] + bucket / 2 for r in rows]}
    for i, c in enumerate(COLS, start=1):
        out[c] = [None if r[i] is None else round(r[i], 2) for r in rows]
    return out


def week(path: Path, now: float | None = None, days: int = 7) -> dict:
    """The "this week" card: Away hours, jobs, chains, crashes, peaks, disk change per drive."""
    now = now or time.time()
    since = now - days * 86400
    empty = {"days": days, "away_hours": 0.0, "away_runs": 0, "jobs_done": 0, "jobs_failed": 0, "chains": {},
             "crashes": 0, "spills": 0, "spills_slow": 0, "hotspot_max": None, "vram_max": None, "gpu_avg": None, "disks": {}, "covered_hours": 0.0}
    if not path.exists():
        return empty
    with _connect(path) as con:
        ev = con.execute("SELECT t, kind, name, detail FROM events WHERE t >= ? ORDER BY t", (since,)).fetchall()
        peaks = con.execute("SELECT MAX(hotspot_max), MAX(vram_max), AVG(gpu), COUNT(*) FROM minutes WHERE t >= ?", (since,)).fetchone()
        first = con.execute("SELECT disks FROM minutes WHERE t >= ? AND disks IS NOT NULL ORDER BY t LIMIT 1", (since,)).fetchone()
        last = con.execute("SELECT disks FROM minutes WHERE t >= ? AND disks IS NOT NULL ORDER BY t DESC LIMIT 1", (since,)).fetchone()
    out = dict(empty)
    start = None
    for t, kind, name, detail in ev:
        if kind == "away_start":
            start = t
            out["away_runs"] += 1
        elif kind == "away_end" and start is not None:
            out["away_hours"] += (t - start) / 3600
            start = None
        elif kind == "job_done":
            out["jobs_done"] += 1
        elif kind == "job_failed":
            out["jobs_failed"] += 1
        elif kind == "chain":
            out["chains"][detail] = out["chains"].get(detail, 0) + 1
        elif kind == "crash":
            out["crashes"] += 1
        elif kind == "spill":
            out["spills"] += 1
            out["spills_slow"] += name == "slow"
    if start is not None:                                       # still away now
        out["away_hours"] += (now - start) / 3600
    out["away_hours"] = round(out["away_hours"], 1)
    out["hotspot_max"], out["vram_max"] = peaks[0], (round(peaks[1], 1) if peaks[1] is not None else None)
    out["gpu_avg"] = round(peaks[2], 1) if peaks[2] is not None else None
    out["covered_hours"] = round(peaks[3] / 60, 1)
    if first and last:
        a, b = json.loads(first[0] or "{}"), json.loads(last[0] or "{}")
        out["disks"] = {d: round(b[d] - a[d], 1) for d in b if d in a}
    return out
