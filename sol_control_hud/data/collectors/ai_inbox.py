"""The "From your AI" card: what the local AI wrote for you and what is worth reading.

Files only, mtime-checked and cheap (the collector runs every 30 s): the newest weekly digest, the open lines of
Tasks.md, the newest morning briefing, the chains that need you, the Bug hunt's real issues. No subprocess, no model
call, no HTTP. The page never sends a path: it sends an id ("digest", "briefing", "bughunt", "tasks", "chain:<name>")
and the server maps it to a file here. Ticking a task rewrites that one exact line, atomically.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from ...swallow import note as _swallowed

DIGESTS_DIR = Path(r"D:\OBVLT\1Notebook\Digests")
TASKS_FILE = Path(r"D:\OBVLT\1Notebook\Tasks.md")
BRIEFINGS_DIR = Path(r"D:\AI\Vault\Daily_Briefings")
BUGHUNT_FILE = Path(r"D:\OBVLT\1Notebook\Chains\Results\Bug hunt (result).md")

MAX_TASKS = 8
MAX_BUGS = 5
MAX_LOOPS = 6
MAX_LINE = 260
BUGHUNT_FRESH_DAYS = 14
NEEDS_STATUSES = ("needs-you", "needs_user", "failed")

DIGEST_RE = re.compile(r"^\d{4}-W\d{2}\.md$")          # not "2026-W46 machine.md"
_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
_OPEN_TASK = re.compile(r"^(\s*)- \[ \] (.*)$")
_FRONT = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_SRC_BRACKET = re.compile(r"\s*\[[^\[\]]*\]\s*$")
_WIKI = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")


# ------------------------------------------------------------------ pure parsing
def plain(s: str, limit: int = MAX_LINE) -> str:
    """One line of markdown as plain text: wiki links -> their label, no bold/code marks, shortened."""
    s = _WIKI.sub(r"\1", s)
    s = s.replace("**", "").replace("`", "")
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def strip_source(s: str) -> str:
    """'Do the thing. [the open loop it came from]' -> 'Do the thing.'"""
    return _SRC_BRACKET.sub("", s).strip()


def section_lines(text: str, heading: str) -> list[str]:
    """The lines under `## heading` (case-insensitive) up to the next `## `. Front matter is skipped."""
    text = _FRONT.sub("", text, count=1)
    out, inside = [], False
    for ln in text.splitlines():
        m = _HEADING.match(ln)
        if m:
            inside = m.group(1).strip().lower() == heading.lower()
            continue
        if inside:
            out.append(ln)
    return out


def section_bullets(text: str, heading: str, strip_src: bool = False) -> list[str]:
    out = []
    for ln in section_lines(text, heading):
        m = _BULLET.match(ln)
        if m:
            item = strip_source(m.group(1)) if strip_src else m.group(1)
            item = plain(item)
            if item and item.lower() != "none noted." and item.lower() != "none.":
                out.append(item)
    return out


def section_paragraph(text: str, heading: str) -> str:
    return plain(" ".join(ln.strip() for ln in section_lines(text, heading) if ln.strip()), 400)


def front_value(text: str, key: str) -> str:
    m = _FRONT.match(text)
    if not m:
        return ""
    for ln in m.group(1).splitlines():
        k, _, v = ln.partition(":")
        if k.strip().lower() == key:
            return v.strip().strip("'\"")
    return ""


def parse_digest(text: str) -> dict:
    return {"date": front_value(text, "date"),
            "next": section_bullets(text, "Next week", strip_src=True),
            "loops": section_bullets(text, "Open loops", strip_src=True)[:MAX_LOOPS]}


def parse_briefing(text: str) -> dict:
    return {"focus": section_paragraph(text, "Focus"), "attention": section_bullets(text, "Needs attention"),
            "date": front_value(text, "date")}


def parse_tasks(text: str) -> list[dict]:
    """Every open `- [ ]` line: {line (0-based index), text}. The index is only a hint: ticking re-checks the text."""
    out = []
    for i, ln in enumerate(text.split("\n")):
        m = _OPEN_TASK.match(ln.rstrip("\r"))
        if m and m.group(2).strip():
            out.append({"line": i, "text": m.group(2).strip()})
    return out


def parse_bug_hunt(text: str) -> list[str]:
    """The '## Real issues' entries: bullets, or bare lines like '**[high]** `file`: what'."""
    out = []
    for ln in section_lines(text, "Real issues"):
        s = ln.strip()
        if not s:
            continue
        m = _BULLET.match(ln)
        out.append(plain(m.group(1) if m else s))
    return out


# ------------------------------------------------------------------ files
def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def newest_digest(d: Path) -> Path | None:
    try:
        names = sorted(f.name for f in d.iterdir() if DIGEST_RE.match(f.name) and f.is_file())
    except OSError:
        return None
    return d / names[-1] if names else None


def newest_briefing(d: Path) -> Path | None:
    best, best_t = None, -1.0
    try:
        for f in d.glob("*.md"):
            t = _mtime(f)
            if t is not None and t > best_t:
                best, best_t = f, t
    except OSError:
        return None
    return best


def needs_chains(chains_dir: Path | None = None) -> tuple[list[dict], float]:
    """Chain notes whose status says the last run needs you or failed (the same note status the Chains card shows)."""
    from ... import control
    out, newest = [], 0.0
    try:
        paths = control._chain_paths(chains_dir)
    except OSError:
        _swallowed("ai_inbox.needs_chains")
        return [], 0.0
    for name, p in paths.items():
        try:
            meta = control._front(p)
        except Exception:                                   # noqa: BLE001 - an unreadable chain note is just not listed
            _swallowed("ai_inbox.needs_chains.front")
            continue
        status = str(meta.get("status") or "").lower()
        if status in NEEDS_STATUSES:
            reason = str(meta.get("reason") or meta.get("error") or meta.get("problem") or "")
            out.append({"name": name, "status": "failed" if status == "failed" else "needs you", "reason": plain(reason, 200)})
            newest = max(newest, _mtime(p) or 0.0)
    return out, newest


def collect(digests: Path | None = None, tasks: Path | None = None, briefings: Path | None = None,
            bughunt: Path | None = None, chains_dir: Path | None = None, now: float | None = None) -> dict:
    digests, tasks, briefings = digests or DIGESTS_DIR, tasks or TASKS_FILE, briefings or BRIEFINGS_DIR
    bughunt = bughunt or BUGHUNT_FILE
    now = time.time() if now is None else now
    out: dict = {"available": True}

    dp = newest_digest(digests)
    text = _read(dp) if dp else None
    if dp and text is not None:
        d = parse_digest(text)
        out["digest"] = {**d, "name": dp.stem, "mtime": _mtime(dp), "date": d["date"] or time.strftime("%Y-%m-%d", time.localtime(_mtime(dp) or now))}

    text = _read(tasks)
    if text is not None:
        allt = parse_tasks(text)
        out["tasks"] = {"open": allt[:MAX_TASKS], "more": max(0, len(allt) - MAX_TASKS), "mtime": _mtime(tasks)}

    bp = newest_briefing(briefings)
    text = _read(bp) if bp else None
    if bp and text is not None:
        b = parse_briefing(text)
        if b["focus"] or b["attention"]:
            out["briefing"] = {**b, "name": bp.stem, "mtime": _mtime(bp)}

    chains, ctime = needs_chains(chains_dir)
    if chains:
        out["needs"] = {"chains": chains, "mtime": ctime}

    bt = _mtime(bughunt)
    if bt is not None and now - bt <= BUGHUNT_FRESH_DAYS * 86400:
        text = _read(bughunt)
        items = parse_bug_hunt(text) if text is not None else []
        if items:
            out["bughunt"] = {"items": items[:MAX_BUGS], "more": max(0, len(items) - MAX_BUGS), "mtime": bt}
    return out


# ------------------------------------------------------------------ actions (the page sends ids, never paths)
def resolve(note_id: str, digests: Path | None = None, tasks: Path | None = None, briefings: Path | None = None,
            bughunt: Path | None = None, chains_dir: Path | None = None) -> Path | None:
    """The file behind a known id, or None."""
    if note_id == "digest":
        return newest_digest(digests or DIGESTS_DIR)
    if note_id == "briefing":
        return newest_briefing(briefings or BRIEFINGS_DIR)
    if note_id == "tasks":
        p = tasks or TASKS_FILE
        return p if p.is_file() else None
    if note_id == "bughunt":
        p = bughunt or BUGHUNT_FILE
        return p if p.is_file() else None
    if note_id.startswith("chain:"):
        from ... import control
        try:
            return control._chain_paths(chains_dir).get(note_id[6:])
        except OSError:
            _swallowed("ai_inbox.resolve")
    return None


def open_note(note_id: str, opener=None, **paths) -> dict:
    p = resolve(str(note_id), **paths)
    if p is None:
        return {"ok": False, "why": "nothing to open"}
    if opener is None:
        from ...obsidian_open import open_path as opener
    try:
        opener(p)
    except OSError as e:
        _swallowed("ai_inbox.open_note")
        return {"ok": False, "why": f"could not open: {e}"}
    return {"ok": True, "why": f"opened {p.stem}"}


def tick_task(text: str, line: int, tasks: Path | None = None) -> dict:
    """Turn `- [ ] <text>` into `- [x] <text>` on exactly line `line`, only if that line still is that open task and
    the file didn't change between reading and replacing it (you may be typing in Obsidian). Bytes are kept as they
    are (line endings, everything else)."""
    path = tasks or TASKS_FILE
    text = str(text or "").strip()
    if not text or not isinstance(line, int) or isinstance(line, bool) or line < 0:
        return {"ok": False, "why": "bad task"}
    try:
        before = os.stat(path).st_mtime_ns
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "why": "Tasks.md can't be read"}
    lines = raw.split("\n")
    m = _OPEN_TASK.match(lines[line].rstrip("\r")) if line < len(lines) else None
    if not m or m.group(2).strip() != text:
        return {"ok": False, "why": "that task moved or was already ticked: reload"}
    ln = lines[line]
    cr = "\r" if ln.endswith("\r") else ""
    lines[line] = f"{m.group(1)}- [x] {m.group(2)}{cr}"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes("\n".join(lines).encode("utf-8"))
        if os.stat(path).st_mtime_ns != before:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "why": "Tasks.md changed just now: try again"}
        os.replace(tmp, path)
    except OSError as e:
        _swallowed("ai_inbox.tick_task")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "why": f"could not write Tasks.md: {e}"}
    return {"ok": True, "why": "ticked off"}


def digest_notice_key(payload: dict) -> str:
    """Identity of the newest digest for the once-per-digest ticker line ('' when there is none)."""
    d = (payload or {}).get("digest") or {}
    return hashlib.sha1(f"{d.get('name')}".encode()).hexdigest()[:12] if d.get("name") else ""
