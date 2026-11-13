import os
from pathlib import Path

import pytest

from sol_control_hud.chains import file_tools, fix_tools, ingest_tools
from sol_control_hud.chains.router import ModelUnavailable
from sol_control_hud.chains.runner import ToolError


@pytest.fixture(autouse=True)
def _no_live_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(fix_tools, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr("sol_control_hud.chains.gpulock.LOCK_PATH", tmp_path / "gpu.lock")


@pytest.fixture
def policy(tmp_path):
    return file_tools.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path / "notes")])


class Ctx:
    """A ToolContext stand-in: cancelled after `cancel_after` checks (None: never)."""
    def __init__(self, cancel_after=None):
        self.run_id, self.step, self.notes, self.checks, self.cancel_after = 7, "s", [], 0, cancel_after

    def note(self, message, **data):
        self.notes.append(message)

    def cancelled(self):
        self.checks += 1
        return self.cancel_after is not None and self.checks > self.cancel_after


def test_inbox_pending_finds_unprocessed_files(tmp_path):
    folder = tmp_path / "video"
    folder.mkdir()
    (folder / "vid1.mp4").write_text("dummy", encoding="utf-8")
    (folder / "vid2.mkv").write_text("dummy", encoding="utf-8")
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")

    ledger_path = tmp_path / "ledger.json"
    res = ingest_tools.inbox_pending({"folder": str(folder), "kind": "video", "ledger_file": str(ledger_path)})
    assert res["count"] == 2
    assert any("vid1.mp4" in p for p in res["pending"])
    assert any("vid2.mkv" in p for p in res["pending"])

    # Mark vid1 processed in ledger
    ingest_tools.record_processed(folder / "vid1.mp4", "note.md", ledger_path=ledger_path)
    res2 = ingest_tools.inbox_pending({"inbox": str(folder), "kind": "video", "ledger_file": str(ledger_path)})
    assert res2["count"] == 1
    assert "vid2.mkv" in res2["pending"][0]


def test_verify_monotonic_timestamps():
    valid = [{"start": 0.0, "end": 2.5}, {"start": 2.5, "end": 5.0}, {"start": 5.0, "end": 10.0}]
    ingest_tools._verify_monotonic_timestamps(valid)

    invalid = [{"start": 0.0, "end": 2.5}, {"start": 5.0, "end": 10.0}, {"start": 4.0, "end": 8.0}]
    with pytest.raises(ToolError, match="timestamps non-monotonic"):
        ingest_tools._verify_monotonic_timestamps(invalid)


def test_ingest_video(tmp_path, policy):
    vid = tmp_path / "interview.mp4"
    vid.write_text("fake video data", encoding="utf-8")
    out_dir = tmp_path / "notes" / "Transcripts"
    ledger_path = tmp_path / "ledger.json"

    def fake_transcribe(args, ctx):
        return {
            "source": args["source"],
            "title": "Interview with Researcher",
            "duration": 125,
            "full_text": "Welcome to the research discussion. We found substantial evidence.",
            "timestamped_text": "[00:00:00] Welcome to the research discussion.\n[00:01:05] We found substantial evidence.",
            "segments": [{"start": 0.0, "text": "Welcome"}, {"start": 65.0, "text": "Evidence"}],
        }

    def fake_propose(messages):
        return {
            "overview": "A discussion on research evidence and findings.",
            "chapters": [{"at": "00:00", "title": "Introduction", "summary": "Welcome remarks"}],
            "key_points": [{"point": "Substantial evidence located", "at": "01:05"}],
            "action_items": ["Review final findings document"],
        }

    res = ingest_tools.ingest_video(
        {"path": str(vid), "notes_folder": str(out_dir), "ledger_file": str(ledger_path)},
        ctx=Ctx(),
        transcribe_fn=fake_transcribe,
        propose=fake_propose,
        policy=policy,
    )
    assert res["ok"] is True
    note = Path(res["note"])
    assert note.exists()
    assert not list(out_dir.glob("*.tmp"))
    content = note.read_text(encoding="utf-8")
    assert "Interview with Researcher" in content
    assert "## Chapters" in content
    assert "## Key points" in content
    assert "## Action items" in content
    assert "Review final findings document" in content

    # Assert ledger updated
    assert ingest_tools.is_processed(vid, ledger_path=ledger_path)


