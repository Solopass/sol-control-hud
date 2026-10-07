"""The exam review helper: capture helpers, the read/explain step, the watch loop, the ticker slide."""
import json
import struct
import threading
import zlib
from contextlib import contextmanager

import pytest

from sol_control_hud import exam_review as er
from sol_control_hud import quiz_capture as qc
from sol_control_hud.chains.llm import ChatResult
from sol_control_hud.data import snapshot as sn

CHOICES = [{"label": "A", "text": "255.255.255.224"}, {"label": "B", "text": "255.255.255.128"},
           {"label": "C", "text": "255.255.255.192"}, {"label": "D", "text": "255.255.255.240"}]


def graded(result="incorrect", yours=("B",), correct=("C",), q="Which mask gives 50 hosts with the fewest host bits?"):
    return {"status": "ok", "question": q, "choices": CHOICES, "your_labels": list(yours),
            "correct_labels": list(correct), "result": result}


class FakeClient:
    def __init__(self, reads, explain=None):
        self.reads, self.calls = list(reads), []
        self.explain = explain or {"topic": "Subnetting", "explanation": "2^6 - 2 = 62 >= 50, so /26.",
                                   "your_mistake": "Picked a mask with too many host bits."}

    def chat(self, model, messages, **kw):
        self.calls.append((model, messages, kw))
        user = messages[-1]["content"]
        if isinstance(user, list):                       # the read step carries the image
            return ChatResult(content=json.dumps(self.reads.pop(0)), finish_reason="stop")
        return ChatResult(content=json.dumps(self.explain), finish_reason="stop")


@contextmanager
def no_lock(what):
    yield


def make(tmp_path, reads, mode="desk", **kw):
    client = FakeClient(reads)
    w = er.ReviewWatcher(grab_thumb=lambda r: b"\x00" * 64, grab_png=lambda r: b"png", client_factory=lambda: client,
                         lock=no_lock, mode_fn=lambda: mode, settings_path=tmp_path / "s.json",
                         notes_dir=tmp_path / "School", **kw)
    return w, client


# ---- capture helpers (no screen needed)
def test_png_is_a_valid_rgb_png():
    w, h = 3, 2
    bgra = bytes([10, 20, 30, 255] * (w * h))
    data = qc.png(w, h, bgra)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (3, 2)
    idat_len = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41:41 + idat_len])
    assert raw == (b"\x00" + bytes([30, 20, 10] * w)) * h           # BGRA -> RGB, filter byte per row


def test_changed_share_and_thumb_size():
    a = bytes([100] * 100)
    assert qc.changed(a, a) == 0
    assert qc.changed(a, bytes([100] * 90 + [200] * 10)) == pytest.approx(0.10)
    assert qc.changed(a, bytes([112] * 100)) == 0                    # small brightness drift is not a change
    assert qc.changed(None, a) == 1.0 and qc.changed(a, a[:50]) == 1.0
    assert qc.thumb_size(1920, 1080) == (160, 90)


def test_labels_are_normalized_to_the_page_labels():
    t = er.tidy_read({**graded(), "your_labels": ["b."], "correct_labels": ["c)", "Z"]})
    assert t["your_labels"] == ["B"] and t["correct_labels"] == ["C"]
    assert er.question_key(graded()) == er.question_key({**graded(), "question": "Which mask gives 50 hosts, with the fewest host bits??"})


def test_radio_circles_as_labels_and_answers_by_text():
    # seen live: every choice labelled 'O' (the radio circle) and the answers named by their text
    read = {"status": "ok", "question": "Which device works at Layer 2?", "result": "incorrect",
            "choices": [{"label": "O", "text": "Router"}, {"label": "O", "text": "Hub"},
                        {"label": "O", "text": "Switch"}, {"label": "O", "text": "Repeater"}],
            "your_labels": ["Hub"], "correct_labels": ["Switch"]}
    t = er.tidy_read(read)
    assert [c["label"] for c in t["choices"]] == ["A", "B", "C", "D"]
    assert t["your_labels"] == ["B"] and t["correct_labels"] == ["C"]
    assert er.tidy_read({**read, "your_labels": ["Hub (your answer)"]})["your_labels"] == ["B"]


def test_read_messages_send_the_image_and_fit_counts_it_once():
    from sol_control_hud.chains.router import message_images, message_text
    m = er.read_messages(b"\x89PNG fake")[1]
    assert message_images(m) == 1
    assert "base64" not in message_text(m) and "multiple-choice" in message_text(m)


# ---- one look
def test_missed_question_is_explained_and_noted(tmp_path):
    w, client = make(tmp_path, [graded()])
    assert w._process(b"png", force=True) is True
    s = w.state()
    assert s["wrong"] == 1 and s["topics"] == {"Subnetting": 1}
    assert s["current"]["yours"] == "B. 255.255.255.128" and s["current"]["correct"] == "C. 255.255.255.192"
    assert [c[0] for c in client.calls] == ["sol-vision", "sol-vision"]      # one model: the router never swaps
    explain_prompt = client.calls[1][1][-1]["content"]
    assert "C. 255.255.255.192" in explain_prompt and "B. 255.255.255.128" in explain_prompt
    note = (tmp_path / "School").glob("* exam review.md").__next__().read_text(encoding="utf-8")
    assert "## Subnetting" in note and "**Correct:** C. 255.255.255.192" in note


