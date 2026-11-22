"""Read-only AI session & handoff snapshot for the Projects card (zero LLM tokens)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

NOMORE_DB = Path(r"D:\Workspace\nomorechats\runs.sqlite")
AG_DB = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
CLAUDE_LOG_DIR = Path(r"D:\AI\Cache\claude-log")
WORKSPACE_ROOT = Path(r"D:\Workspace")

_CACHE_TTL_S = 10.0
_cache: tuple[float, dict[str, dict], dict[str, dict]] = (0.0, {}, {})


def _project_from_uri(raw_uris: str, workspace_root: Path = WORKSPACE_ROOT) -> list[str]:
    if not raw_uris or not raw_uris.strip():
        return []
    try:
        items = json.loads(raw_uris)
        if not isinstance(items, list):
            return []
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    root = workspace_root.resolve()
    for uri in items:
        if not isinstance(uri, str):
            continue
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            continue
        p = unquote(parsed.path)
        if os.name == "nt" and len(p) >= 3 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        try:
            rp = Path(p).resolve()
            if rp.is_relative_to(root) and rp != root:
                out.append(rp.relative_to(root).parts[0])
        except OSError:
            continue
    return out


def _read_handoffs(db_path: Path = NOMORE_DB) -> dict[str, dict]:
    if not db_path.exists():
        return {}
    uri = f"file:///{db_path.resolve().as_posix().lstrip('/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT h.* FROM handoffs h
            INNER JOIN (
                SELECT project, MAX(updated_at) AS max_u FROM handoffs GROUP BY project
            ) latest ON h.project = latest.project AND h.updated_at = latest.max_u
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    out: dict[str, dict] = {}
    for r in rows:
        out[str(r["project"])] = {
            "summary": str(r["summary"]),
            "next_steps": json.loads(r["next_steps_json"] or "[]"),
            "blockers": json.loads(r["blockers_json"] or "[]"),
            "status": str(r["status"]),
            "source_agent": str(r["source_agent"]),
            "updated_at": str(r["updated_at"]),
        }
    return out


def _read_sessions(
    ag_db: Path = AG_DB,
    claude_dir: Path = CLAUDE_SESSIONS_DIR,
    claude_log_dir: Path = CLAUDE_LOG_DIR,
    workspace_root: Path = WORKSPACE_ROOT,
) -> dict[str, dict]:
    sessions: dict[str, dict] = {}
    root = workspace_root.resolve()

    if ag_db.exists():
        uri = f"file:///{ag_db.resolve().as_posix().lstrip('/')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1.5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT title, workspace_uris, status, last_modified_time
                FROM conversation_summaries
                WHERE COALESCE(parent_conversation_id, '') = ''
                ORDER BY last_modified_time DESC
                LIMIT 40
                """
            ).fetchall()
            conn.close()
            for r in rows:
                running = r["status"] == "CASCADE_RUN_STATUS_RUNNING"
                for proj in _project_from_uri(r["workspace_uris"] or "", workspace_root):
                    cur = sessions.get(proj)
                    if not cur or (running and not cur.get("active")):
                        sessions[proj] = {
                            "provider": "antigravity",
                            "active": running,
                            "title": str(r["title"] or ""),
                            "updated_at": str(r["last_modified_time"] or ""),
                        }
        except sqlite3.Error:
            pass

    if claude_dir.exists():
        for f in claude_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cwd = Path(str(data.get("cwd") or "")).resolve()
                if cwd.is_relative_to(root) and cwd != root:
                    proj = cwd.relative_to(root).parts[0]
                    sessions[proj] = {
                        "provider": "claude",
                        "active": True,
                        "title": "Claude session",
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
            except (OSError, json.JSONDecodeError):
                continue

    today_log = claude_log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    if today_log.exists():
        try:
            for line in reversed(today_log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]):
                if not line.strip():
                    continue
                rec = json.loads(line)
                git_root = (rec.get("git") or {}).get("root") or rec.get("cwd") or ""
                rp = Path(str(git_root)).resolve()
                if rp.is_relative_to(root) and rp != root:
                    proj = rp.relative_to(root).parts[0]
                    if proj not in sessions:
                        sessions[proj] = {
                            "provider": "claude",
                            "active": False,
                            "title": str(rec.get("reply") or "")[:80],
                            "updated_at": str(rec.get("time") or ""),
                        }
        except (OSError, json.JSONDecodeError):
            pass

    return sessions


def collect_for_projects() -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (handoffs_by_project, sessions_by_project), cached for 10s."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < _CACHE_TTL_S:
        return _cache[1], _cache[2]
    handoffs = _read_handoffs()
    sessions = _read_sessions()
    _cache = (now, handoffs, sessions)
    return handoffs, sessions
