"""What the dashboard gets besides the ticker's snapshot: a one-hour history for the charts, the Away queue, a recent
activity feed. Pure functions + one small ring buffer; the hub owns the instances."""
from __future__ import annotations

import os
import re
import json
import threading
from collections import deque
from pathlib import Path

from ...data.snapshot import RED, AMBER, GREEN, CYAN, MUTED, RUN_RESULT, RUN_WORDS, clean_log_line

LLM_DIR = Path(r"D:\AI\Cache\llm")
QUEUE_DIR = Path(r"D:\AI\Queue")
DOCTOR_LOG = Path(r"D:\OBVLT\reports\doctor\history.log")
BRIEFING_LOG = Path(r"D:\OBVLT\reports\briefing.log")
TASKS_FILE = Path(os.environ.get("SOL_TASKS_FILE", r"D:\AI\Cache\llm\tasks.json"))

SERIES = ("gpu", "vram", "ram", "cpu", "temp", "hotspot", "down", "up")


class History:
    """The last hour of the numbers the charts draw, one point per collector sample (2 s .. 10 s apart)."""

    def __init__(self, seconds: float = 3600.0):
        self.seconds = seconds
        self.points: deque[tuple] = deque()
        self._last_sample = 0.0
        self._lock = threading.Lock()

    def add(self, s) -> bool:
        """Append the snapshot's numbers if it's a new sample. Returns True when something was added."""
        if not s.sampled_at or s.sampled_at == self._last_sample:
            return False
        self._last_sample = s.sampled_at
        point = (round(s.sampled_at, 1), s.gpu_load, s.vram_used_gb, round(s.ram_used_gb, 2), round(s.cpu_percent, 1),
                 s.gpu_temp, s.gpu_hotspot, round(s.net_down_kb, 1), round(s.net_up_kb, 1))
        with self._lock:
            self.points.append(point)
            while self.points and s.sampled_at - self.points[0][0] > self.seconds:
                self.points.popleft()
        return True

    def export(self, max_points: int = 600) -> dict:
        """Columns for the charts, thinned to at most max_points (every n-th point, the newest always kept)."""
        with self._lock:
            pts = list(self.points)
        if len(pts) > max_points:
            step = len(pts) / max_points
            pts = [pts[int(i * step)] for i in range(max_points - 1)] + [pts[-1]]
        return {"t": [p[0] for p in pts], **{name: [p[i + 1] for p in pts] for i, name in enumerate(SERIES)}}

    def latest(self) -> dict | None:
        with self._lock:
            if not self.points:
                return None
            p = self.points[-1]
        return {"t": p[0], **{name: p[i + 1] for i, name in enumerate(SERIES)}}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def away_info(llm_dir: Path = LLM_DIR, queue_dir: Path = QUEUE_DIR) -> dict:
    """Away mode as the dashboard shows it: state, the running job's progress, what's queued, what's running."""
    names = lambda d: sorted((p.stem for p in d.glob("*.json")), key=str.lower) if d.is_dir() else []  # noqa: E731
    state = _read_json(llm_dir / "state.json") or {}
    return {"mode": state.get("mode"), "reason": state.get("reason"), "until": state.get("until"),
            "present": bool(state.get("present")), "sleep_when_done": bool(state.get("sleepWhenDone")),
            "progress": _read_json(llm_dir / "progress.json"), "queued": names(queue_dir),
            "running": names(queue_dir / "running"), "screen": (llm_dir / "away-screen.json").exists()}


AWAY_WORDS = {  # away.jsonl event -> (words, color)
    "away-start": ("Away started", CYAN), "away-end": ("Away ended", MUTED), "job-start": ("job started", CYAN),
    "job-done": ("job done", GREEN), "job-failed": ("job failed", RED), "job-requeued": ("job back in the queue", AMBER),
    "game-off": ("AI off for a game", AMBER), "ram-guard": ("RAM guard stopped Away", RED),
    "user-present": ("you came back (AI kept working)", CYAN), "queue-done": ("queue done", GREEN),
    "sleep": ("PC went to sleep", MUTED), "sleep-cancelled": ("sleep cancelled", AMBER),
}


def _tail_lines(path: Path, max_bytes: int = 16384) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "ignore").splitlines()[1 if size > max_bytes else 0:]
    except OSError:
        return []


def activity(away_log: Path = LLM_DIR / "away.jsonl", chains_log: Path = LLM_DIR / "chains.log",
             doctor_log: Path | None = None, briefing_log: Path | None = None,
             tasks_file: Path | None = None, n: int = 12) -> list[dict]:
    """The latest things that happened (Away events, chains, doctor checks, briefings, and tasks), newest first: {time, text, color}."""
    items = []
    # Only use machine defaults when called without custom paths (e.g. tests provide tmp_path)
    if doctor_log is None and away_log == LLM_DIR / "away.jsonl":
        doctor_log = DOCTOR_LOG
    if briefing_log is None and away_log == LLM_DIR / "away.jsonl":
        briefing_log = BRIEFING_LOG
    if tasks_file is None and away_log == LLM_DIR / "away.jsonl":
        tasks_file = TASKS_FILE

    for line in _tail_lines(away_log):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        words, color = AWAY_WORDS.get(e.get("event"), (e.get("event"), MUTED))
        extra = e.get("job") or e.get("what") or e.get("reason") or e.get("model") or ""
        why = e.get("why")
        items.append({"time": e.get("time", ""), "text": f"{words}{': ' + str(extra) if extra else ''}"
                                                          f"{' (' + str(why) + ')' if why else ''}", "color": color})
    for line in _tail_lines(chains_log):
        stamp, rest = line[:19], clean_log_line(line)
        m = RUN_RESULT.match(rest)
        if m:
            word = RUN_WORDS[m["status"]]
            color = {"finished": GREEN, "failed": RED}.get(word, AMBER)
            items.append({"time": stamp, "text": f"chain {m['name']}: {word}", "color": color})
        elif " start " in f" {rest} " and rest.startswith("start "):
            items.append({"time": stamp, "text": f"chain {rest[6:].split(' (')[0].removesuffix('.md')} started",
                          "color": CYAN})

    if doctor_log and doctor_log.exists():
        for line in _tail_lines(doctor_log):
            m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+exit=(\d+)", line)
            if m:
                stamp, code = m.group(1), int(m.group(2))
                color = GREEN if code == 0 else (AMBER if code == 1 else RED)
                status_txt = "all OK" if code == 0 else ("warnings" if code == 1 else "failures")
                items.append({"time": stamp, "text": f"doctor: stability check {status_txt} (exit {code})", "color": color})

    if briefing_log and briefing_log.exists():
        for line in _tail_lines(briefing_log):
            m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\[\w+\]\s+wrote\s+(.+)$", line)
            if m:
                stamp, name = m.group(1), m.group(2)
                items.append({"time": stamp, "text": f"briefing: wrote {name.split(' (')[0]}", "color": GREEN})

    if tasks_file and tasks_file.exists():
        try:
            tdata = json.loads(tasks_file.read_text(encoding="utf-8-sig"))
            act = tdata.get("active")
            if isinstance(act, dict) and act.get("task") and act.get("started"):
                desc_str = f" ({act['desc']})" if act.get("desc") else ""
                items.append({"time": act["started"][:19], "text": f"task: {act['task']}{desc_str} running", "color": CYAN})
        except Exception:
            pass

    items = [i for i in items if len(i["time"]) >= 16]
    items.sort(key=lambda i: i["time"], reverse=True)
    return items[:n]

