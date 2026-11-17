"""Watch-folder ingestion pipelines (LOCAL_AI_CHAINS_V2_PLAN.md Phase 3):
- D:\\Output\\Inbox\\video\\* -> Whisper transcribe + chapters + key points note in 1Notebook\\Transcripts\\
- D:\\Output\\Inbox\\pdf\\*   -> pypdf extraction + structured study outline in 1Notebook\\School\\

Deterministic checks:
- Video: non-empty transcript and monotonic timestamps (start times never decrease).
- PDF: non-empty extracted text (>= 50 chars).
- Inbox ledger (D:\\AI\\Cache\\llm\\inbox-ledger.json) tracks processed files by normalized path, size and mtime
  so source files are never deleted or moved, yet never re-ingested. Failed files are recorded too (MAX_ATTEMPTS,
  then skipped until the file changes). Notes go through file_tools.check_write; model calls take the GPU lock.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

import jsonschema

from . import file_tools, fix_tools, media_tools
from .runner import ToolContext, ToolError

LEDGER_FILE = Path(os.environ.get("SOL_INBOX_LEDGER", r"D:\AI\Cache\llm\inbox-ledger.json"))
VIDEO_INBOX = Path(os.environ.get("SOL_VIDEO_INBOX", r"D:\Output\Inbox\video"))
PDF_INBOX = Path(os.environ.get("SOL_PDF_INBOX", r"D:\Output\Inbox\pdf"))
TRANSCRIPTS_DIR = Path(os.environ.get("SOL_TRANSCRIPTS_NOTEBOOK", r"D:\OBVLT\1Notebook\Transcripts"))
SCHOOL_DIR = Path(os.environ.get("SOL_SCHOOL_NOTEBOOK", r"D:\OBVLT\1Notebook\School"))

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".mp3", ".wav", ".m4a", ".webm"}
PDF_EXTS = {".pdf"}

VIDEO_SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["overview", "chapters", "key_points"],
    "properties": {
        "overview": {"type": "string"},
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["at", "title"],
                "properties": {
                    "at": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["point"],
                "properties": {
                    "point": {"type": "string"},
                    "at": {"type": "string"},
                },
            },
        },
        "action_items": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

PDF_OUTLINE_SCHEMA = {
    "type": "object",
    "required": ["title", "summary", "outline"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "outline": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["section", "bullets"],
                "properties": {
                    "section": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "key_terms": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["term", "definition"],
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                },
            },
        },
        "study_questions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


# ------------------------------------------------------------------ ledger
MAX_ATTEMPTS = 2  # a file that fails this many times is given up on until the file itself changes


class Interrupted(Exception):
    """An abort (cancel, you came back, a game started) or the model being unavailable: stop, leave the files pending."""


class _NullCtx:
    """Stands in for a ToolContext when a function is called without one."""
    run_id = None

    def note(self, message: str, **data) -> None:
        pass

    def cancelled(self) -> bool:
        return False


def _abort_fn(ctx) -> Callable[[], bool]:
    """True when the run was cancelled or the idle/away lane closed (the daemon's abort flag, as the fix lane uses)."""
    def abort() -> bool:
        return bool((ctx is not None and ctx.cancelled()) or fix_tools.abort_requested())
    return abort


def read_ledger(path: Path = LEDGER_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_ledger(data: dict, path: Path = LEDGER_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def file_key(path: Path) -> str:
    return str(path.resolve()).lower()


def _stat_sig(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_size, int(st.st_mtime)
    except OSError:
        return 0, 0


def is_processed(path: Path, ledger: dict | None = None, ledger_path: Path = LEDGER_FILE) -> bool:
    ledger = ledger if ledger is not None else read_ledger(ledger_path)
    entry = ledger.get(file_key(path))
    if not entry or entry.get("status") != "done":
        return False
    try:
        st = path.stat()
        return entry.get("size") == st.st_size and entry.get("mtime") == int(st.st_mtime)
    except OSError:
        return False


def failed_attempts(path: Path, ledger: dict | None = None, ledger_path: Path = LEDGER_FILE) -> int:
    """How often this exact file (same size and mtime) has failed; 0 if never or if the file changed since."""
    ledger = ledger if ledger is not None else read_ledger(ledger_path)
    entry = ledger.get(file_key(path))
    if not entry or entry.get("status") != "failed":
        return 0
    try:
        st = path.stat()
    except OSError:
        return 0
    if entry.get("size") == st.st_size and entry.get("mtime") == int(st.st_mtime):
        return int(entry.get("attempts") or 0)
    return 0


def is_given_up(path: Path, ledger: dict | None = None, ledger_path: Path = LEDGER_FILE) -> bool:
    return failed_attempts(path, ledger, ledger_path) >= MAX_ATTEMPTS


def record_processed(path: Path, note_path: str, run_id: int | None = None,
                     ledger_path: Path = LEDGER_FILE) -> None:
    ledger = read_ledger(ledger_path)
    size, mtime = _stat_sig(path)
    ledger[file_key(path)] = {
        "path": str(path),
        "name": path.name,
        "size": size,
        "mtime": mtime,
        "processed_at": datetime.now().isoformat(),
        "note_path": note_path,
        "run_id": run_id,
        "status": "done",
    }
    write_ledger(ledger, ledger_path)


def record_failure(path: Path, reason: str, run_id: int | None = None, ledger_path: Path = LEDGER_FILE) -> int:
    """Count one failure of this file (reset if the file changed). Returns the attempt count."""
    ledger = read_ledger(ledger_path)
    attempts = failed_attempts(path, ledger) + 1
    size, mtime = _stat_sig(path)
    ledger[file_key(path)] = {
        "path": str(path),
        "name": path.name,
        "size": size,
        "mtime": mtime,
        "failed_at": datetime.now().isoformat(),
        "reason": str(reason)[:300],
        "attempts": attempts,
        "run_id": run_id,
        "status": "failed",
    }
    write_ledger(ledger, ledger_path)
    return attempts


# ------------------------------------------------------------------ tools
def _int_arg(value, default: int, lo: int = 1, hi: int = 100) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def inbox_pending(args: dict, ctx: ToolContext | None = None) -> dict:
    """Scan an inbox folder for files that still need ingesting (oldest first).

    A file is skipped when it is done (same size and mtime) or has failed MAX_ATTEMPTS times (same size and mtime);
    changing the file makes it pending again. Given-up files don't count against max_items.

    args:
      inbox: folder to scan (defaults to VIDEO_INBOX or PDF_INBOX based on kind)
      folder: old name of `inbox`, still accepted here for back-compat (it never meant the output folder)
      kind: 'video' | 'pdf' (default: 'video')
      max_items: int (default: 3; bad values fall back to 3, capped at 100)
      ledger_file: optional path override (for tests)
    returns: {folder, kind, count, pending, given_up, given_up_files}
    """
    kind = str(args.get("kind") or "video").lower()
    default_dir = PDF_INBOX if kind == "pdf" else VIDEO_INBOX
    folder = Path(args.get("inbox") or args.get("folder") or default_dir)
    folder.mkdir(parents=True, exist_ok=True)
    max_items = _int_arg(args.get("max_items"), 3)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)
    ledger = read_ledger(ledger_path)

    exts = PDF_EXTS if kind == "pdf" else VIDEO_EXTS
    candidates = []
    try:
        items = list(folder.iterdir())
    except OSError:
        items = []
    for item in items:
        try:  # a file can vanish between the listing and the stat
            if item.is_file() and item.suffix.lower() in exts:
                candidates.append((item.stat().st_mtime, item.name.lower(), item))
        except OSError:
            continue
    candidates.sort(key=lambda c: (c[0], c[1]))

    pending, given_up = [], []
    for _mtime, _name, item in candidates:
        if is_processed(item, ledger, ledger_path):
            continue
        if is_given_up(item, ledger, ledger_path):
            given_up.append(str(item))
        elif len(pending) < max_items:
            pending.append(str(item))
    return {"folder": str(folder), "kind": kind, "count": len(pending), "pending": pending,
            "given_up": len(given_up), "given_up_files": given_up}


def _verify_monotonic_timestamps(segments: list[dict]) -> None:
    """Deterministic check: transcript segment start times must never decrease."""
    last_start = -0.001
    for i, s in enumerate(segments):
        start = float(s.get("start") or 0.0)
        if start < last_start:
            raise ToolError(f"Segment timestamps non-monotonic at index {i}: start {start}s < previous {last_start}s")
        last_start = start


def _router_chat(model: str, messages: list[dict], schema: dict, abort: Callable[[], bool]) -> str:
    """The model call, the way the runner and the fix lane make it: take the GPU lock (waiting turns, and giving up if
    `abort()` goes true while waiting), and treat the router's own abort / 'model not served right now' as an
    interruption (leave the file pending), not as a failure of the file."""
    from .gpulock import LockTimeout, gpu_lock
    from .router import ModelUnavailable, RouterClient
    from .workflow import Cancelled
    if abort():
        raise Interrupted("aborted")
    try:
        with gpu_lock("inbox ingest", should_stop=abort):
            return RouterClient().chat(model, messages, schema=schema, max_tokens=3000).content
    except (LockTimeout, Cancelled, ModelUnavailable) as e:
        raise Interrupted(str(e) or type(e).__name__) from e


def _chat_with_model(model: str, system: str, user_prompt: str, schema: dict, propose=None,
                     abort: Callable[[], bool] | None = None) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]
    raw = propose(messages) if propose is not None else _router_chat(model, messages, schema, abort or (lambda: False))
    return raw if isinstance(raw, dict) else json.loads(raw)


def _write_note(out_dir: Path, title: str, content: str, now: datetime, policy: file_tools.Policy | None) -> Path:
    """Write the note under out_dir if the chain write policy allows it (tmp file, then os.replace). Never overwrites."""
    safe_title = file_tools.safe_name(title)
    note_file = out_dir / f"{now.strftime('%Y-%m-%d')} {safe_title}.md"
    n = 2
    while note_file.exists():
        note_file = out_dir / f"{now.strftime('%Y-%m-%d')} {safe_title} ({n}).md"
        n += 1
    target = file_tools.check_write(note_file, policy)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return target


def ingest_video(args: dict, ctx: ToolContext | None = None, transcribe_fn=None, propose=None,
                 policy: file_tools.Policy | None = None, abort: Callable[[], bool] | None = None) -> dict:
    """Transcribe a media file with deterministic timestamp checks, extract chapters and key points,
    and write an Obsidian note to 1Notebook/Transcripts/ (checked against the chain write policy).

    args: path (the file), notes_folder (output folder, default TRANSCRIPTS_DIR), model, ledger_file.
    Raises Interrupted if aborted (the ledger is not touched).
    """
    path = Path(args.get("path") or "")
    if not path.is_file():
        raise ToolError(f"file not found: {path}")

    abort = abort or _abort_fn(ctx)
    c = ctx if ctx is not None else _NullCtx()
    model = str(args.get("model") or "sol-fast")
    out_dir = Path(args.get("notes_folder") or TRANSCRIPTS_DIR)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)

    if abort():
        raise Interrupted("aborted before transcribing")
    c.note(f"transcribing {path.name}")
    tr = (transcribe_fn or media_tools.transcribe)({"source": str(path)}, c)
    if abort():
        raise Interrupted("aborted after transcribing")

    full_text = str(tr.get("full_text") or "").strip()
    if not full_text:
        raise ToolError(f"empty transcript returned for {path.name}")

    segments = tr.get("segments") or []
    if segments:
        _verify_monotonic_timestamps(segments)

    # Summarize with schema check
    system = ("You summarize media transcripts. Extract an overview paragraph, timestamped chapters, "
              "key points with timestamps, and optional action items. Return JSON conforming to the schema.")
    user_prompt = f"Title: {path.stem}\n\nTranscript:\n{tr.get('timestamped_text') or full_text[:12000]}"
    data = _chat_with_model(model, system, user_prompt, VIDEO_SUMMARY_SCHEMA, propose=propose, abort=abort)
    jsonschema.validate(instance=data, schema=VIDEO_SUMMARY_SCHEMA)

    # Format Markdown
    title = str(tr.get("title") or path.stem).strip()
    now = datetime.now()
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"date: {now.strftime('%Y-%m-%d %H:%M')}",
        f"source: {json.dumps(str(path), ensure_ascii=False)}",
        f"duration: {json.dumps(media_tools.timestamp(tr.get('duration') or 0))}",
        f"model: {json.dumps(model)}",
        "made_by: sol-chain",
        "tags:",
        "  - transcription",
        "  - inbox-ingest",
        "---",
        "",
        f"# {title}",
        "",
        "> [!abstract] Overview",
        "> " + data.get("overview", "").strip().replace("\n", "\n> "),
        "",
        "## Chapters",
    ]
    for ch in data.get("chapters", []):
        lines.append(f"- **`{ch.get('at', '00:00')}`** {ch.get('title', '').strip()}"
                     + (f": {ch['summary'].strip()}" if ch.get("summary") else ""))

    lines += ["", "## Key points"]
    for kp in data.get("key_points", []):
        lines.append(f"- {kp['point'].strip()}" + (f" `{kp['at']}`" if kp.get("at") else ""))

    if data.get("action_items"):
        lines += ["", "## Action items"]
        for act in data["action_items"]:
            lines.append(f"- [ ] {act.strip()}")

    lines += [
        "",
        "---",
        "",
        "## Timestamped transcript",
        "",
        tr.get("timestamped_text") or full_text,
        "",
    ]
    note_file = _write_note(out_dir, title, "\n".join(lines), now, policy)

    record_processed(path, str(note_file), run_id=c.run_id, ledger_path=ledger_path)
    c.note(f"wrote transcript note to {note_file.name}")
    return {"ok": True, "path": str(path), "note": str(note_file)}


