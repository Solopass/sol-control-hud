"""Watch-folder ingestion pipelines (LOCAL_AI_CHAINS_V2_PLAN.md Phase 3):
- D:\\Output\\Inbox\\video\\* -> Whisper transcribe + chapters + key points note in 1Notebook\\Transcripts\\
- D:\\Output\\Inbox\\pdf\\*   -> pypdf extraction + structured study outline in 1Notebook\\School\\

Deterministic checks:
- Video: non-empty transcript and monotonic timestamps (start times never decrease).
- PDF: non-empty extracted text (>= 50 chars).
- Inbox ledger (D:\\AI\\Cache\\llm\\inbox-ledger.json) tracks processed files by normalized path, size and mtime
  so source files are never deleted or moved, yet never re-ingested.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import jsonschema

from . import file_tools
from . import media_tools
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


def is_processed(path: Path, ledger: dict | None = None, ledger_path: Path = LEDGER_FILE) -> bool:
    ledger = ledger if ledger is not None else read_ledger(ledger_path)
    key = file_key(path)
    entry = ledger.get(key)
    if not entry or entry.get("status") != "done":
        return False
    try:
        st = path.stat()
        return entry.get("size") == st.st_size and entry.get("mtime") == int(st.st_mtime)
    except OSError:
        return False


def record_processed(path: Path, note_path: str, run_id: int | None = None,
                     ledger_path: Path = LEDGER_FILE) -> None:
    ledger = read_ledger(ledger_path)
    key = file_key(path)
    try:
        st = path.stat()
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        size, mtime = 0, 0
    ledger[key] = {
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


# ------------------------------------------------------------------ tools
def inbox_pending(args: dict, ctx: ToolContext | None = None) -> dict:
    """Scan an inbox folder for unprocessed files.

    args:
      folder: path to scan (defaults to VIDEO_INBOX or PDF_INBOX based on kind)
      kind: 'video' | 'pdf' (default: 'video')
      max_items: int (default: 3)
      ledger_file: optional path override (for tests)
    """
    kind = str(args.get("kind") or "video").lower()
    default_dir = PDF_INBOX if kind == "pdf" else VIDEO_INBOX
    folder = Path(args.get("folder") or default_dir)
    folder.mkdir(parents=True, exist_ok=True)
    max_items = int(args.get("max_items") or 3)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)
    ledger = read_ledger(ledger_path)

    exts = PDF_EXTS if kind == "pdf" else VIDEO_EXTS
    pending = []
    if folder.exists():
        for item in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if item.is_file() and item.suffix.lower() in exts and not is_processed(item, ledger, ledger_path):
                pending.append(str(item))
                if len(pending) >= max_items:
                    break
    return {"folder": str(folder), "kind": kind, "count": len(pending), "pending": pending}


def _verify_monotonic_timestamps(segments: list[dict]) -> None:
    """Deterministic check: transcript segment start times must never decrease."""
    last_start = -0.001
    for i, s in enumerate(segments):
        start = float(s.get("start") or 0.0)
        if start < last_start:
            raise ToolError(f"Segment timestamps non-monotonic at index {i}: start {start}s < previous {last_start}s")
        last_start = start


def _chat_with_model(model: str, system: str, user_prompt: str, schema: dict, propose=None) -> dict:
    if propose is not None:
        raw = propose([{"role": "system", "content": system}, {"role": "user", "content": user_prompt}])
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)
    from .router import RouterClient
    client = RouterClient()
    content = client.chat(model, [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
                          schema=schema, max_tokens=3000).content
    return json.loads(content)


def ingest_video(args: dict, ctx: ToolContext | None = None, transcribe_fn=None, propose=None) -> dict:
    """Transcribe a media file with deterministic timestamp checks, extract chapters and key points,

    and write an Obsidian note to 1Notebook/Transcripts/.
    """
    path = Path(args.get("path") or "")
    if not path.is_file():
        raise ToolError(f"file not found: {path}")

    model = str(args.get("model") or "sol-fast")
    out_dir = Path(args.get("folder") or TRANSCRIPTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)

    if ctx is not None:
        ctx.note(f"transcribing {path.name}")

    if transcribe_fn is not None:
        tr = transcribe_fn({"source": str(path)}, ctx or ToolContext(0, 0, lambda: False, lambda m: None))
    else:
        tr = media_tools.transcribe({"source": str(path)}, ctx or ToolContext(0, 0, lambda: False, lambda m: None))

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
    data = _chat_with_model(model, system, user_prompt, VIDEO_SUMMARY_SCHEMA, propose=propose)
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
    note_content = "\n".join(lines)
    safe_title = file_tools.safe_name(title)
    note_file = out_dir / f"{now.strftime('%Y-%m-%d')} {safe_title}.md"
    n = 2
    while note_file.exists():
        note_file = out_dir / f"{now.strftime('%Y-%m-%d')} {safe_title} ({n}).md"
        n += 1
    note_file.write_text(note_content, encoding="utf-8")

    run_id = ctx.run_id if ctx is not None else None
    record_processed(path, str(note_file), run_id=run_id, ledger_path=ledger_path)
    if ctx is not None:
        ctx.note(f"wrote transcript note to {note_file.name}")
    return {"ok": True, "path": str(path), "note": str(note_file)}


def ingest_pdf(args: dict, ctx: ToolContext | None = None, read_fn=None, propose=None) -> dict:
    """Extract text from an inbox PDF (with deterministic >= 50 chars check), build a study outline,

    and write an Obsidian note to 1Notebook/School/.
    """
    path = Path(args.get("path") or "")
    if not path.is_file():
        raise ToolError(f"file not found: {path}")

    model = str(args.get("model") or "sol-fast")
    out_dir = Path(args.get("folder") or SCHOOL_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = Path(args.get("ledger_file") or LEDGER_FILE)

    if ctx is not None:
        ctx.note(f"reading PDF {path.name}")

    if read_fn is not None:
        doc = read_fn(path)
    else:
        doc = file_tools.read_file(path)

    text = str(doc.get("text") or "").strip()
    if len(text) < 50:
        raise ToolError(f"PDF text too short or unreadable ({len(text)} chars)")

    system = ("You extract structured study outlines from school and technical documents. "
              "Produce an outline with sections, bullet points, key terms with definitions, "
              "and review study questions. Return JSON conforming to the schema.")
    user_prompt = f"Document: {path.stem}\n\nContent:\n{text[:14000]}"
    data = _chat_with_model(model, system, user_prompt, PDF_OUTLINE_SCHEMA, propose=propose)
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

    note_content = "\n".join(lines).strip() + "\n"
    safe_title = file_tools.safe_name(title)
    note_file = out_dir / f"{now.strftime('%Y-%m-%d')} {safe_title}.md"
    n = 2
    while note_file.exists():
        note_file = out_dir / f"{now.strftime('%Y-%m-%d')} {safe_title} ({n}).md"
        n += 1
    note_file.write_text(note_content, encoding="utf-8")

    run_id = ctx.run_id if ctx is not None else None
    record_processed(path, str(note_file), run_id=run_id, ledger_path=ledger_path)
    if ctx is not None:
        ctx.note(f"wrote study outline to {note_file.name}")
    return {"ok": True, "path": str(path), "note": str(note_file)}


def ingest_inbox_videos(args: dict, ctx: ToolContext | None = None) -> dict:
    """Batch ingest pending inbox videos."""
    res = inbox_pending({**args, "kind": "video"}, ctx)
    processed = []
    for p in res.get("pending", []):
        try:
            r = ingest_video({"path": p, **args}, ctx)
            processed.append(r)
        except Exception as e:  # noqa: BLE001
            if ctx is not None:
                ctx.note(f"failed ingesting {p}: {e}")
    return {"processed": processed, "count": len(processed)}


def ingest_inbox_pdfs(args: dict, ctx: ToolContext | None = None) -> dict:
    """Batch ingest pending inbox PDFs."""
    res = inbox_pending({**args, "kind": "pdf"}, ctx)
    processed = []
    for p in res.get("pending", []):
        try:
            r = ingest_pdf({"path": p, **args}, ctx)
            processed.append(r)
        except Exception as e:  # noqa: BLE001
            if ctx is not None:
                ctx.note(f"failed ingesting {p}: {e}")
    return {"processed": processed, "count": len(processed)}


TOOLS = {
    "inbox_pending": inbox_pending,
    "ingest_video": ingest_video,
    "ingest_pdf": ingest_pdf,
    "ingest_inbox_videos": ingest_inbox_videos,
    "ingest_inbox_pdfs": ingest_inbox_pdfs,
}
