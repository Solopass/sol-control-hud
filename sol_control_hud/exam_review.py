"""Exam review helper: watches one box of the screen while you go through a graded results / review page and explains
the questions you got wrong, with the local AI (sol-vision).

- Set the box from the dashboard's Exam review card ("Set box & start"); it starts on whatever the box shows now.
- Every 15 s it takes a small grey copy of the box and compares it with the last question's. When enough of it changed
  (you scrolled or went to the next question) it waits a moment for the page to settle and copies the box.
- Step 1 (Read, sol-vision with the image): copy the question, the choices, which ones the page marks as yours and
  which as correct. A question the page hasn't graded (nothing marked right or wrong) is left alone: this explains
  results, it never answers a question for you.
- Step 2 (Explain, sol-vision text only: the same model, so the router never swaps presets): only for a wrong answer -
  the topic, why the correct answer is right, why yours isn't. Correct answers are just counted.
- No new screenshot until the explanation is ready. One GPU job at a time (gpu.lock), like the chains.
- Every missed question goes into today's note in 1Notebook\\School; stops by itself after 10 quiet minutes.
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

READ_SYSTEM = ("You transcribe multiple-choice quiz and practice exam questions exactly as shown on the screen.")
READ_PROMPT = """This screenshot shows a quiz or practice exam question.

If it shows one multiple-choice question in full, copy:
- question: the question text, exactly.
- choices: every answer choice. Use the letter or number the page prints before the choice (A, B, C... or 1, 2, 3...); if there is none, label them A, B, C... from top to bottom. Radio buttons and checkboxes (○ ◉ ☐ ☑) are not labels.
- your_labels: the labels of the choice(s) the page marks or has selected as the student's answer (a filled radio button or checkbox, "Your answer", "You selected"). Empty if none selected.
- correct_labels: the labels of the choice(s) the page marks as correct (a check mark, green highlight, "Correct answer"). Empty if the page doesn't show it.
- result: "correct" or "incorrect" as the page marks it, or "not_graded" if the question is not graded yet (e.g. an active quiz or practice exam).

Use status "ok" for that. Use status "no_question" (and leave the rest empty) if there is no multiple-choice question, and "cut_off" if the question or its choices run past the edge of the screenshot. Leave out navigation, buttons, timers and other page text."""

EXPLAIN_SYSTEM = ("You are a patient networking and IT tutor (CCNA level). You explain why an answer on a graded "
                  "practice question was right or wrong so the student understands the concept for next time.")
EXPLAIN_PROMPT = """A student got this multiple-choice question wrong on a graded practice exam.

Question: {question}

Choices:
{choices}

The student answered: {yours}
{correct_line}

Reply with:
- topic: the concept this tests, in 2-5 words (for example "Subnetting /26 hosts", "OSI layer 2 devices").
- explanation: 2-4 short sentences: {explain_what}. Show the key step (the calculation, the rule, the command) rather than just stating the result. Plain text only: no LaTeX, no $ signs, no Markdown (write 2^6 - 2 = 62).
- your_mistake: one sentence on the likely mistake behind the student's choice."""

SOLVE_SYSTEM = ("You are an expert IT and networking tutor (CCNA level). You solve multiple-choice practice "
                "exam questions, identifying the correct answer and providing concise step-by-step reasoning.")
SOLVE_PROMPT = """Solve this multiple-choice practice exam question.

Question: {question}

Choices:
{choices}

Reply with:
- answer_labels: array of labels for the correct choice(s) (e.g. ["C"]).
- topic: the concept this tests, in 2-5 words (for example "Subnetting /26 hosts", "OSI layer 2 devices").
- explanation: 2-4 short sentences explaining why this answer is correct and showing the key calculation, rule, or concept. Plain text only: no LaTeX, no $ signs, no Markdown (write 2^6 - 2 = 62)."""