def test_ingest_pdf(tmp_path, policy):
    pdf = tmp_path / "physics_lecture.pdf"
    pdf.write_text("fake pdf data", encoding="utf-8")
    out_dir = tmp_path / "notes" / "School"
    ledger_path = tmp_path / "ledger.json"

    def fake_read(p):
        return {
            "path": str(p),
            "text": "This lecture covers Newton's three laws of motion and gravitational acceleration in classical mechanics in detail with problem sets." * 3,
        }

    def fake_propose(messages):
        return {
            "title": "Classical Mechanics Lecture",
            "summary": "Overview of Newton's laws and gravitational acceleration.",
            "outline": [
                {"section": "Laws of Motion", "bullets": ["First Law: Inertia", "Second Law: F=ma"]},
            ],
            "key_terms": [{"term": "Inertia", "definition": "Tendency of an object to resist changes in velocity"}],
            "study_questions": ["Explain how F=ma relates to acceleration"],
        }

    res = ingest_tools.ingest_pdf(
        {"path": str(pdf), "notes_folder": str(out_dir), "ledger_file": str(ledger_path)},
        ctx=Ctx(),
        read_fn=fake_read,
        propose=fake_propose,
        policy=policy,
    )
    assert res["ok"] is True
    note = Path(res["note"])
    assert note.exists()
    content = note.read_text(encoding="utf-8")
    assert "Classical Mechanics Lecture" in content
    assert "### Laws of Motion" in content
    assert "Inertia" in content
    assert "Explain how F=ma relates to acceleration" in content

    assert ingest_tools.is_processed(pdf, ledger_path=ledger_path)


def test_ingest_pdf_too_short_fails(tmp_path, policy):
    pdf = tmp_path / "empty.pdf"
    pdf.write_text("tiny", encoding="utf-8")
    out_dir = tmp_path / "notes" / "School"
    ledger_path = tmp_path / "ledger.json"

    def fake_read(p):
        return {"path": str(p), "text": "Too short"}

    with pytest.raises(ToolError, match="too short"):
        ingest_tools.ingest_pdf(
            {"path": str(pdf), "notes_folder": str(out_dir), "ledger_file": str(ledger_path)},
            ctx=Ctx(),
            read_fn=fake_read,
            policy=policy,
        )


# ------------------------------------------------------------------ batch behaviour
GOOD_PDF = {"title": "Doc", "summary": "s", "outline": [{"section": "S", "bullets": ["b"]}]}
LONG = "x" * 80


def _transcribe_ok(args, ctx):
    return {"title": Path(args["source"]).stem, "duration": 5, "full_text": "hello there",
            "timestamped_text": "[00:00:00] hello there", "segments": [{"start": 0.0}]}


def _setup(tmp_path, names):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    files = []
    for i, n in enumerate(names):
        f = inbox / n
        f.write_text("data" + str(i), encoding="utf-8")
        os.utime(f, (1_700_000_000 + i * 10, 1_700_000_000 + i * 10))
        files.append(f)
    base = {"inbox": str(inbox), "notes_folder": str(tmp_path / "notes" / "out"),
            "ledger_file": str(tmp_path / "ledger.json"), "max_items": "5"}
    return files, base


def _read_bad(p):
    return {"text": "short"}


def _read_good(p):
    return {"text": LONG}


def test_failure_recorded_retried_then_given_up(tmp_path, policy):
    (bad,), args = _setup(tmp_path, ["bad.pdf"])
    ledger = Path(args["ledger_file"])
    for attempt in (1, 2):
        res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_bad, policy=policy)
        assert res["count"] == 0 and res["failed_count"] == 1
        assert res["failed"][0]["attempts"] == attempt and "too short" in res["failed"][0]["reason"]
        entry = ingest_tools.read_ledger(ledger)[ingest_tools.file_key(bad)]
        assert entry["status"] == "failed" and entry["attempts"] == attempt
        assert entry["size"] == bad.stat().st_size and "reason" in entry
    assert res["given_up"] == 1  # the second failure gives up
    # now skipped: the reader is not even called
    called = []
    res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=lambda p: called.append(p) or _read_bad(p), policy=policy)
    assert called == [] and res["failed"] == [] and res["given_up"] == 1
    pend = ingest_tools.inbox_pending({**args, "kind": "pdf"})
    assert pend["pending"] == [] and pend["given_up_files"] == [str(bad)]


