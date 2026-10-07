"""The dashboard as a control panel (phase C of plans/SOL_CONTROL_HUD_NEXT_PLAN.md).

"Ask it overnight": a question becomes a normal Away queue job (type chat, the format tools\\sol-queue.ps1 writes);
the answer lands in 1Notebook\\Answers. With "Run now" a Desk model answers it right away instead (ask_now), into the
same Answers format. Chain controls change a chain note's `status:` exactly like editing the note.
Every path comes from here, never from the page: the page names things (a job, an answer file, a chain), and each name
is checked against what exists.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .chains import chain_note, file_tools

QUEUE_DIR = Path(os.environ.get("SOL_QUEUE_DIR", r"D:\AI\Queue"))
ANSWERS_DIR = Path(os.environ.get("SOL_ANSWERS", r"D:\OBVLT\1Notebook\Answers"))
LOCAL_AI = Path(os.environ.get("SOL_LOCAL_AI", r"D:\OBVLT\configs\local-ai.json"))
ASK_PREFIX = "ask-"
ASK_MAX_TOKENS = 12000          # thinking models need room to think before they answer
ASK_CTX = 32768                 # every Away model runs with 32k (configs\local-ai.json)
CHARS_PER_TOKEN = file_tools.CHARS_PER_TOKEN
MAX_FILES = 12
CHAIN_OPS = {"run": "queued", "pause": chain_note.PAUSED, "resume": "done", "cancel": "cancel"}
# "Run now": the Desk models (served whenever the AI is on). Their contexts come from models.json at call time.
DESK_ASK_MODELS = {"sol-fast": "sol-fast (quick, 8k context)", "sol-smart": "sol-smart (reasoning, 32k)",
                   "sol-long": "sol-long (long documents, 65k)"}
ROUTER = os.environ.get("SOL_ROUTER", "http://127.0.0.1:11440")
_now_jobs: dict[str, dict] = {}          # questions being answered right now by this process: job -> state
_now_lock = threading.Lock()


class ControlError(ValueError):
    """Something the page asked for that isn't allowed or doesn't exist (shown as the answer's `why`)."""


# ---------------------------------------------------------------- ask it overnight
def away_models(path: Path | None = None) -> list[dict]:
    """sol-away (the Away model that fits best that night) + each Away model with its quality/speed notes."""
    out = [{"id": "sol-away", "label": "Best Away model (chosen when Away starts)"}]
    path = path or LOCAL_AI         # looked up at call time (a default argument would be fixed at import)
    try:
        away = (json.loads(path.read_text(encoding="utf-8-sig")).get("away") or {}).get("models", {})
    except (OSError, ValueError):
        return out
    for name, m in (away.items() if isinstance(away, dict) else []):
        if isinstance(m, dict) and m.get("file"):
            notes = ", ".join(x for x in (f"quality {m['quality']}" if m.get("quality") else "",
                                          f"{m['tokps']} tok/s" if m.get("tokps") else "") if x)
            out.append({"id": name, "label": f"{name} ({notes})" if notes else name})
    return out


def _slug(title: str) -> str:
    s = re.sub(r"[^\w .,()'-]+", "", title, flags=re.UNICODE).strip()[:60].strip(" .")
    return s or "question"


def _build_prompt(question: str, files: list[str] | None, ctx: int, max_tokens: int) -> tuple[str, str, list[str]]:
    """The question plus the files' text (read now, same rules as chains), checked against the model's room."""
    question = (question or "").strip()
    if not question:
        raise ControlError("write a question or task first")
    files = [f.strip().strip('"') for f in (files or []) if f and f.strip()]
    if len(files) > MAX_FILES:
        raise ControlError(f"at most {MAX_FILES} files")
    parts = [question]
    for f in files:
        try:
            item = file_tools.read_file(f)       # raises for anything outside the allowed folders or secret-looking
        except Exception as e:  # noqa: BLE001
            raise ControlError(f"can't use {f}: {e}") from e
        parts.append(f"\n\n---\nFile: {item.get('path', f)}\n```\n{item.get('text', '')}\n```")
    prompt = "".join(parts)
    room = (ctx - max_tokens) * CHARS_PER_TOKEN
    if len(prompt) > room:
        raise ControlError(f"too long for one question: ~{int(len(prompt) / CHARS_PER_TOKEN)} tokens, the limit is "
                           f"~{int(room / CHARS_PER_TOKEN)} (leave out a file, pick a model with more context, "
                           "or make a chain for it)")
    return question, prompt, files


def queue_ask(title: str, question: str, files: list[str] | None = None, model: str = "sol-away",
              queue_dir: Path | None = None, answers_dir: Path | None = None, now: datetime | None = None) -> dict:
    """Put a question in the Away queue. Files are read now (same rules as chains: allowed folders, no secrets)."""
    queue_dir, answers_dir, now = queue_dir or QUEUE_DIR, answers_dir or ANSWERS_DIR, now or datetime.now()
    if model not in {m["id"] for m in away_models()}:
        raise ControlError(f"unknown model {model!r}")
    question, prompt, files = _build_prompt(question, files, ASK_CTX, ASK_MAX_TOKENS)
    title = _slug(title or question.splitlines()[0])
    name, n = f"{ASK_PREFIX}{title}", 2
    queue_dir.mkdir(parents=True, exist_ok=True)
    while any((d / f"{name}.json").exists() for d in (queue_dir, queue_dir / "running")):
        name, n = f"{ASK_PREFIX}{title} ({n})", n + 1
    job = {"type": "chat", "model": model, "prompt": prompt, "maxTokens": ASK_MAX_TOKENS,
           "outFile": str(answers_dir / f"{now:%Y-%m-%d} {name[len(ASK_PREFIX):]}.md"),
           "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
           "ask": {"title": name[len(ASK_PREFIX):], "question": question[:4000], "files": files}}
    (queue_dir / f"{name}.json").write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"job": name, "answer": job["outFile"]}


