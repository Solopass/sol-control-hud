import json
from pathlib import Path
import pytest
from sol_control_hud.chains import ingest_tools
from sol_control_hud.chains.runner import ToolContext, ToolError


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
    res2 = ingest_tools.inbox_pending({"folder": str(folder), "kind": "video", "ledger_file": str(ledger_path)})
    assert res2["count"] == 1
    assert "vid2.mkv" in res2["pending"][0]


def test_verify_monotonic_timestamps():
    valid = [{"start": 0.0, "end": 2.5}, {"start": 2.5, "end": 5.0}, {"start": 5.0, "end": 10.0}]
    ingest_tools._verify_monotonic_timestamps(valid)

    invalid = [{"start": 0.0, "end": 2.5}, {"start": 5.0, "end": 10.0}, {"start": 4.0, "end": 8.0}]
    with pytest.raises(ToolError, match="timestamps non-monotonic"):
        ingest_tools._verify_monotonic_timestamps(invalid)


def test_ingest_video(tmp_path):
    vid = tmp_path / "interview.mp4"
    vid.write_text("fake video data", encoding="utf-8")
    out_dir = tmp_path / "Transcripts"
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

    fake_ctx = type("Ctx", (), {"run_id": 101, "step": 1, "note": lambda *a, **k: None, "cancelled": lambda: False})()
    res = ingest_tools.ingest_video(
        {"path": str(vid), "folder": str(out_dir), "ledger_file": str(ledger_path)},
        ctx=fake_ctx,
        transcribe_fn=fake_transcribe,
        propose=fake_propose,
    )
    assert res["ok"] is True
    note = Path(res["note"])
    assert note.exists()
    content = note.read_text(encoding="utf-8")
    assert "Interview with Researcher" in content
    assert "## Chapters" in content
    assert "## Key points" in content
    assert "## Action items" in content
    assert "Review final findings document" in content

    # Assert ledger updated
    assert ingest_tools.is_processed(vid, ledger_path=ledger_path)


def test_ingest_pdf(tmp_path):
    pdf = tmp_path / "physics_lecture.pdf"
    pdf.write_text("fake pdf data", encoding="utf-8")
    out_dir = tmp_path / "School"
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

    fake_ctx = type("Ctx", (), {"run_id": 102, "step": 1, "note": lambda *a, **k: None, "cancelled": lambda: False})()
    res = ingest_tools.ingest_pdf(
        {"path": str(pdf), "folder": str(out_dir), "ledger_file": str(ledger_path)},
        ctx=fake_ctx,
        read_fn=fake_read,
        propose=fake_propose,
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


def test_ingest_pdf_too_short_fails(tmp_path):
    pdf = tmp_path / "empty.pdf"
    pdf.write_text("tiny", encoding="utf-8")
    out_dir = tmp_path / "School"
    ledger_path = tmp_path / "ledger.json"

    def fake_read(p):
        return {"path": str(p), "text": "Too short"}

    fake_ctx = type("Ctx", (), {"run_id": 103, "step": 1, "note": lambda *a, **k: None, "cancelled": lambda: False})()
    with pytest.raises(ToolError, match="too short"):
        ingest_tools.ingest_pdf(
            {"path": str(pdf), "folder": str(out_dir), "ledger_file": str(ledger_path)},
            ctx=fake_ctx,
            read_fn=fake_read,
        )
