"""The machine's week, as a note (plans/SOL_HUD_IMPROVEMENTS_PLAN.md E2).

Sunday 20:00 (or the first look after it, if the PC was off), once a week: `1Notebook\\Digests\\<year>-W<week> machine.md`
next to the Weekly digest chain's own `<year>-W<week>.md` (that one is the AI's summary of your notes and commits; this
one is exact numbers from data\\metrics.sqlite, no AI call). Then one notification. Never during quiet hours: it
waits for them to end, like the notifications do.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from . import metrics
from .paths import DATA_DIR

NOTES_DIR = Path(r"D:\OBVLT\1Notebook\Digests")
STATE_FILE = DATA_DIR / "digest-state.json"
DUE_WEEKDAY, DUE_HOUR = 6, 20          # Sunday 20:00, like the Weekly digest chain
LOW_FREE_PCT = 15                      # drives under this much free get a "days left at this rate"


def week_end(now: datetime) -> datetime:
    """The most recent Sunday 20:00 at or before `now`."""
    end = (now - timedelta(days=(now.weekday() - DUE_WEEKDAY) % 7)).replace(hour=DUE_HOUR, minute=0, second=0, microsecond=0)
    return end if end <= now else end - timedelta(days=7)


def week_name(end: datetime) -> str:
    iso = end.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def load_state(path: Path | None = None) -> dict:
    try:
        return json.loads((path or STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE_FILE
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def due(now: datetime, state: dict) -> datetime | None:
    """The week end to write now, or None. A first start with no state counts the current week as done (no digest
    of a week that ended days ago the moment this ships); the caller saves that."""
    end = week_end(now)
    if not state.get("last"):
        state["last"] = week_name(end)
        return None
    return end if state["last"] != week_name(end) else None


def _speeds(con: sqlite3.Connection, start: float, end: float) -> list[float]:
    """Answer speeds in the window. A minute row repeats the last answer's speed until the next one, so only changes
    count (otherwise a long idle stretch would outvote every other answer)."""
    out, last = [], None
    for (tps,) in con.execute("SELECT tps FROM minutes WHERE t >= ? AND t < ? AND tps IS NOT NULL ORDER BY t", (start, end)):
        if tps != last:
            out.append(tps)
            last = tps
    return out


def build(db: Path, end: datetime, disks_now: list[dict] | None = None) -> str:
    """The note's text for the 7 days ending at `end`."""
    t_end = end.timestamp()
    t_start = t_end - 7 * 86400
    this = metrics.week(db, now=t_end)
    prev = metrics.week(db, now=t_start)
    failed_jobs, failed_chains, hot_at, speeds = [], {}, None, []
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            for name, detail in con.execute("SELECT name, detail FROM events WHERE kind = 'job_failed' AND t >= ? AND t < ? "
                                            "ORDER BY t", (t_start, t_end)):
                failed_jobs.append(f"{name}" + (f" ({detail})" if detail else ""))
            for (name,) in con.execute("SELECT name FROM events WHERE kind = 'chain' AND detail = 'failed' AND t >= ? AND t < ?",
                                       (t_start, t_end)):
                failed_chains[name] = failed_chains.get(name, 0) + 1
            row = con.execute("SELECT t FROM minutes WHERE t >= ? AND t < ? AND hotspot_max IS NOT NULL "
                              "ORDER BY hotspot_max DESC LIMIT 1", (t_start, t_end)).fetchone()
            hot_at = row[0] if row else None
            speeds = _speeds(con, t_start, t_end)
        finally:
            con.close()

    def vs(key: str, unit: str = "") -> str:
        a, b = this.get(key), prev.get(key)
        if b in (None, 0) or a is None or prev.get("covered_hours", 0) < 24:
            return ""
        return f" (last week {b:g}{unit})" if isinstance(b, (int, float)) else f" (last week {b}{unit})"

    lines = [f"# {week_name(end)}: the machine's week",
             "",
             f"_{datetime.fromtimestamp(t_start):%a %d %b %H:%M} to {end:%a %d %b %H:%M}, from SOL Control HUD's history"
             f" ({this['covered_hours']:.0f} of 168 h recorded). Exact numbers, no AI._",
             "",
             "## Away and chains",
             f"- Away: {this['away_hours']} h over {this['away_runs']} run{'s' if this['away_runs'] != 1 else ''}"
             f"{vs('away_hours', ' h')}; jobs {this['jobs_done']} done, {this['jobs_failed']} failed.",
             ]
    for j in failed_jobs[:8]:
        lines.append(f"  - failed: {j}")
    runs = sum(n for k, n in this["chains"].items() if k in ("finished", "failed"))
    lines.append(f"- Chains: {runs} run{'s' if runs != 1 else ''}, {this['chains'].get('failed', 0)} failed"
                 + (f" ({', '.join(f'{k} x{n}' if n > 1 else k for k, n in failed_chains.items())})" if failed_chains else "") + ".")
    lines += ["", "## Local AI"]
    if speeds:
        lines.append(f"- Typical answer speed: {statistics.median(speeds):.0f} tok/s over {len(speeds)} "
                     f"answer{'s' if len(speeds) != 1 else ''} (slowest {min(speeds):.0f}, fastest {max(speeds):.0f}).")
    else:
        lines.append("- No answer speeds recorded this week.")
    lines.append(f"- VRAM spills: {this.get('spills', 0)}, of which {this.get('spills_slow', 0)} slowed answers.")
    lines += ["", "## Hardware"]
    if this["hotspot_max"] is not None:
        when = f" ({datetime.fromtimestamp(hot_at):%a %H:%M})" if hot_at else ""
        lines.append(f"- Hottest GPU hotspot: {this['hotspot_max']:.0f} °C{when}{vs('hotspot_max', ' °C')}.")
    if this["vram_max"] is not None:
        lines.append(f"- Peak VRAM: {this['vram_max']} GB; average GPU load {this['gpu_avg']} %.")
    lines.append(f"- Crash events: {this['crashes']}.")
    if this["disks"]:
        lines += ["", "## Disks"]
        free_now = {str(d.get("drive")): d for d in (disks_now or [])}
        for drive, change in sorted(this["disks"].items()):
            note = ""
            d = free_now.get(drive)
            if d and change < 0 and (100 - (d.get("percent") or 0)) < LOW_FREE_PCT:
                days = (d.get("free_gb") or 0) / (-change / 7)
                note = f"; {d.get('free_gb', 0):.0f} GB free, about {days:.0f} days left at this rate"
            lines.append(f"- {drive}: {'+' if change > 0 else ''}{change} GB free over the week{note}.")
    return "\n".join(lines) + "\n"


def write(text: str, end: datetime, notes_dir: Path | None = None) -> Path:
    notes_dir = notes_dir or NOTES_DIR
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{week_name(end)} machine.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def summary_line(db: Path, end: datetime) -> str:
    w = metrics.week(db, now=end.timestamp())
    return (f"Away {w['away_hours']} h · {w['jobs_done']} jobs · chains {sum(w['chains'].values())} · "
            f"crashes {w['crashes']} · spills {w.get('spills', 0)}")