def ingest_pdf(args: dict, ctx: ToolContext | None = None, read_fn=None, propose=None,
               policy: file_tools.Policy | None = None, abort: Callable[[], bool] | None = None) -> dict:
    """Extract text from an inbox PDF (with deterministic >= 50 chars check), build a study outline,
    and write an Obsidian note to 1Notebook/School/ (checked against the chain write policy).

    args: path (the file), notes_folder (output folder, default SCHOOL_DIR), model, ledger_file.
    Raises Interrupted if aborted (the ledger is not touched).
    """
    path = Path(args.get("path") or "")
    if not path.is_file():
        raise ToolError(f"file not found: {path}")

    abort = abort or _abort_fn(ctx)
    c = ctx if ctx is not None else _NullCtx()
    model = str(args.get("model") or "sol-fast")
    out_dir = Path(args.get("notes_folder") or SCHOOL_DIR)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)

    if abort():
        raise Interrupted("aborted before reading")
    c.note(f"reading PDF {path.name}")
    doc = read_fn(path) if read_fn is not None else file_tools.read_file(path)

    text = str(doc.get("text") or "").strip()
    if len(text) < 50:
        raise ToolError(f"PDF text too short or unreadable ({len(text)} chars)")

    system = ("You extract structured study outlines from school and technical documents. "
              "Produce an outline with sections, bullet points, key terms with definitions, "
              "and review study questions. Return JSON conforming to the schema.")
    user_prompt = f"Document: {path.stem}\n\nContent:\n{text[:14000]}"
    data = _chat_with_model(model, system, user_prompt, PDF_OUTLINE_SCHEMA, propose=propose, abort=abort)
    jsonschema.validate(instance=data, schema=PDF_OUTLINE_SCHEMA)

    title = str(data.get("title") or path.stem).strip()
    now = datetime.now()
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"date: {now.strftime('%Y-%m-%d %H:%M')}",
        f"source: {json.dumps(str(path), ensure_ascii=False)}",
        f"model: {json.dumps(model)}",
        "made_by: sol-chain",
        "tags:",
        "  - school",
        "  - pdf-outline",
        "  - study",
        "---",
        "",
        f"# {title}",
        "",
        "> [!abstract] Summary",
        "> " + data.get("summary", "").strip().replace("\n", "\n> "),
        "",
        "## Outline",
    ]
    for sec in data.get("outline", []):
        lines.append(f"### {sec.get('section', 'Section').strip()}")
        for b in sec.get("bullets", []):
            lines.append(f"- {b.strip()}")
        lines.append("")

    if data.get("key_terms"):
        lines += ["## Key terms", ""]
        for kt in data["key_terms"]:
            lines.append(f"- **{kt.get('term', '').strip()}**: {kt.get('definition', '').strip()}")
        lines.append("")

    if data.get("study_questions"):
        lines += ["## Study questions", ""]
        for q in data["study_questions"]:
            lines.append(f"- [ ] {q.strip()}")
        lines.append("")

    note_file = _write_note(out_dir, title, "\n".join(lines).strip() + "\n", now, policy)

    record_processed(path, str(note_file), run_id=c.run_id, ledger_path=ledger_path)
    c.note(f"wrote study outline to {note_file.name}")
    return {"ok": True, "path": str(path), "note": str(note_file)}