def desk_models() -> list[dict]:
    return [{"id": m, "label": label} for m, label in DESK_ASK_MODELS.items()]


def _answer_md(title: str, question: str, files: list[str], model: str, answer: str, asked: str) -> str:
    """The note tools\\sol-queue.ps1 writes for an overnight ask: the question on top, so it reads on its own."""
    quoted = "\n".join(f"> {line}" for line in question.splitlines())
    files_line = ("\n\nFiles: " + ", ".join(f"`{f}`" for f in files)) if files else ""
    return (f"---\nsource: SOL Control HUD (ask now)\nmodel: {model}\ncreated: {datetime.now():%Y-%m-%dT%H:%M:%S}\n"
            f"asked: {asked}\n---\n\n# {title}\n\n{quoted}{files_line}\n\n---\n\n{answer}\n")


def ask_now(title: str, question: str, files: list[str] | None = None, model: str = "sol-smart",
            answers_dir: Path | None = None, now: datetime | None = None, client=None, mode: str | None = None,
            lock=None, background: bool = True) -> dict:
    """Answer a question right away with a Desk model, into 1Notebook\\Answers (same note as overnight). Runs in the
    background and takes its turn on the GPU like a chain (gpu.lock). Refused while the AI is off or Away has it."""
    from .chains.gpulock import gpu_lock
    from .chains.llm import load_registry
    from .chains.router import RouterClient, engine_mode
    answers_dir, now = answers_dir or ANSWERS_DIR, now or datetime.now()
    if model not in DESK_ASK_MODELS:
        raise ControlError(f"{model!r} isn't a Desk model (pick sol-fast, sol-smart or sol-long)")
    mode = mode or engine_mode()
    if mode == "off":
        raise ControlError("the local AI is off right now (game guard?): queue it for tonight instead")
    if mode == "away":
        raise ControlError("Away mode has the GPU right now: queue it for tonight instead")
    ctx = int((load_registry().get(model) or {}).get("num_ctx") or 8192)
    max_tokens = min(ASK_MAX_TOKENS, max(2048, ctx // 2))      # room to think, then answer
    question, prompt, files = _build_prompt(question, files, ctx, max_tokens)
    title = _slug(title or question.splitlines()[0])
    with _now_lock:
        taken = {j["out"] for j in _now_jobs.values()}
        out, n = answers_dir / f"{now:%Y-%m-%d} {title}.md", 2
        while out.exists() or str(out) in taken:
            out, n = answers_dir / f"{now:%Y-%m-%d} {title} ({n}).md", n + 1
        job, shown = f"now-{out.stem}", out.stem.split(" ", 1)[1]
        _now_jobs[job] = {"title": shown, "model": model, "state": "running now", "out": str(out),
                          "created": now.strftime("%Y-%m-%dT%H:%M:%S"), "question": question[:300]}

    def work():
        try:
            with (lock or gpu_lock)(f"ask now: {shown}", timeout=1800):
                r = (client or RouterClient(timeout=3600)).chat(model, [{"role": "user", "content": prompt}],
                                                                max_tokens=max_tokens)
            if not (r.content or "").strip():
                raise ControlError(f"empty answer (finish_reason {r.finish_reason})")
            answers_dir.mkdir(parents=True, exist_ok=True)
            out.write_text(_answer_md(shown, question, files, r.model or model, r.content.strip(),
                                      _now_jobs[job]["created"]), encoding="utf-8")
            with _now_lock:
                _now_jobs.pop(job, None)
        except Exception as e:  # noqa: BLE001 - stays in the Waiting list (with ✕ to clear) until the HUD restarts
            with _now_lock:
                if job in _now_jobs:
                    _now_jobs[job]["state"] = f"failed: {str(e)[:200] or type(e).__name__}"
    if background:
        threading.Thread(target=work, name=f"ask now: {shown}", daemon=True).start()
    else:
        work()
    return {"job": job, "answer": str(out)}


def list_asks(queue_dir: Path | None = None) -> list[dict]:
    queue_dir = queue_dir or QUEUE_DIR
    out = []
    for state, d in (("waiting", queue_dir), ("running", queue_dir / "running")):
        for f in sorted(d.glob(f"{ASK_PREFIX}*.json")) if d.is_dir() else []:
            try:
                job = json.loads(f.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            ask = job.get("ask") or {}
            out.append({"job": f.stem, "title": ask.get("title") or f.stem[len(ASK_PREFIX):], "state": state,
                        "model": job.get("model"), "created": job.get("created"), "question": ask.get("question", "")[:300]})
    with _now_lock:
        out += [{"job": k, "title": j["title"], "state": j["state"], "model": j["model"], "created": j["created"],
                 "question": j["question"], "now": True} for k, j in _now_jobs.items()]
    return sorted(out, key=lambda a: (not a["state"].startswith("running"), a["created"] or "", a["job"]))  # running first


def remove_ask(job: str, queue_dir: Path | None = None) -> None:
    """Take a waiting question out of the queue (moved to Queue\\removed, not deleted). A running one can't be removed:
    stop the Away work instead."""
    queue_dir = queue_dir or QUEUE_DIR
    if job.startswith("now-"):
        with _now_lock:
            j = _now_jobs.get(job)
            if j and j["state"].startswith("failed"):
                del _now_jobs[job]                      # clear a failed "ask now" from the list
                return
        raise ControlError("it's being answered now" if j else "no such question")
    if not job.startswith(ASK_PREFIX) or any(c in job for c in "\\/:") or job.startswith("."):
        raise ControlError("not a question job")
    src = queue_dir / f"{job}.json"
    if not src.is_file():
        running = (queue_dir / "running" / f"{job}.json").is_file()
        raise ControlError("it's running now (Stop AI work puts it back in the queue)" if running else "no such question")
    (queue_dir / "removed").mkdir(exist_ok=True)
    shutil.move(str(src), str(queue_dir / "removed" / f"{job}.json"))


def list_answers(answers_dir: Path | None = None, days: int = 7) -> list[dict]:
    answers_dir = answers_dir or ANSWERS_DIR
    if not answers_dir.is_dir():
        return []
    cutoff, out = time.time() - days * 86400, []
    for f in sorted(answers_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        if st.st_mtime < cutoff:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        body = chain_note._FRONT.sub("", text, count=1).strip()
        model = re.search(r"(?m)^model:\s*(.+)$", text)
        out.append({"file": f.name, "title": f.stem, "time": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M"),
                    "model": model.group(1).strip() if model else "", "preview": re.sub(r"\s+", " ", body)[:220]})
    return out


def read_answer(file: str, answers_dir: Path | None = None) -> str:
    answers_dir = answers_dir or ANSWERS_DIR
    if file not in {a["file"] for a in list_answers(answers_dir, days=3650)}:
        raise ControlError("no such answer")
    return (answers_dir / file).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- chain controls
def _chain_paths(chains_dir: Path | None = None) -> dict[str, Path]:
    d = chains_dir or chain_note.CHAINS_DIR
    out = {}
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        try:
            meta = _front(p)
        except Exception:  # noqa: BLE001 - an unreadable note is just not listed
            continue
        if meta.get("type") in (None, "chain"):
            out[p.stem] = p
    return out


def _front(p: Path) -> dict:
    import yaml
    m = chain_note._FRONT.match(p.read_text(encoding="utf-8-sig"))
    data = yaml.safe_load(m.group(1)) if m else {}
    return data if isinstance(data, dict) else {}


def list_chains(chains_dir: Path | None = None) -> list[dict]:
    out = []
    for name, p in _chain_paths(chains_dir).items():
        meta = _front(p)
        result = p.parent / chain_note.RESULTS_DIR_NAME / f"{name} (result).md"
        out.append({"name": name, "status": str(meta.get("status") or "draft").lower(), "model": meta.get("model") or "",
                    "schedule": str(meta.get("schedule") or ""), "watch": bool(meta.get("watch")),
                    "result_time": datetime.fromtimestamp(result.stat().st_mtime).strftime("%Y-%m-%dT%H:%M")
                    if result.is_file() else None})
    return out


def chain_result(name: str, chains_dir: Path | None = None) -> str:
    paths = _chain_paths(chains_dir)
    if name not in paths:
        raise ControlError("no such chain")
    result = paths[name].parent / chain_note.RESULTS_DIR_NAME / f"{name} (result).md"
    if not result.is_file():
        raise ControlError("no result yet")
    return result.read_text(encoding="utf-8", errors="replace")


def set_schedule(name: str, schedule: str, chains_dir: Path | None = None) -> str:
    """Change only `schedule:` in a chain note (empty = no schedule). Checked with the chain runner's own parser and
    written in its canonical form (`daily 07:00`, `weekly Mon,Thu 09:30`, `on away`). Returns what it says now."""
    from .chains.chain_daemon import DAYS, ScheduleError, parse_schedule
    paths = _chain_paths(chains_dir)
    if name not in paths:
        raise ControlError("no such chain")
    text = " ".join(str(schedule or "").split())
    if not text:
        chain_note.set_note_props(paths[name], {}, remove=("schedule",))
        return ""
    try:
        sched = parse_schedule(text)
    except ScheduleError as e:
        raise ControlError(str(e)) from e
    if sched["kind"] == "away":
        canon = "on away"
    else:
        if not (0 <= sched["h"] <= 23 and 0 <= sched["m"] <= 59):
            raise ControlError(f"{sched['h']:02d}:{sched['m']:02d} isn't a time of day")
        when = f"{sched['h']:02d}:{sched['m']:02d}"
        names = {i: d.capitalize() for d, i in DAYS.items()}
        canon = f"daily {when}" if len(sched["days"]) == 7 else f"weekly {','.join(names[d] for d in sched['days'])} {when}"
    chain_note.set_note_props(paths[name], {"schedule": canon})
    return canon


def chain_op(name: str, op: str, chains_dir: Path | None = None) -> str:
    """run -> queued, pause -> paused (no schedule, no watch), resume -> done (schedules on again), cancel -> the runner
    stops after the current step. Exactly what changing `status:` in the note does."""
    paths = _chain_paths(chains_dir)
    if name not in paths:
        raise ControlError("no such chain")
    if op == "now":
        # "Run now" on a waiting chain: one run that skips the polite waits (chain_daemon.skip_wait). A running one keeps
        # its status; anything else is queued as well.
        meta = _front(paths[name])
        if str(meta.get("status") or "").lower() == "running":
            chain_note.set_note_props(paths[name], {"skip_wait": "true"})
            return "running"
        chain_note.set_chain_status(paths[name], "queued", skip_wait="true")
        return "queued"
    if op not in CHAIN_OPS:
        raise ControlError(f"unknown action {op!r}")
    chain_note.set_chain_status(paths[name], CHAIN_OPS[op])
    return CHAIN_OPS[op]