def test_changed_file_is_pending_again(tmp_path, policy):
    (bad,), args = _setup(tmp_path, ["bad.pdf"])
    for _ in range(2):
        ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_bad, policy=policy)
    assert ingest_tools.inbox_pending({**args, "kind": "pdf"})["count"] == 0
    os.utime(bad, (1_800_000_000, 1_800_000_000))  # touched: same size, new mtime
    assert ingest_tools.inbox_pending({**args, "kind": "pdf"})["pending"] == [str(bad)]
    os.utime(bad, (1_700_000_000, 1_700_000_000))  # back to the failed mtime: given up again
    assert ingest_tools.inbox_pending({**args, "kind": "pdf"})["count"] == 0
    bad.write_text("different size content", encoding="utf-8")  # size changed
    os.utime(bad, (1_700_000_000, 1_700_000_000))
    assert ingest_tools.inbox_pending({**args, "kind": "pdf"})["count"] == 1
    res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, propose=lambda m: GOOD_PDF, policy=policy)
    assert res["count"] == 1 and res["failed"] == []


def test_schema_failure_and_good_file_in_one_batch(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.pdf", "b.pdf"])
    answers = iter([{"nonsense": 1}, GOOD_PDF])
    res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, propose=lambda m: next(answers), policy=policy)
    assert res["count"] == 1 and res["failed_count"] == 1
    assert res["failed"][0]["path"] == str(files[0]) and "ValidationError" in res["failed"][0]["reason"]


