"""Exam assist helper: watches one box of the screen in real time while you take an exam or quiz,
solves each question with the local AI (sol-vision), explains the reasoning, and saves all questions to notes.

- Set the box from the dashboard's Exam assist card ("Set box & start"); it starts on whatever the box shows now.
- Every 3 s it checks the box. When the screen changes (you go to the next question) it waits a moment for the page
  to settle and captures the box.
- Reads whatever is in the box and immediately solves it in real time: determines the answer, topic, and step-by-step
  reasoning. No waiting for grading, no limits or restrictions.
- Shows the answer instantly on the dashboard and taskbar ticker, and appends every question and solution to today's
  note in 1Notebook\\School\\<date> practice exam.md.
- Stops by itself after 10 quiet minutes.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .paths import DATA_DIR

SETTINGS_FILE = DATA_DIR / "exam-review.json"
NOTES_DIR = Path(r"D:\OBVLT\1Notebook\School")
MODEL = "sol-vision"
INTERVAL_S = 3.0         # how often it looks at the box (while not busy)
SETTLE_S = 1.0           # after a change: wait this long and look again, until the box stops changing
SETTLE_TRIES = 3
CHANGE = 0.015           # share of the box that must change to count as a new question: a new question on a mostly
                         # white page measured 3.5-4.5 % (160-column copy), a still page 0
STILL = 0.004            # under this between two looks = the page has settled
IDLE_STOP_S = 600.0      # stop after this long without a new question
HISTORY = 60

READ_SYSTEM = "You transcribe quiz and practice exam questions exactly as shown on the screen."
READ_PROMPT = """This screenshot shows a multiple-choice, drag-and-drop matching, fill-in-the-blank, or step ordering quiz or practice exam question.

Transcribe the question:
- question_type: "multiple_choice", "matching" (drag-and-drop), "fill_in_the_blank" (CLI command or text/numeric value), or "ordering" (sequencing steps).
- question: the question prompt or instructions, verbatim.
- exhibit_text: transcript of any network diagram labels, routing table, CLI output, or exhibit table visible in the question. Empty string if none.

For multiple_choice:
- choices: every answer choice as [{"label": "A", "text": "..."}]. Use the letter or number shown (A, B, C... or 1, 2, 3...); if none, label A, B, C... from top to bottom.
- select_count: number of options to select (e.g. 1, 2, 3).
- your_labels: labels of choice(s) selected by the student if any.
- correct_labels: labels of choice(s) marked correct on the screen if any.

For matching:
- matching_items: {"sources": ["Draggable item 1", ...], "targets": ["Drop target description 1", ...]}. Leave choices empty.

For fill_in_the_blank:
- blank_answers: any answers already typed into the blank(s). Leave choices empty.

For ordering:
- ordered_items: list of items/steps to be arranged in order. Leave choices empty.

Use status "ok" if complete. Use status "no_question" if no question is visible, and "cut_off" if text or choices are cut off at the edge. Leave out navigation, buttons, timers and other page text."""

SOLVE_SYSTEM = (
    "You are an expert tutor. You solve practice exam questions across multiple formats: "
    "multiple-choice, drag-and-drop matching, fill-in-the-blank CLI commands, and step sequencing. "
    "You identify the correct answer and provide concise step-by-step reasoning."
)

SOLVE_PROMPT_MC = """Solve this multiple-choice practice exam question.

Question: {question}
{exhibit}
Choices:
{choices}

Reply with:
- question_type: "multiple_choice"
- answer_labels: array of labels for the correct choice(s) (e.g. ["C"] or ["A", "D"]).
- topic: the concept this tests, in 2-5 words (for example "Subnetting /26 hosts", "OSI layer 2 devices").
- explanation: 2-4 short sentences explaining why this answer is correct and showing the key calculation, rule, or concept. Plain text only: no LaTeX, no $ signs, no Markdown (write 2^6 - 2 = 62)."""

SOLVE_PROMPT_MATCHING = """Solve this drag-and-drop matching practice exam question.

Question: {question}
{exhibit}
Draggable items:
{sources}

Drop targets:
{targets}

Reply with:
- question_type: "matching"
- matching_pairs: array of objects with "source" (draggable item) and "target" (matching slot/description).
- answer_text: concise summary of the matches (e.g. "5 pairs matched").
- topic: the concept this tests, in 2-5 words.
- explanation: 2-4 short sentences explaining the pairings and underlying rules."""

SOLVE_PROMPT_BLANK = """Solve this fill-in-the-blank / CLI command practice exam question.

Question: {question}
{exhibit}