READ_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "question", "choices", "your_labels", "correct_labels", "result"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "no_question", "cut_off"]},
        "question": {"type": "string"},
        "choices": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["label", "text"],
                                               "properties": {"label": {"type": "string"}, "text": {"type": "string"}}}},
        "your_labels": {"type": "array", "items": {"type": "string"}},
        "correct_labels": {"type": "array", "items": {"type": "string"}},
        "result": {"type": "string", "enum": ["correct", "incorrect", "not_graded"]},
    },
}
EXPLAIN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["topic", "explanation", "your_mistake"],
    "properties": {"topic": {"type": "string"}, "explanation": {"type": "string"}, "your_mistake": {"type": "string"}},
}
SOLVE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer_labels", "topic", "explanation"],
    "properties": {"answer_labels": {"type": "array", "items": {"type": "string"}},
                   "topic": {"type": "string"}, "explanation": {"type": "string"}},
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
    text = read.get("question", "") + "|" + "|".join(c.get("text", "") for c in read.get("choices") or [])
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
    those labels."""
    choices = [dict(c) for c in read.get("choices") or [] if str(c.get("text", "")).strip()]
    yours, correct = pick_indexes(read.get("your_labels"), choices), pick_indexes(read.get("correct_labels"), choices)
    labels = [str(c.get("label", "")).strip().rstrip(".)") for c in choices]
    if len(set(labels)) != len(labels) or not all(re.fullmatch(r"[A-Za-z]|\d{1,2}", lb) for lb in labels):
        labels = [chr(65 + i) if i < 26 else str(i + 1) for i in range(len(choices))]
    for c, lb in zip(choices, labels):
        c["label"] = lb.upper()
    return {**read, "choices": choices, "your_labels": [choices[i]["label"] for i in yours],
            "correct_labels": [choices[i]["label"] for i in correct]}


def choice_line(labels: list[str], choices: list[dict]) -> str:
    by = {c.get("label"): c.get("text", "") for c in choices}
    return "; ".join(f"{lb}. {by.get(lb, '')}".strip() for lb in labels) or "(none)"


def explain_messages(read: dict) -> list[dict]:
    choices = read["choices"]
    yours, correct = read.get("your_labels") or [], read.get("correct_labels") or []
    if correct:
        correct_line = f"The correct answer (as marked by the exam): {choice_line(correct, choices)}"
        what = "why the correct answer is right and why the student's answer is wrong"
    else:
        correct_line = "The exam marked the student's answer wrong but didn't show the correct one."
        what = ("why the student's answer is wrong and which idea to review; don't guess which choice is correct, "
                "the student will check it on the next attempt")
    prompt = EXPLAIN_PROMPT.format(question=read["question"], yours=choice_line(yours, choices),
                                   choices="\n".join(f"{c['label']}. {c['text']}" for c in choices),
                                   correct_line=correct_line, explain_what=what)
    return [{"role": "system", "content": EXPLAIN_SYSTEM}, {"role": "user", "content": prompt}]


def solve_messages(read: dict) -> list[dict]:
    choices = read["choices"]
    prompt = SOLVE_PROMPT.format(question=read["question"],
                                 choices="\n".join(f"{c['label']}. {c['text']}" for c in choices))
    return [{"role": "system", "content": SOLVE_SYSTEM}, {"role": "user", "content": prompt}]


def read_messages(png: bytes) -> list[dict]:
    url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return [{"role": "system", "content": READ_SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": READ_PROMPT},
                                         {"type": "image_url", "image_url": {"url": url}}]}]


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    return json.loads(text)


def append_note(item: dict, notes_dir: Path | None = None, now: datetime | None = None) -> Path:
    """One question -> today's note in 1Notebook\\School (made on first use)."""
    notes_dir, now = notes_dir or NOTES_DIR, now or datetime.now()
    notes_dir.mkdir(parents=True, exist_ok=True)
    is_live = item.get("result") == "live"
    path = notes_dir / (f"{now:%Y-%m-%d} practice exam.md" if is_live else f"{now:%Y-%m-%d} exam review.md")
    if not path.exists():
        head = (f"# Practice exam {now:%Y-%m-%d}\n\nQuestions from practice exam, solved and explained in real time "
                f"by local AI ({MODEL}).\n") if is_live else (
                f"# Exam review {now:%Y-%m-%d}\n\nQuestions missed on practice attempts, explained by the local AI "
                f"({MODEL}). Check anything that looks off.\n")
    else:
        head = ""

    body = (f"\n## {item.get('topic') or 'Question'} ({now:%H:%M})\n\n{item['question']}\n\n"
            + "\n".join(f"- {c['label']}. {c['text']}" for c in item["choices"])
            + "\n\n")
    if is_live:
        body += f"**Recommended Answer:** {item.get('answer') or item.get('correct')}\n\n"
        if item.get("yours") and item.get("yours") != "(none)":
            body += f"**Your selection:** {item['yours']}\n\n"
        if item.get("explanation"):
            body += f"{item['explanation']}\n"
    else:
        body += (f"**Your answer:** {item['yours']}  \n**Correct:** {item['correct'] or '(not shown)'}\n\n"
                 + f"{item['explanation']}\n\n*Likely mistake:* {item.get('your_mistake', '')}\n")

    with open(path, "a", encoding="utf-8") as f:
        f.write(head + body)
    return path