def _ingest_batch(kind: str, single: Callable, args: dict, ctx: ToolContext | None, extra: dict,
                  policy: file_tools.Policy | None, abort: Callable[[], bool] | None) -> dict:
    """Shared batch loop. Only the keys the single-file function needs are passed on, with `path` last, so nothing in
    `args` can redirect which file is read or where the note goes (the output folder is `notes_folder` only).
    Failures are recorded in the ledger (MAX_ATTEMPTS, then skipped until the file changes). An abort or an unavailable
    model stops the batch cleanly: the remaining files stay pending, nothing is recorded as failed, and ModelUnavailable
    is raised so the runner retries the step later (as the fix lane does)."""
    c = ctx if ctx is not None else _NullCtx()
    if abort is None:
        fix_tools.clear_abort()  # the daemon's lane-closed flag from an earlier session must not stop this one (as fix_attempts)
        abort = _abort_fn(ctx)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)
    res = inbox_pending({**args, "kind": kind}, ctx)
    common = {k: args[k] for k in ("model", "notes_folder", "ledger_file") if args.get(k)}
    processed, failed, interrupted = [], [], None
    for p in res["pending"]:
        if abort():
            interrupted = "aborted"
            break
        try:
            processed.append(single({**common, "path": p}, ctx, policy=policy, abort=abort, **extra))
        except Interrupted as e:
            interrupted = str(e) or "aborted"
            break
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest, but it must be remembered
            reason = f"{type(e).__name__}: {e}" if not isinstance(e, ToolError) else str(e)
            try:
                attempts = record_failure(Path(p), reason, run_id=c.run_id, ledger_path=ledger_path)
            except OSError:
                attempts = 0
            failed.append({"path": p, "reason": reason, "attempts": attempts})
            c.note(f"failed ingesting {p} (attempt {attempts}/{MAX_ATTEMPTS}): {reason}")
    if interrupted is not None:
        from .router import ModelUnavailable
        c.note(f"ingest interrupted ({interrupted}); {len(processed)} done, the rest stay pending")
        raise ModelUnavailable(f"ingest interrupted ({interrupted}); it runs again when the AI is free")
    given_up = res["given_up"] + sum(1 for f in failed if f["attempts"] >= MAX_ATTEMPTS)
    out = {"processed": processed, "count": len(processed), "failed": failed, "failed_count": len(failed),
           "given_up": given_up}
    if not processed and not failed:
        out[file_tools.IDLE_KEY] = True   # an empty inbox: the runner leaves the result note alone and nobody is told "finished"
    return out


