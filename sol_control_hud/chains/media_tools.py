"""Tool steps for media workflows: transcribe through media-api (WSL) and write an Obsidian note.

media-api runs in WSL behind systemd socket activation on 127.0.0.1:8080. WSL has no keepalive (SETUP_PLAN D6),
so `transcribe` starts WSL if needed and holds it awake with a `sleep` process only while the job runs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path, PureWindowsPath

import httpx

from .llm import load_registry
from .runner import ToolContext, ToolError

MEDIA_API = os.environ.get("SOL_MEDIA_API", "http://127.0.0.1:8080").rstrip("/")
WSL_DISTRO = os.environ.get("SOL_WSL_DISTRO", "Ubuntu-24.04")
VAULT = Path(os.environ.get("SOL_VAULT", r"D:\AI\Vault"))
POLL_SECONDS = 2.0
STARTUP_SECONDS = 90.0
JOB_TIMEOUT_SECONDS = 4 * 3600.0


def wsl_to_windows(path: str) -> str:
    """/mnt/d/Output/x.wav -> D:\\Output\\x.wav (media-api reports WSL paths)."""
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
    return str(PureWindowsPath(f"{m.group(1).upper()}:/{m.group(2)}")) if m else path


def timestamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def timestamped_text(segments: list[dict]) -> str:
    return "\n".join(f"[{timestamp(s['start'])}] {s['text']}" for s in segments)


class _WslAwake:
    """Keeps the WSL VM running (so media-api stays reachable) for the lifetime of the `with` block."""

    def __enter__(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(["wsl.exe", "-d", WSL_DISTRO, "--", "sleep", "infinity"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=flags)
        return self

    def __exit__(self, *exc):
        self.proc.kill()
        self.proc.wait(timeout=10)


def _wait_for_api(ctx: ToolContext, http: httpx.Client) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    last = None
    while time.monotonic() < deadline:
        try:
            http.get(f"{MEDIA_API}/api/v1/health", timeout=10).raise_for_status()
            return
        except httpx.HTTPError as e:
            last = e
        if ctx.cancelled():
            return
        time.sleep(1)
    raise ToolError(f"media-api did not answer at {MEDIA_API} within {STARTUP_SECONDS:.0f}s ({last})")


def transcribe(args: dict, ctx: ToolContext) -> dict:
    """args: source (URL or Windows/WSL path), title, engine, language (all optional except source)."""
    source = str(args.get("source") or "").strip()
    if not source:
        raise ToolError("no source given")
    is_url = source.startswith(("http://", "https://"))
    if not is_url and re.match(r"^[a-zA-Z]:[\\/]", source) and not Path(source).exists():
        raise ToolError(f"file not found: {source}")

    body = {"source": source}
    for key in ("engine", "language"):
        if args.get(key):
            body[key] = args[key]

    with _WslAwake(), httpx.Client() as http:
        _wait_for_api(ctx, http)
        if ctx.cancelled():
            return {}
        r = http.post(f"{MEDIA_API}/api/v1/transcribe", json=body, timeout=30)
        if r.status_code != 200:
            raise ToolError(f"media-api refused the job: HTTP {r.status_code} {r.text[:300]}")
        job_id = r.json()["job_id"]
        ctx.note("media-api job queued", job_id=job_id, message_from_api=r.json().get("message"))

        started, last_stage = time.monotonic(), None
        while True:
            if ctx.cancelled():
                # media-api has no per-job cancel; the job finishes on its own and its files stay in D:\Output
                return {}
            if time.monotonic() - started > JOB_TIMEOUT_SECONDS:
                raise ToolError(f"media-api job {job_id} still not done after {JOB_TIMEOUT_SECONDS / 3600:.0f}h")
            try:
                job = http.get(f"{MEDIA_API}/api/v1/status/{job_id}", timeout=30).json()
            except (httpx.HTTPError, ValueError) as e:
                raise ToolError(f"lost contact with media-api while job {job_id} was running: {e}") from e
            if job.get("speed") != last_stage and job.get("status") == "running":
                last_stage = job.get("speed")
                ctx.note(last_stage or "running", percent=job.get("progress_percent"))
            if job["status"] == "completed":
                break
            if job["status"] in ("failed", "cancelled"):
                raise ToolError(f"media-api job {job_id} {job['status']}: {job.get('error')}")
            time.sleep(POLL_SECONDS)

    files = job.get("output_files") or {}
    if "transcript_json" not in files:
        raise ToolError("media-api did not return transcript_json (is media-api older than the step-10 change?)")
    data = json.loads(Path(wsl_to_windows(files["transcript_json"])).read_text(encoding="utf-8"))
    if not data.get("full_text", "").strip():
        raise ToolError("transcript is empty (no speech found?)")
    segments = data.get("segments") or []
    return {
        "source": source,
        "title": str(args.get("title") or "").strip() or (data.get("media") or {}).get("title") or "Transcript",
        "uploader": (data.get("media") or {}).get("uploader"),
        "url": (data.get("media") or {}).get("url"),
        "engine": data.get("engine"),
        "stt_model": data.get("model"),
        "language": data.get("language"),
        "duration": data.get("duration") or 0,
        "segments": segments,
        "full_text": data["full_text"],
        "timestamped_text": timestamped_text(segments) if segments else data["full_text"],
        "files": {k: wsl_to_windows(v) for k, v in files.items()},
    }


def _yaml_str(value) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/*?:"<>|#^\[\]]', "", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80] or "Transcript"


def render_note(title: str, transcription: dict, summary: dict, summary_model: str, run_id: int, now: datetime) -> str:
    lines = [
        "---",
        f"title: {_yaml_str(title)}",
        f"date: {now.strftime('%Y-%m-%d %H:%M')}",
        f"source: {_yaml_str(transcription.get('url') or transcription.get('source'))}",
        f"duration: {_yaml_str(timestamp(transcription.get('duration') or 0))}",
        f"engine: {_yaml_str(transcription.get('stt_model') or transcription.get('engine'))}",
        f"summary_model: {_yaml_str(summary_model)}",
        f"language: {_yaml_str(transcription.get('language') or 'auto')}",
        f"sol_run: {run_id}",
        "tags:",
        "  - transcription",
        "  - sol-pipeline",
        "---",
        "",
        f"# {title}",
        "",
        "> [!abstract] Overview",
        "> " + summary["overview"].strip().replace("\n", "\n> "),
        "",
        "## Key points",
    ]
    lines += [f"- {p['point'].strip()} `{p['at']}`" if p.get("at") else f"- {p['point'].strip()}" for p in summary["key_points"]]
    if summary.get("action_items"):
        lines += ["", "## Action items"]
        for a in summary["action_items"]:
            extra = [x for x in (f"owner: {a['owner']}" if a.get("owner") and a["owner"].lower() != "unknown" else "",
                                 f"due: {a['due']}" if a.get("due") and a["due"].lower() not in ("none", "unknown") else "") if x]
            lines.append(f"- [ ] {a['task'].strip()}" + (f" ({', '.join(extra)})" if extra else ""))
    if summary.get("decisions"):
        lines += ["", "## Decisions"] + [f"- {d.strip()}" for d in summary["decisions"]]
    if summary.get("dates"):
        lines += ["", "## Dates"] + [f"- **{d['date'].strip()}**: {d['what'].strip()}" for d in summary["dates"]]
    if summary.get("people"):
        lines += ["", "## People", ", ".join(p.strip() for p in summary["people"])]
    lines += [
        "",
        "---",
        "",
        "## Source",
        f"- **Media:** `{transcription.get('source')}`",
    ]
    if transcription.get("uploader"):
        lines.append(f"- **Uploader:** {transcription['uploader']}")
    lines += [
        f"- **Duration:** {timestamp(transcription.get('duration') or 0)}",
        f"- **Processed by:** SOL pipeline run {run_id} ({transcription.get('stt_model')} → {summary_model})",
        "- Summary written by a local model from the transcript below; check names and numbers against it.",
        "",
        "## Timestamped transcript",
        "",
        transcription.get("timestamped_text") or transcription.get("full_text", ""),
        "",
    ]
    return "\n".join(lines)


def write_note(args: dict, ctx: ToolContext) -> dict:
    """args: transcription (transcribe output), summary (summary step output), summary_model, folder (optional)."""
    transcription, summary = args.get("transcription") or {}, args.get("summary") or {}
    if not isinstance(summary, dict) or "overview" not in summary:
        raise ToolError("summary is missing or has no overview")
    title = transcription.get("title") or "Transcript"
    alias = str(args.get("summary_model") or "")
    base = load_registry().get(alias, {}).get("base")
    summary_model = f"{alias} ({base})" if base else alias

    now = datetime.now()
    folder = Path(args.get("folder") or VAULT / "Transcripts")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%Y-%m-%d')} {safe_filename(title)}"
    path, n = folder / f"{stem}.md", 2
    while path.exists():  # never overwrite an existing note
        path, n = folder / f"{stem} ({n}).md", n + 1
    path.write_text(render_note(title, transcription, summary, summary_model, ctx.run_id, now), encoding="utf-8")
    ctx.note("note written", path=str(path))
    link = f"[[{folder.name}/{path.stem}]]" if folder.parent == VAULT else str(path)
    return {"path": str(path), "link": link}


TOOLS = {"transcribe": transcribe, "write_note": write_note}