Reply with:
- question_type: "fill_in_the_blank"
- blank_answers: array of strings containing the exact command syntax or value to type into the prompt/blank.
- answer_text: the primary command or value string.
- topic: the concept this tests, in 2-5 words.
- explanation: 2-4 short sentences explaining the command syntax or value."""

SOLVE_PROMPT_ORDERING = """Solve this step-ordering / sequencing practice exam question.

Question: {question}
{exhibit}
Items to arrange:
{items}

Reply with:
- question_type: "ordering"
- ordered_sequence: array of strings in correct chronological or procedural sequence (e.g. ["1. De-encapsulate frame", "2. Inspect IP header", ...]).
- answer_text: concise summary of the sequence.
- topic: the concept this tests, in 2-5 words.
- explanation: 2-4 short sentences explaining why this sequence is correct."""

# Backwards compatibility alias
SOLVE_PROMPT = SOLVE_PROMPT_MC

READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "question", "choices", "your_labels", "correct_labels"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "no_question", "cut_off"]},
        "question_type": {
            "type": "string",
            "enum": ["multiple_choice", "matching", "fill_in_the_blank", "ordering"],
        },
        "question": {"type": "string"},
        "exhibit_text": {"type": "string"},
        "choices": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "text"],
                "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
            },
        },
        "select_count": {"type": "integer"},
        "matching_items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "sources": {"type": "array", "items": {"type": "string"}},
                "targets": {"type": "array", "items": {"type": "string"}},
            },
        },
        "ordered_items": {"type": "array", "items": {"type": "string"}},
        "blank_answers": {"type": "array", "items": {"type": "string"}},
        "your_labels": {"type": "array", "items": {"type": "string"}},
        "correct_labels": {"type": "array", "items": {"type": "string"}},
    },
}

SOLVE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topic", "explanation"],
    "properties": {
        "question_type": {
            "type": "string",
            "enum": ["multiple_choice", "matching", "fill_in_the_blank", "ordering"],
        },
        "topic": {"type": "string"},
        "explanation": {"type": "string"},
        "answer_labels": {"type": "array", "items": {"type": "string"}},
        "answer_text": {"type": "string"},
        "matching_pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "target"],
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                },
            },
        },
        "blank_answers": {"type": "array", "items": {"type": "string"}},
        "ordered_sequence": {"type": "array", "items": {"type": "string"}},
        "why_previous_wrong": {"type": "string"},
    },
}


def load_settings(path: Path | None = None) -> dict:
    try:
        return json.loads((path or SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(s: dict, path: Path | None = None) -> None:
    try:
        p = path or SETTINGS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except OSError:
        pass


def question_key(read: dict) -> str:
    """The same question read twice (the page redrew, you scrolled a little) gives the same key."""
    parts = [read.get("question", "")]
    for c in read.get("choices") or []:
        parts.append(c.get("text", ""))
    m_items = read.get("matching_items") or {}
    for s in m_items.get("sources", []):
        parts.append(str(s))
    for o in read.get("ordered_items") or []:
        parts.append(str(o))
    text = "|".join(parts)
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:400]


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def pick_indexes(labels, choices) -> list[int]:
    """Which choices the model meant, in choice order. It may name a choice by its label ('B', 'b.', 'B)') or by its
    text ('Hub') - seen live: it took the radio circles for labels ('O' on every choice) and named answers by text."""
    keys = [_norm(str(c.get("label", "")).strip().rstrip(".)")) for c in choices]
    texts = [_norm(c.get("text", "")) for c in choices]
    out: set[int] = set()
    for x in labels or []:
        k = _norm(str(x).strip().rstrip(".)"))
        if not k:
            continue
        if keys.count(k) == 1:
            out.add(keys.index(k))
        elif k in texts:
            out.add(texts.index(k))
        else:
            hits = [i for i, t in enumerate(texts) if t and (t.startswith(k) or k.startswith(t))]
            if len(hits) == 1:
                out.add(hits[0])
    return sorted(out)


def tidy_read(read: dict) -> dict:
    """Fix the labels: odd or repeated ones (radio circles, bullets) become A, B, C... in order; yours/correct become
    those labels. Also normalizes question_type and non-multiple-choice fields."""
    read = dict(read)
    q_type = read.get("question_type") or "multiple_choice"
    read["question_type"] = q_type
    choices = [dict(c) for c in read.get("choices") or [] if str(c.get("text", "")).strip()]
    if choices:
        yours, correct = pick_indexes(read.get("your_labels"), choices), pick_indexes(read.get("correct_labels"), choices)
        labels = [str(c.get("label", "")).strip().rstrip(".)") for c in choices]
        if len(set(labels)) != len(labels) or not all(re.fullmatch(r"[A-Za-z]|\d{1,2}", lb) for lb in labels):
            labels = [chr(65 + i) if i < 26 else str(i + 1) for i in range(len(choices))]
        for c, lb in zip(choices, labels):
            c["label"] = lb.upper()
        read["choices"] = choices
        read["your_labels"] = [choices[i]["label"] for i in yours]
        read["correct_labels"] = [choices[i]["label"] for i in correct]
    else:
        read["choices"] = []
        read["your_labels"] = [str(x) for x in read.get("your_labels") or []]
        read["correct_labels"] = [str(x) for x in read.get("correct_labels") or []]
    return read


def choice_line(labels: list[str], choices: list[dict]) -> str:
    by = {c.get("label"): c.get("text", "") for c in choices}
    return "; ".join(f"{lb}. {by.get(lb, '')}".strip() for lb in labels) or "(none)"


def explain_messages(read: dict) -> list[dict]:
    choices = read.get("choices") or []
    yours, correct = read.get("your_labels") or [], read.get("correct_labels") or []
    if correct:
        correct_line = f"The correct answer (as marked by the exam): {choice_line(correct, choices)}"
        what = "why the correct answer is right and why the student's answer is wrong"
    else:
        correct_line = "The exam marked the student's answer wrong but didn't show the correct one."
        what = ("why the student's answer is wrong and which idea to review; don't guess which choice is correct, "
                "the student will check it on the next attempt")
    prompt = (
        f"Question: {read.get('question')}\n"
        f"Student selection: {choice_line(yours, choices)}\n"
        f"Choices:\n" + "\n".join(f"{c['label']}. {c['text']}" for c in choices) + "\n"
        f"{correct_line}\nExplain {what}."
    )
    return [{"role": "system", "content": "You are an expert exam tutor explaining mistakes."}, {"role": "user", "content": prompt}]


def solve_messages(read: dict) -> list[dict]:
    q_type = read.get("question_type", "multiple_choice")
    exhibit_str = f"\nExhibit / Diagram:\n{read['exhibit_text']}\n" if read.get("exhibit_text") else ""

    if q_type == "matching":
        m_items = read.get("matching_items") or {}
        sources = "\n".join(f"- {s}" for s in m_items.get("sources", [])) or "(see question)"
        targets = "\n".join(f"- {t}" for t in m_items.get("targets", [])) or "(see question)"
        prompt = SOLVE_PROMPT_MATCHING.format(
            question=read["question"],
            exhibit=exhibit_str,
            sources=sources,
            targets=targets,
        )
    elif q_type == "fill_in_the_blank":
        prompt = SOLVE_PROMPT_BLANK.format(
            question=read["question"],
            exhibit=exhibit_str,
        )
    elif q_type == "ordering":
        items = "\n".join(f"- {item}" for item in read.get("ordered_items", [])) or "(see question)"
        prompt = SOLVE_PROMPT_ORDERING.format(
            question=read["question"],
            exhibit=exhibit_str,
            items=items,
        )
    else:  # multiple_choice
        choices = read.get("choices") or []
        prompt = SOLVE_PROMPT_MC.format(
            question=read["question"],
            exhibit=exhibit_str,
            choices="\n".join(f"{c['label']}. {c['text']}" for c in choices),
        )
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": prompt}]


def read_messages(png: bytes) -> list[dict]:
    url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return [{"role": "system", "content": READ_SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": READ_PROMPT},
                                         {"type": "image_url", "image_url": {"url": url}}]}]


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def append_note(item: dict, notes_dir: Path | None = None, now: datetime | None = None) -> Path:
    """One question -> today's practice exam note in 1Notebook\\School (made on first use)."""
    notes_dir, now = notes_dir or NOTES_DIR, now or datetime.now()
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{now:%Y-%m-%d} practice exam.md"
    head = "" if path.exists() else (f"# Practice exam {now:%Y-%m-%d}\n\nQuestions from practice exam, "
                                     f"solved and explained in real time by local AI ({MODEL}).\n")

    q_type = item.get("question_type", "multiple_choice")
    type_tag = ""
    if q_type == "matching":
        type_tag = " (Matching)"
    elif q_type == "fill_in_the_blank":
        type_tag = " (Fill in Blank)"
    elif q_type == "ordering":
        type_tag = " (Ordering)"

    topic_title = item.get("topic") or "Question"
    body = f"\n## {topic_title}{type_tag} ({now:%H:%M})\n\n{item['question']}\n\n"

    if item.get("exhibit_text"):
        body += f"> [!NOTE] **Exhibit / Diagram**\n> ```\n> " + item["exhibit_text"].replace("\n", "\n> ") + "\n> ```\n\n"

    if q_type == "matching" and item.get("matching_pairs"):
        body += "| Item | Target / Category |\n| :--- | :--- |\n"
        for p in item["matching_pairs"]:
            body += f"| **{p.get('source', '')}** | {p.get('target', '')} |\n"
        body += f"\n**Answer:** {item.get('answer') or item.get('correct')}\n\n"
    elif q_type == "fill_in_the_blank" and item.get("blank_answers"):
        body += "**Command / Value:**\n```cisco\n" + "\n".join(item["blank_answers"]) + "\n```\n\n"
        body += f"**Answer:** {item.get('answer') or item.get('correct')}\n\n"
    elif q_type == "ordering" and item.get("ordered_sequence"):
        body += "**Correct Sequence:**\n"
        for idx, step in enumerate(item["ordered_sequence"], 1):
            line = step if re.match(r"^\d+\.", step) else f"{idx}. {step}"
            body += f"{line}\n"
        body += f"\n**Answer:** {item.get('answer') or item.get('correct')}\n\n"
    else:
        choices = item.get("choices") or []
        if choices:
            sel_labels = set(item.get("answer_labels") or item.get("correct_labels") or [])
            if not sel_labels and item.get("answer"):
                sel_labels = set(re.findall(r"\b([A-Z])\b", str(item.get("answer"))))
            c_lines = []
            for c in choices:
                lb = c.get("label", "").strip()
                txt = c.get("text", "")
                if lb in sel_labels:
                    c_lines.append(f"- [x] **{lb}.** {txt}")
                else:
                    c_lines.append(f"- [ ] {lb}. {txt}")
            body += "\n".join(c_lines) + "\n\n"
        body += f"**Answer:** {item.get('answer') or item.get('correct')}\n\n"

    if item.get("yours") and item.get("yours") != "(none)":
        body += f"**Your selection:** {item['yours']}\n\n"
    if item.get("explanation"):
        body += f"{item['explanation']}\n"

    with open(path, "a", encoding="utf-8") as f:
        f.write(head + body)
    return path


