"""SQLite run history. A run is never left 'running' across restarts (the old HUD's zombie-session bug).

Since 2026-09-25 (chains, OBVLT plans\\LOCAL_AI_AUTOMATION_PLAN.md phase 1):
- every finished step (and every finished for_each item) is saved as it completes (`step_outputs`), so a run can resume;
- a run has an owner (pid + process start time + heartbeat). Recovery only touches runs whose owner process is gone,
  so starting one pipelines command never breaks another command's live run;
- cancel can come from another process (`request_cancel`); the owner sees it before its next step or item.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import psutil

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow TEXT NOT NULL,
  status TEXT NOT NULL,            -- queued | running | succeeded | failed | cancelled | interrupted | needs_user | waiting
  inputs TEXT NOT NULL,
  outputs TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  at REAL NOT NULL,
  step TEXT,
  kind TEXT NOT NULL,              -- run_started | step_started | attempt | step_succeeded | step_failed | escalated | run_finished | note | ...
  data TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS step_outputs (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  step TEXT NOT NULL,
  item INTEGER NOT NULL,           -- -1 = the whole step; 0.. = one for_each item
  output TEXT NOT NULL,
  at REAL NOT NULL,
  PRIMARY KEY (run_id, step, item)
);
"""
# columns added after the first release (ALTER TABLE on open, so old databases keep working)
RUN_COLUMNS = {"owner_pid": "INTEGER", "owner_started": "REAL", "heartbeat": "REAL", "source": "TEXT",
               "cancel_requested": "INTEGER DEFAULT 0"}

ACTIVE = ("queued", "running")
RESUMABLE = ("interrupted", "waiting", "needs_user", "failed", "cancelled")
WHOLE = -1


def _this_process() -> tuple[int, float]:
    return os.getpid(), psutil.Process().create_time()


def owner_alive(pid: int | None, started: float | None) -> bool:
    """Same pid AND same process start time (Windows reuses pids)."""
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        return started is None or abs(p.create_time() - float(started)) < 1.0
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")  # the chain runner and CLI commands share the file
            self._db.executescript(SCHEMA)
            have = {r["name"] for r in self._db.execute("PRAGMA table_info(runs)")}
            for col, kind in RUN_COLUMNS.items():
                if col not in have:
                    self._db.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")

    def recover_after_restart(self) -> int:
        """Mark active runs whose owner process is gone as interrupted (resumable). Live runs of other processes are left alone."""
        now = time.time()
        rows = self._db.execute("SELECT id, owner_pid, owner_started FROM runs WHERE status IN (?, ?)", ACTIVE).fetchall()
        dead = [r["id"] for r in rows if not owner_alive(r["owner_pid"], r["owner_started"])]
        with self._lock, self._db:
            for rid in dead:
                self._db.execute(
                    "UPDATE runs SET status='interrupted', finished_at=?, error=COALESCE(error, 'process ended while the run was active') "
                    "WHERE id=?", (now, rid))
        return len(dead)

    def create_run(self, workflow: str, inputs: dict, source: str | None = None) -> int:
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO runs(workflow, status, inputs, created_at, source) VALUES (?, 'queued', ?, ?, ?)",
                                   (workflow, json.dumps(inputs), time.time(), source))
            return int(cur.lastrowid)

    def claim(self, run_id: int) -> None:
        """This process now owns the run (start or resume)."""
        pid, started = _this_process()
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET owner_pid=?, owner_started=?, heartbeat=?, cancel_requested=0, "
                             "error=NULL, finished_at=NULL WHERE id=?", (pid, started, time.time(), run_id))

    def heartbeat(self, run_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET heartbeat=? WHERE id=?", (time.time(), run_id))

    def set_status(self, run_id: int, status: str, *, outputs: dict | None = None, error: str | None = None) -> None:
        now = time.time()
        fields = ["status=?"]
        values: list = [status]
        if status == "running":
            fields.append("started_at=COALESCE(started_at, ?)"); values.append(now)
        if status not in ACTIVE:
            fields.append("finished_at=?"); values.append(now)
        if outputs is not None:
            fields.append("outputs=?"); values.append(json.dumps(outputs))
        if error is not None:
            fields.append("error=?"); values.append(error)
        with self._lock, self._db:
            self._db.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id=?", (*values, run_id))

    def event(self, run_id: int, kind: str, step: str | None = None, **data) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO events(run_id, at, step, kind, data) VALUES (?, ?, ?, ?, ?)",
                             (run_id, time.time(), step, kind, json.dumps(data, default=str)))
            self._db.execute("UPDATE runs SET heartbeat=? WHERE id=?", (time.time(), run_id))

    # ---- per-step outputs (resume)
    def save_output(self, run_id: int, step: str, output, item: int = WHOLE) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO step_outputs(run_id, step, item, output, at) VALUES (?, ?, ?, ?, ?)",
                             (run_id, step, item, json.dumps(output), time.time()))

    def saved_outputs(self, run_id: int) -> dict[str, dict[int, object]]:
        """{step: {item: output}}; item -1 is the whole step."""
        out: dict[str, dict[int, object]] = {}
        for r in self._db.execute("SELECT step, item, output FROM step_outputs WHERE run_id=?", (run_id,)):
            out.setdefault(r["step"], {})[r["item"]] = json.loads(r["output"])
        return out

    def forget_outputs(self, run_id: int, steps: list[str]) -> None:
        """Drop saved outputs (e.g. the steps after an edited step, so they run again)."""
        with self._lock, self._db:
            self._db.executemany("DELETE FROM step_outputs WHERE run_id=? AND step=?", [(run_id, s) for s in steps])

    def set_inputs(self, run_id: int, inputs: dict) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET inputs=? WHERE id=?", (json.dumps(inputs), run_id))

    def latest_run(self, source: str) -> dict | None:
        """The newest run started from this workflow file / chain note."""
        row = self._db.execute("SELECT * FROM runs WHERE source=? ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        return dict(row) if row else None

    # ---- cancel from any process
    def request_cancel(self, run_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))

    def cancel_requested(self, run_id: int) -> bool:
        row = self._db.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def run(self, run_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def events(self, run_id: int, after_id: int = 0) -> list[dict]:
        rows = self._db.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]