def test_ungraded_question_is_never_answered(tmp_path):
    w, client = make(tmp_path, [graded(result="not_graded", yours=(), correct=())])
    assert w._process(b"png", force=True) is False
    assert len(client.calls) == 1                                    # read only: no explain / answer step
    assert "isn't graded" in w.state()["text"] and w.state()["current"] is None


def test_wrong_without_marked_answer_is_treated_as_ungraded(tmp_path):
    w, client = make(tmp_path, [graded(result="incorrect", yours=(), correct=())])
    assert w._process(b"png", force=True) is False and len(client.calls) == 1


def test_correct_answer_is_counted_without_explaining(tmp_path):
    w, client = make(tmp_path, [graded(result="correct", yours=("C",))])
    assert w._process(b"png", force=True) is True
    assert len(client.calls) == 1 and w.state()["correct"] == 1
    assert not (tmp_path / "School").exists()


def test_same_question_twice_is_skipped_unless_forced(tmp_path):
    w, client = make(tmp_path, [graded(), graded(), graded()])
    assert w._process(b"png", force=True) is True
    assert w._process(b"png", force=False) is False and "Same question" in w.state()["text"]
    assert w._process(b"png", force=True) is True                   # Look now reads it again
    assert w.state()["wrong"] == 2


def test_wrong_hint_when_the_explanation_mentions_no_correct_answer(tmp_path):
    w, client = make(tmp_path, [graded(correct=())])
    w._process(b"png", force=True)
    assert "didn't show the correct one" in client.calls[1][1][-1]["content"]


def test_cut_off_and_empty_box(tmp_path):
    w, _ = make(tmp_path, [{**graded(), "status": "cut_off"}, {**graded(), "status": "no_question", "choices": []}])
    assert w._process(b"png", True) is False and "bigger box" in w.state()["text"]
    assert w._process(b"png", True) is False and "No question" in w.state()["text"]


def test_waits_while_away_or_off(tmp_path):
    w, client = make(tmp_path, [graded()], mode="away")
    assert w._process(b"png", True) is False and w.state()["status"] == "paused" and not client.calls
    assert w._force is True                                          # tries again on the next look


def test_bad_json_from_the_model_is_an_error_not_a_crash(tmp_path):
    w, client = make(tmp_path, [])
    client.chat = lambda *a, **k: ChatResult(content="not json", finish_reason="stop")
    assert w._process(b"png", True) is False and w.state()["status"] == "error"


# ---- the loop
def test_loop_looks_only_on_change_and_never_while_busy(tmp_path):
    thumbs = iter([b"\x00" * 64] * 3 + [b"\xff" * 64] * 20)          # first screen, then a new question
    events, busy = [], threading.Event()
    w, client = make(tmp_path, [graded(q="first question?"), graded(q="second question?")], interval=0.02, settle=0.001)

    def grab_thumb(rect):
        assert not busy.is_set(), "looked at the screen while still explaining"
        events.append("look")
        return next(thumbs)
    w._grab_thumb = grab_thumb
    real = w._process

    def process(png, force):
        busy.set()
        try:
            events.append("process")
            return real(png, force)
        finally:
            busy.clear()
            if events.count("process") == 2:
                w._stop.set()
    w._process = process
    w.set_rect([0, 0, 400, 300], start=True)
    w._thread.join(timeout=5)
    assert events.count("process") == 2 and w.state()["wrong"] == 2
    assert json.loads((tmp_path / "s.json").read_text())["rect"] == [0, 0, 400, 300]


def test_stops_by_itself_after_quiet_minutes(tmp_path):
    now = [0.0]
    w, _ = make(tmp_path, [graded()], interval=0.01, settle=0.001, idle_stop=600, clock=lambda: now[0])
    w._grab_thumb = lambda r: (now.__setitem__(0, now[0] + 400), b"\x00" * 64)[1]
    w.set_rect([0, 0, 100, 100])
    w._thread.join(timeout=5)
    s = w.state()
    assert not s["running"] and s["status"] == "stopped" and "by itself" in s["text"]


def test_start_needs_a_box(tmp_path):
    w, _ = make(tmp_path, [])
    assert w.start()["ok"] is False


# ---- ticker
def test_review_slide_shows_the_last_result():
    r = {"status": "watching", "text": "Explained in 9 s", "running": True, "correct": 3, "wrong": 1,
         "current": {"result": "incorrect", "topic": "Subnetting", "your_labels": ["B"], "correct_labels": ["C"],
                     "question": "Q?", "yours": "B. x", "correct": "C. y", "explanation": "Because."}}
    sl = sn.review_slide(r)
    assert sl["tag"] == "REVIEW" and sl["level"] == "alert"
    assert "✗ Subnetting" in sl["text"] and "you B" in sl["text"] and "correct C" in sl["text"] and "3 ✓ 1 ✗" in sl["text"]
    assert "Because." in sl["detail"]
    assert sn.review_slide({**r, "running": False})["level"] == "info"


def test_rotation_takes_turns_between_two_alert_slides():
    slides = [{"level": "info"}, {"level": "info"}, {"level": "alert"}, {"level": "info"}, {"level": "alert"}]
    assert sn.rotation(slides) == [2, 0, 4, 1, 2, 3]