def test_failed_files_do_not_use_up_max_items(tmp_path):
    files, args = _setup(tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    for _ in range(2):
        ingest_tools.record_failure(files[0], "boom", ledger_path=Path(args["ledger_file"]))
    res = ingest_tools.inbox_pending({**args, "kind": "pdf", "max_items": 2})
    assert res["pending"] == [str(files[1]), str(files[2])] and res["given_up"] == 1


def test_batch_args_cannot_redirect_file_or_output(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.pdf"])
    other = tmp_path / "other.pdf"
    other.write_text("x", encoding="utf-8")
    seen = []

    def read(p):
        seen.append(Path(p))
        return {"text": LONG}

    evil = {**args, "path": str(other), "folder": str(tmp_path / "elsewhere")}
    res = ingest_tools.ingest_inbox_pdfs(evil, Ctx(), read_fn=read, propose=lambda m: GOOD_PDF, policy=policy)
    assert seen == [files[0]]  # `path` did not override the inbox file
    assert Path(res["processed"][0]["note"]).parent == Path(args["notes_folder"])  # `folder` is not the output
    assert not (tmp_path / "elsewhere").exists()


def test_write_outside_policy_is_refused(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.pdf"])
    args["notes_folder"] = str(tmp_path / "not-allowed")
    res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, propose=lambda m: GOOD_PDF, policy=policy)
    assert res["count"] == 0 and "not allowed to write" in res["failed"][0]["reason"]
    assert not (tmp_path / "not-allowed").exists()
    with pytest.raises(file_tools.FileToolError):
        ingest_tools.ingest_pdf({"path": str(files[0]), "notes_folder": args["notes_folder"]}, Ctx(),
                                read_fn=_read_good, propose=lambda m: GOOD_PDF, policy=policy)


def test_default_policy_allows_the_real_notebook_folders():
    for d in (ingest_tools.TRANSCRIPTS_DIR, ingest_tools.SCHOOL_DIR):
        file_tools.check_write(d / "2026-01-01 Some title.md")


def test_abort_before_first_file_leaves_everything_pending(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.pdf", "b.pdf"])
    with pytest.raises(ModelUnavailable):
        ingest_tools.ingest_inbox_pdfs(args, Ctx(cancel_after=0), read_fn=_read_good, propose=lambda m: GOOD_PDF,
                                       policy=policy)
    assert ingest_tools.read_ledger(Path(args["ledger_file"])) == {}
    assert ingest_tools.inbox_pending({**args, "kind": "pdf"})["count"] == 2


def test_abort_between_files_keeps_done_ones_and_rest_pending(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    state = {"n": 0}

    def propose(m):
        state["n"] += 1
        return GOOD_PDF

    with pytest.raises(ModelUnavailable):
        ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, propose=propose, policy=policy,
                                       abort=lambda: state["n"] >= 1)
    ledger = ingest_tools.read_ledger(Path(args["ledger_file"]))
    assert [e["status"] for e in ledger.values()] == ["done"]  # a.pdf only; nothing failed
    assert ingest_tools.inbox_pending({**args, "kind": "pdf"})["pending"] == [str(files[1]), str(files[2])]


def test_abort_after_transcribe_skips_the_model_and_is_not_a_failure(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.mp4"])
    state = {"transcribed": False}

    def transcribe(a, c):
        state["transcribed"] = True
        return _transcribe_ok(a, c)

    def propose(m):
        raise AssertionError("model must not be called")

    with pytest.raises(ModelUnavailable):
        ingest_tools.ingest_inbox_videos(args, Ctx(), transcribe_fn=transcribe, propose=propose, policy=policy,
                                         abort=lambda: state["transcribed"])
    assert state["transcribed"]
    assert ingest_tools.read_ledger(Path(args["ledger_file"])) == {}
    assert ingest_tools.inbox_pending({**args, "kind": "video"})["count"] == 1


def test_lane_closed_flag_aborts_and_stale_flag_is_cleared(tmp_path, policy):
    files, args = _setup(tmp_path, ["a.pdf"])
    fix_tools.request_abort()  # stale flag from an earlier session: the batch clears it and runs
    res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, propose=lambda m: GOOD_PDF, policy=policy)
    assert res["count"] == 1
    fix_tools.request_abort()  # a flag raised while a file is being ingested aborts that file
    with pytest.raises(ingest_tools.Interrupted):
        ingest_tools.ingest_pdf({"path": str(files[0]), "notes_folder": args["notes_folder"]}, Ctx(),
                                read_fn=_read_good, propose=lambda m: GOOD_PDF, policy=policy)


def test_model_call_goes_through_gpu_lock_and_router(tmp_path, monkeypatch, policy):
    from contextlib import contextmanager
    from sol_control_hud.chains import gpulock, router
    events = []

    @contextmanager
    def fake_lock(what, **kw):
        events.append(("lock", kw.get("should_stop")))
        yield
        events.append(("unlock",))

    class FakeClient:
        def chat(self, model, messages, **kw):
            events.append(("chat", kw.get("schema") is ingest_tools.PDF_OUTLINE_SCHEMA))
            return type("R", (), {"content": '{"title": "T", "summary": "s", "outline": []}'})()

    monkeypatch.setattr(gpulock, "gpu_lock", fake_lock)
    monkeypatch.setattr(router, "RouterClient", FakeClient)
    files, args = _setup(tmp_path, ["a.pdf"])
    res = ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, policy=policy)
    assert res["count"] == 1
    assert [e[0] for e in events] == ["lock", "chat", "unlock"]
    assert events[0][1] is not None and events[1][1] is True  # should_stop wired in; schema passed


def test_router_unavailable_is_an_interruption_not_a_failure(tmp_path, monkeypatch, policy):
    from sol_control_hud.chains import router
    files, args = _setup(tmp_path, ["a.pdf"])

    class Down:
        def chat(self, *a, **k):
            raise ModelUnavailable("switching")

    monkeypatch.setattr(router, "RouterClient", Down)
    with pytest.raises(ModelUnavailable):
        ingest_tools.ingest_inbox_pdfs(args, Ctx(), read_fn=_read_good, policy=policy)
    assert ingest_tools.read_ledger(Path(args["ledger_file"])) == {}


def test_bad_max_items_and_vanishing_files(tmp_path, monkeypatch):
    files, args = _setup(tmp_path, ["a.pdf", "b.pdf"])
    for bad in ("many", "", None, "0", "-3"):
        res = ingest_tools.inbox_pending({**args, "kind": "pdf", "max_items": bad})
        assert 1 <= res["count"] <= 2
    gone = files[0]
    real_stat = Path.stat

    def flaky(self, *a, **k):
        if self == gone:
            raise FileNotFoundError(str(self))
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky)
    res = ingest_tools.inbox_pending({**args, "kind": "pdf"})
    assert res["pending"] == [str(files[1])]