def append_gemini_retry(item: dict, notes_dir: Path | None = None, now: datetime | None = None) -> Path:
    """Appends Gemini API re-evaluation to today's note after user marks an answer as bad."""
    notes_dir, now = notes_dir or NOTES_DIR, now or datetime.now()
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{now:%Y-%m-%d} practice exam.md"
    why_wrong = f"> **Why previous answer was wrong:** {item['why_previous_wrong']}\n" if item.get("why_previous_wrong") else ""
    q_type = item.get("question_type", "multiple_choice")
    type_info = f"> **Format:** {q_type.replace('_', ' ').title()}\n" if q_type != "multiple_choice" else ""

    extra = ""
    if q_type == "matching" and item.get("matching_pairs"):
        extra = "> \n> | Item | Target |\n> | :--- | :--- |\n" + "".join(f"> | **{p.get('source')}** | {p.get('target')} |\n" for p in item["matching_pairs"]) + "> \n"
    elif q_type == "fill_in_the_blank" and item.get("blank_answers"):
        extra = "> \n> **Command:**\n> ```cisco\n> " + "\n> ".join(item["blank_answers"]) + "\n> ```\n> \n"
    elif q_type == "ordering" and item.get("ordered_sequence"):
        extra = "> \n> **Sequence:**\n" + "".join(f"> {s}\n" for s in item["ordered_sequence"]) + "> \n"

    body = (
        f"\n> [!WARNING] **Marked Bad · Re-attempt via Gemini 3.8 Flash** ({now:%H:%M})\n"
        f"{type_info}"
        f"> **Revised Answer:** {item.get('answer') or item.get('correct')}\n"
        f"> **Topic:** {item.get('topic') or 'Exam Revision'}\n"
        f"{extra}"
        f"> **Explanation:** {item.get('explanation') or ''}\n"
        f"{why_wrong}\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(body)
    return path


class ReviewWatcher:
    """The watch loop and its state. Thread-safe: the dashboard, the ticker and the tray read `state()`."""

    def __init__(self, *, grab_thumb=None, grab_png=None, client_factory=None, lock=None, mode_fn=None,
                 settings_path: Path | None = None, notes_dir: Path | None = None, interval: float = INTERVAL_S,
                 settle: float = SETTLE_S, idle_stop: float = IDLE_STOP_S, clock=time.monotonic,
                 on_item: Callable[[dict], None] | None = None):
        from . import quiz_capture
        self._grab_thumb = grab_thumb or quiz_capture.grab_thumb
        self._grab_png = grab_png or quiz_capture.grab_png
        self._changed = quiz_capture.changed
        self._client_factory = client_factory
        self._lock_fn = lock
        self._mode_fn = mode_fn
        self._settings_path, self._notes_dir = settings_path, notes_dir
        self.interval, self.settle, self.idle_stop, self._clock = interval, settle, idle_stop, clock
        self._on_item = on_item
        self._mu = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._force = False
        self._last_key: str | None = None
        self._last_png_bytes: bytes | None = None
        self._additional_snips: list[bytes] = []
        self._stitched_png_bytes: bytes | None = None
        self._pending_scroll: bool = False
        self._last_cut_off_png: bytes | None = None
        rect = load_settings(settings_path).get("rect")
        self._s = {"status": "stopped", "text": "Set a box around the question area, then start.",
                   "rect": rect if isinstance(rect, list) and len(rect) == 4 else None,
                   "current": None, "history": [], "correct": 0, "wrong": 0, "answered": 0, "topics": {},
                   "note": None, "since": None, "next_look": None}

    def set_on_item(self, cb: Callable[[dict], None] | None) -> None:
        self._on_item = cb

    # ---- what the views read
    def state(self) -> dict:
        with self._mu:
            s = json.loads(json.dumps(self._s))
        s["running"] = bool(self._thread and self._thread.is_alive())
        s["parts_count"] = len(self._additional_snips) + (1 if self._last_png_bytes else 0)
        try:
            from . import gemini_solver
            s["has_gemini_key"] = bool(gemini_solver.get_api_key(self._settings_path))
        except Exception:
            s["has_gemini_key"] = False
        return s

    def _set(self, **kw) -> None:
        with self._mu:
            self._s.update(kw)

    # ---- controls (any thread)
    def set_rect(self, rect, start: bool = True) -> None:
        rect = [int(v) for v in rect]
        self._set(rect=rect)
        s = load_settings(self._settings_path)
        s["rect"] = rect
        save_settings(s, self._settings_path)
        if start:
            self.start(restart=True)

    def start(self, restart: bool = False) -> dict:
        if not self._s.get("rect"):
            return {"ok": False, "why": "set the box first"}
        if self._thread and self._thread.is_alive():
            if not restart:
                return {"ok": True, "why": "already watching"}
            self.stop(wait=True)
        self._stop.clear()
        self._force, self._last_key = True, None
        self._set(status="watching", text="Starting: reading what the box shows now", since=time.strftime("%H:%M:%S"))
        self._thread = threading.Thread(target=self._run, name="exam-review", daemon=True)
        self._thread.start()
        return {"ok": True, "why": f"watching the box (every {int(self.interval)} s)"}

    def stop(self, wait: bool = False, why: str = "Stopped.") -> dict:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if wait and t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._set(status="stopped", text=why, next_look=None)
        return {"ok": True, "why": "stopped"}

    def look_now(self) -> dict:
        """Read the box again now, even if it looks the same (e.g. the model misread it)."""
        if not (self._thread and self._thread.is_alive()):
            return self.start()
        self._force = True
        self._wake.set()
        return {"ok": True, "why": "looking again now"}

    def clear(self) -> dict:
        self._last_key = None
        self._last_png_bytes = None
        self._additional_snips = []
        self._stitched_png_bytes = None
        self._pending_scroll = False
        self._last_cut_off_png = None
        self._set(current=None, history=[], correct=0, wrong=0, answered=0, topics={})
        return {"ok": True, "why": "cleared this session's list"}

    def add_snip(self, rect: list[int] | None = None) -> dict:
        """Capture an additional in-memory snip for tall/scrolled multi-page questions, stitching vertically."""
        target_rect = rect or self._s.get("rect")
        if not target_rect:
            return {"ok": False, "why": "No capture box set"}
        try:
            from .quiz_capture import stitch_pngs_vertical
            png = self._grab_png(target_rect)
            self._additional_snips.append(png)
            all_parts = ([self._last_png_bytes] if self._last_png_bytes else []) + self._additional_snips
            self._stitched_png_bytes = stitch_pngs_vertical(all_parts, dedup_overlap=True)
            count = len(self._additional_snips) + (1 if self._last_png_bytes else 0)
            msg = f"Captured scroll snip #{len(self._additional_snips)} ({count} parts stitched in memory)"
            self._set(text=msg)
            return {"ok": True, "why": msg, "count": count, "stitched": bool(self._stitched_png_bytes)}
        except Exception as e:
            return {"ok": False, "why": f"Failed to capture scroll snip: {e}"}

    def auto_scroll_and_solve(self, clicks: int = -4, restore_scroll: bool = True) -> dict:
        """One-click / hotkey auto-scroll: captures slice 1, simulates mouse wheel down,
        captures slice 2, restores scroll position, stitches with overlap deduplication, and solves."""
        target_rect = self._s.get("rect")
        if not target_rect:
            return {"ok": False, "why": "No capture box set"}
        try:
            from .quiz_capture import scroll_window, stitch_pngs_vertical
            self._set(status="reading", text="Auto-scrolling question view…")

            # Slice 1 (top of question)
            slice1 = self._last_cut_off_png or self._last_png_bytes or self._grab_png(target_rect)
            self._last_png_bytes = slice1

            # Auto-scroll down
            scroll_window(target_rect, clicks=clicks, wait_s=0.25)

            # Slice 2 (bottom of question)
            slice2 = self._grab_png(target_rect)

            # Optional: scroll back up to starting position
            if restore_scroll:
                scroll_window(target_rect, clicks=-clicks, wait_s=0.05)

            # Stitch with overlap deduplication
            self._additional_snips = [slice2]
            stitched = stitch_pngs_vertical([slice1, slice2], dedup_overlap=True)
            self._stitched_png_bytes = stitched
            self._pending_scroll = False
            self._last_cut_off_png = None

            # Solve immediately
            solved = self._process(stitched, force=True)
            return {"ok": solved, "why": "Auto-scrolled, stitched, and solved", "parts": 2}
        except Exception as e:
            return {"ok": False, "why": f"Auto-scroll failed: {e}"}

    def retry_gemini(self, model: str = "gemini-3.8-flash") -> dict:
        """User marked current answer bad: send in-memory images to Gemini API for a new answer."""
        cur = self._s.get("current")
        if not cur and not self._last_png_bytes and not self._stitched_png_bytes:
            return {"ok": False, "why": "No active question to re-solve"}

        images = []
        if self._stitched_png_bytes:
            images.append(self._stitched_png_bytes)
        else:
            if self._last_png_bytes:
                images.append(self._last_png_bytes)
            elif self._s.get("rect"):
                try:
                    png = self._grab_png(self._s["rect"])
                    self._last_png_bytes = png
                    images.append(png)
                except Exception:
                    pass
            images.extend(self._additional_snips)
        if not images:
            return {"ok": False, "why": "No screenshot in memory"}

        self._set(status="answering", text=f"Re-solving with Gemini ({model})…")
        try:
            from . import gemini_solver
            sol = gemini_solver.solve_with_gemini(
                images,
                previous_item=cur,
                question_text=(cur or {}).get("question"),
                model=model,
            )
            q_type = sol.get("question_type") or (cur or {}).get("question_type") or "multiple_choice"
            raw_labels = sol.get("answer_labels") or []
            choices = sol.get("choices") or (cur or {}).get("choices") or []
            ans_idxs = pick_indexes(raw_labels, choices) if choices else []
            ans_labels = [choices[i]["label"] for i in ans_idxs] if ans_idxs else [str(x).strip().upper() for x in raw_labels]

            matching_pairs = sol.get("matching_pairs") or (cur or {}).get("matching_pairs") or []
            blank_answers = sol.get("blank_answers") or (cur or {}).get("blank_answers") or []
            ordered_sequence = sol.get("ordered_sequence") or (cur or {}).get("ordered_sequence") or []

            if q_type == "matching" and matching_pairs:
                answer_text = "; ".join(f"{p.get('source')} ➔ {p.get('target')}" for p in matching_pairs)
            elif q_type == "fill_in_the_blank" and blank_answers:
                answer_text = "\n".join(blank_answers)
            elif q_type == "ordering" and ordered_sequence:
                answer_text = " ➔ ".join(ordered_sequence)
            elif q_type == "multiple_choice" and choices:
                answer_text = choice_line(ans_labels, choices)
            else:
                answer_text = sol.get("answer_text") or (choice_line(ans_labels, choices) if choices else ", ".join(ans_labels))
            if not answer_text and ans_labels:
                answer_text = ", ".join(ans_labels)

            revised_item = dict(cur or {})
            revised_item.update({
                "question_type": q_type,
                "choices": choices,
                "answer_labels": ans_labels,
                "correct_labels": ans_labels,
                "matching_pairs": matching_pairs,
                "blank_answers": blank_answers,
                "ordered_sequence": ordered_sequence,
                "answer": answer_text,
                "correct": answer_text,
                "topic": sol.get("topic") or (cur or {}).get("topic") or "Exam Revision",
                "explanation": sol.get("explanation") or "",
                "why_previous_wrong": sol.get("why_previous_wrong") or "",
                "model": model,
                "gemini_retried": True,
                "at": time.strftime("%H:%M:%S"),
            })
            if not revised_item.get("question"):
                revised_item["question"] = sol.get("question") or (f"[{sol.get('topic')}]" if sol.get("topic") else "Exam Question")

            note_path = str(append_gemini_retry(revised_item, self._notes_dir))
            with self._mu:
                self._s["current"] = revised_item
                self._s["note"] = note_path
                if self._s.get("history"):
                    self._s["history"][0]["topic"] = revised_item["topic"]
                    self._s["history"][0]["gemini"] = True

            done_text = f"✨ Gemini Answer: {revised_item['answer']}"
            self._set(status="watching", text=done_text)
            self._additional_snips = []
            if self._on_item:
                try:
                    self._on_item(revised_item)
                except Exception:
                    pass
            return {"ok": True, "why": done_text, "item": revised_item}
        except Exception as e:
            err_msg = f"Gemini retry failed: {type(e).__name__}: {str(e)[:160]}"
            self._set(status="error", text=err_msg)
            return {"ok": False, "why": err_msg}

    # ---- the loop
    def _run(self) -> None:
        baseline: bytes | None = None
        last_new = self._clock()
        while not self._stop.is_set():
            rect = self._s["rect"]
            try:
                thumb = self._grab_thumb(rect)
                if self._force or self._changed(baseline, thumb) >= CHANGE:
                    thumb = self._settled(rect, thumb)
                    if self._stop.is_set():
                        break
                    png = self._grab_png(rect)
                    force, self._force = self._force, False
                    baseline = thumb                      # the next look compares with this screen
                    if self._pending_scroll and self._last_cut_off_png:
                        from .quiz_capture import stitch_pngs_vertical
                        slice1 = self._last_cut_off_png
                        slice2 = png
                        stitched = stitch_pngs_vertical([slice1, slice2], dedup_overlap=True)
                        self._additional_snips = [slice2]
                        self._stitched_png_bytes = stitched
                        self._pending_scroll = False
                        self._last_cut_off_png = None
                        if self._process(stitched, force=True):
                            last_new = self._clock()
                    else:
                        if self._process(png, force):
                            last_new = self._clock()
            except Exception as e:  # noqa: BLE001 - one bad look (screen locked, engine hiccup) must not end the watch
                self._set(status="error", text=f"{type(e).__name__}: {str(e)[:200]}")
            if self._clock() - last_new > self.idle_stop:
                self.stop(why=f"Stopped by itself: no new question for {int(self.idle_stop // 60)} minutes.")
                break
            self._set(next_look=time.strftime("%H:%M:%S", time.localtime(time.time() + self.interval)))
            if self._s["status"] in ("reading", "explaining", "waiting"):
                self._set(status="watching")
            self._wake.wait(self.interval)
            self._wake.clear()

    def _settled(self, rect, thumb: bytes) -> bytes:
        """Wait until the page stops changing (loading, scrolling, animations), up to SETTLE_TRIES looks."""
        for _ in range(SETTLE_TRIES):
            if self._stop.wait(self.settle):
                break
            again = self._grab_thumb(rect)
            if self._changed(thumb, again) < STILL:
                return again
            thumb = again
        return thumb

    def _client(self):
        if self._client_factory:
            return self._client_factory()
        from .chains.router import RouterClient
        return RouterClient(timeout=600)

    def _mode(self) -> str:
        if self._mode_fn:
            return self._mode_fn()
        from .chains.router import engine_mode
        return engine_mode()

    def _gpu(self, what: str):
        if self._lock_fn:
            return self._lock_fn(what)
        from .chains.gpulock import gpu_lock
        return gpu_lock(what, should_stop=self._stop.is_set,
                        on_wait=lambda h: self._set(status="waiting",
                                                    text=f"Waiting for the GPU ({(h or {}).get('what') or 'another job'})"))

    def _process(self, png: bytes, force: bool) -> bool:
        """Read the box and explain it if it's a missed question. True when it was a new graded question."""
        from .chains.gpulock import LockTimeout
        from .chains.llm import LLMError
        mode = self._mode()
        if mode in ("off", "away"):
            self._force = True                    # try again next look
            self._set(status="paused", text="The local AI is off (game guard)" if mode == "off"
                      else "Away mode has the GPU: this waits until Desk mode")
            return False
        self._last_png_bytes = png
        t0 = time.monotonic()
        try:
            with self._gpu("exam review"):
                self._set(status="reading", text="Reading the box…")
                client = self._client()
                r = client.chat(MODEL, read_messages(png), schema=READ_SCHEMA, max_tokens=2048, reasoning="off",
                                temperature=0)
                read = tidy_read(parse_json(r.content))
                choices = read["choices"]
                status = read.get("status")
                if status == "cut_off":
                    self._pending_scroll = True
                    self._last_cut_off_png = png
                    self._set(status="waiting", text="Question cut off at bottom: draw a bigger box or scroll down to auto-solve")
                    return False
                if status != "ok" or not read.get("question"):
                    self._set(status="watching", text="No question in the box right now")
                    return False
                key = question_key(read)
                if key == self._last_key and not force:
                    self._set(status="watching", text="Same question as before")
                    return False
                if self._last_key is not None and key != self._last_key:
                    self._additional_snips = []
                    self._stitched_png_bytes = None
                self._last_key = key
                self._pending_scroll = False
                self._last_cut_off_png = None

                # Solve immediately in real time: no waiting for grading, no grading checks or restrictions!
                self._set(status="answering", text="Solving question in real time…")
                s = client.chat(MODEL, solve_messages(read), schema=SOLVE_SCHEMA, max_tokens=1024, reasoning="off")
                sol = parse_json(s.content)
                q_type = sol.get("question_type") or read.get("question_type") or "multiple_choice"

                raw_ans = sol.get("answer_labels") or []
                ans_idxs = pick_indexes(raw_ans, choices) if choices else []
                ans_labels = [choices[i]["label"] for i in ans_idxs] if ans_idxs else [str(x).strip().upper() for x in raw_ans]

                matching_pairs = sol.get("matching_pairs") or []
                blank_answers = sol.get("blank_answers") or []
                ordered_sequence = sol.get("ordered_sequence") or []

                if q_type == "matching" and matching_pairs:
                    answer_text = "; ".join(f"{p.get('source')} ➔ {p.get('target')}" for p in matching_pairs)
                elif q_type == "fill_in_the_blank" and blank_answers:
                    answer_text = "\n".join(blank_answers)
                elif q_type == "ordering" and ordered_sequence:
                    answer_text = " ➔ ".join(ordered_sequence)
                elif q_type == "multiple_choice" and choices:
                    answer_text = choice_line(ans_labels, choices)
                else:
                    answer_text = sol.get("answer_text") or (choice_line(ans_labels, choices) if choices else ", ".join(ans_labels))
                if not answer_text and ans_labels:
                    answer_text = ", ".join(ans_labels)

                item = {
                    "question_type": q_type,
                    "question": read["question"],
                    "exhibit_text": read.get("exhibit_text") or "",
                    "choices": choices,
                    "matching_pairs": matching_pairs,
                    "blank_answers": blank_answers,
                    "ordered_sequence": ordered_sequence,
                    "result": "live",
                    "your_labels": read.get("your_labels") or [],
                    "correct_labels": ans_labels,
                    "answer_labels": ans_labels,
                    "yours": choice_line(read.get("your_labels") or [], choices) if choices else "",
                    "answer": answer_text,
                    "correct": answer_text,
                    "topic": str(sol.get("topic") or "").strip(),
                    "explanation": str(sol.get("explanation") or "").strip(),
                    "your_mistake": "",
                    "at": time.strftime("%H:%M:%S"),
                    "seconds": round(time.monotonic() - t0, 1),
                }

                if q_type == "matching" and matching_pairs:
                    done_text = f"Matched: {len(matching_pairs)} pairs ({item['seconds']:.0f} s)"
                elif q_type == "fill_in_the_blank" and blank_answers:
                    done_text = f"Command: {blank_answers[0][:30]} ({item['seconds']:.0f} s)"
                elif q_type == "ordering" and ordered_sequence:
                    done_text = f"Ordered: {len(ordered_sequence)} steps ({item['seconds']:.0f} s)"
                else:
                    done_text = f"Answer: {item['answer']} ({item['seconds']:.0f} s)"
            try:
                note = str(append_note(item, self._notes_dir))
            except OSError as err:
                note = f"(not saved: {err})"
            self._record(item, note)
            self._set(status="watching", text=done_text)
            if self._on_item:
                try:
                    self._on_item(item)
                except Exception:
                    pass
            return True
        except LockTimeout:
            return False
        except (LLMError, ValueError, KeyError) as err:
            self._force = True
            self._set(status="error", text=f"The AI step failed ({type(err).__name__}: {str(err)[:160]}); trying "
                                           "again on the next look")
            return False

    def _record(self, item: dict, note: str | None = None) -> None:
        with self._mu:
            s = self._s
            s["current"] = item
            s["answered"] = s.get("answered", 0) + 1
            s["history"] = ([{"n": s["answered"], "ok": True,
                              "topic": item["topic"], "question": item["question"][:140]}] + s["history"])[:HISTORY]
            if item.get("topic"):
                s["topics"][item["topic"]] = s["topics"].get(item["topic"], 0) + 1
            if note:
                s["note"] = note


_watcher: ReviewWatcher | None = None
_watcher_mu = threading.Lock()


def watcher() -> ReviewWatcher:
    global _watcher
    with _watcher_mu:
        if _watcher is None:
            _watcher = ReviewWatcher()
        return _watcher


def ticker_state() -> dict | None:
    """What the ticker's REVIEW slide needs, or None when the helper isn't in use."""
    w = _watcher
    if w is None:
        return None
    s = w.state()
    if not s["running"] and not s["current"]:
        return None
    return {k: s[k] for k in ("status", "text", "running", "current", "correct", "wrong", "answered")}