class ReviewWatcher:
    """The watch loop and its state. Thread-safe: the dashboard, the ticker and the tray read `state()`."""

    def __init__(self, *, grab_thumb=None, grab_png=None, client_factory=None, lock=None, mode_fn=None,
                 settings_path: Path | None = None, notes_dir: Path | None = None, interval: float = INTERVAL_S,
                 settle: float = SETTLE_S, idle_stop: float = IDLE_STOP_S, clock=time.monotonic):
        from . import quiz_capture
        self._grab_thumb = grab_thumb or quiz_capture.grab_thumb
        self._grab_png = grab_png or quiz_capture.grab_png
        self._changed = quiz_capture.changed
        self._client_factory = client_factory
        self._lock_fn = lock
        self._mode_fn = mode_fn
        self._settings_path, self._notes_dir = settings_path, notes_dir
        self.interval, self.settle, self.idle_stop, self._clock = interval, settle, idle_stop, clock
        self._mu = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._force = False
        self._last_key: str | None = None
        rect = load_settings(settings_path).get("rect")
        self._s = {"status": "stopped", "text": "Set a box around the question area, then start.",
                   "rect": rect if isinstance(rect, list) and len(rect) == 4 else None,
                   "current": None, "history": [], "correct": 0, "wrong": 0, "answered": 0, "topics": {},
                   "note": None, "since": None, "next_look": None}

    # ---- what the views read
    def state(self) -> dict:
        with self._mu:
            s = json.loads(json.dumps(self._s))
        s["running"] = bool(self._thread and self._thread.is_alive())
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
        self._set(current=None, history=[], correct=0, wrong=0, answered=0, topics={})
        return {"ok": True, "why": "cleared this session's list"}

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
                if status != "ok" or not read.get("question") or len(choices) < 2:
                    self._set(status="watching", text="The box cuts the question off: draw a bigger box"
                              if status == "cut_off" else "No question in the box right now")
                    return False
                key = question_key(read)
                if key == self._last_key and not force:
                    self._set(status="watching", text="Same question as before")
                    return False
                result = read.get("result")
                self._last_key = key
                item = {"question": read["question"], "choices": choices, "result": result,
                        "your_labels": read["your_labels"], "correct_labels": read["correct_labels"],
                        "yours": choice_line(read["your_labels"], choices),
                        "correct": choice_line(read["correct_labels"], choices) if read["correct_labels"] else "",
                        "answer_labels": read.get("correct_labels") or [],
                        "answer": choice_line(read["correct_labels"], choices) if read["correct_labels"] else "",
                        "topic": "", "explanation": "", "your_mistake": "", "at": time.strftime("%H:%M:%S")}
                read_s = time.monotonic() - t0
                if result == "correct":
                    item["seconds"] = round(read_s, 1)
                    item["topic"] = "Correct"
                    self._record(item)
                    self._set(status="watching", text="Correct: nothing to explain")
                    return True
                if result == "incorrect" and read.get("your_labels"):
                    self._set(status="explaining", text="Explaining the missed question…")
                    # thinking off: the exam already says which answer is right, and with thinking on Gemma spent all
                    # 2048 tokens thinking about a /26 question and never answered (measured 2026-10-07; off: 3 s, good)
                    e = client.chat(MODEL, explain_messages(read), schema=EXPLAIN_SCHEMA, max_tokens=1024, reasoning="off")
                    ex = parse_json(e.content)
                    item.update({k: str(ex.get(k) or "").strip() for k in ("topic", "explanation", "your_mistake")})
                    item["answer_labels"] = read["correct_labels"]
                    item["answer"] = item["correct"]
                    item["seconds"] = round(time.monotonic() - t0, 1)
                    done_text = f"Explained in {item['seconds']:.0f} s"
                else:
                    # Live / ungraded practice question: solve it in real time
                    self._set(status="answering", text="Solving practice question…")
                    s = client.chat(MODEL, solve_messages(read), schema=SOLVE_SCHEMA, max_tokens=1024, reasoning="off")
                    sol = parse_json(s.content)
                    raw_ans = sol.get("answer_labels") or []
                    ans_idxs = pick_indexes(raw_ans, choices)
                    ans_labels = [choices[i]["label"] for i in ans_idxs] if ans_idxs else [str(x).strip().upper() for x in raw_ans]
                    item["result"] = "live"
                    item["answer_labels"] = ans_labels
                    item["correct_labels"] = ans_labels
                    item["answer"] = choice_line(ans_labels, choices)
                    item["correct"] = item["answer"]
                    item["topic"] = str(sol.get("topic") or "").strip()
                    item["explanation"] = str(sol.get("explanation") or "").strip()
                    item["seconds"] = round(time.monotonic() - t0, 1)
                    done_text = f"Answer: {item['answer']} ({item['seconds']:.0f} s)"
            try:
                note = str(append_note(item, self._notes_dir))
            except OSError as err:
                note = f"(not saved: {err})"
            self._record(item, note)
            self._set(status="watching", text=done_text)
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
            ok = item["result"] in ("correct", "live")
            s["history"] = ([{"n": s["correct"] + s["wrong"] + s.get("answered", 0) + 1, "ok": ok,
                              "topic": item["topic"], "question": item["question"][:140]}] + s["history"])[:HISTORY]
            if item["result"] == "correct":
                s["correct"] += 1
            elif item["result"] == "incorrect":
                s["wrong"] += 1
                if item["topic"]:
                    s["topics"][item["topic"]] = s["topics"].get(item["topic"], 0) + 1
            elif item["result"] == "live":
                s["answered"] = s.get("answered", 0) + 1
                if item["topic"]:
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