def ingest_inbox_videos(args: dict, ctx: ToolContext | None = None, transcribe_fn=None, propose=None,
                        policy: file_tools.Policy | None = None, abort: Callable[[], bool] | None = None) -> dict:
    """Batch ingest pending inbox videos.

    args: inbox (source folder; old `folder` is accepted for the scan only), notes_folder (output), max_items, model,
    ledger_file. `path` is ignored. Returns {processed, count, failed, failed_count, given_up}; `given_up` is how many
    files are being skipped after MAX_ATTEMPTS failures.
    """
    return _ingest_batch("video", ingest_video, args, ctx, {"transcribe_fn": transcribe_fn, "propose": propose},
                         policy, abort)


def ingest_inbox_pdfs(args: dict, ctx: ToolContext | None = None, read_fn=None, propose=None,
                      policy: file_tools.Policy | None = None, abort: Callable[[], bool] | None = None) -> dict:
    """Batch ingest pending inbox PDFs. Same args and result as ingest_inbox_videos."""
    return _ingest_batch("pdf", ingest_pdf, args, ctx, {"read_fn": read_fn, "propose": propose}, policy, abort)


TOOLS = {
    "inbox_pending": inbox_pending,
    "ingest_video": ingest_video,
    "ingest_pdf": ingest_pdf,
    "ingest_inbox_videos": ingest_inbox_videos,
    "ingest_inbox_pdfs": ingest_inbox_pdfs,
}
