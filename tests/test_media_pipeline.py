"""Tool steps + the transcript-note workflow, with media-api and the model faked."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from sol_control_hud.chains import media_tools
from sol_control_hud.chains.runner import Runner, ToolError
from sol_control_hud.chains.store import Store
from sol_control_hud.chains.workflow import WorkflowError, load, parse, resolve_args

from test_pipelines import FakeClient

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "transcript-note.yaml"

TRANSCRIPTION = {
    "source": r"D:\rec\standup.wav", "title": "Standup", "uploader": None, "url": None,
    "engine": "parakeet", "stt_model": "parakeet-tdt-0.6b-v3-q8_0", "language": "auto", "duration": 125.4,
    "segments": [{"id": 0, "start": 0.0, "end": 4.0, "text": "Welcome."}, {"id": 1, "start": 65.2, "end": 70.0, "text": "Mei owns the review."}],
    "full_text": "Welcome. Mei owns the review.",
    "timestamped_text": "[00:00:00] Welcome.\n[00:01:05] Mei owns the review.",
    "files": {},
}
SUMMARY = {
    "overview": "A short standup where the team assigned the security review to Mei.",
    "key_points": [{"point": "Mei owns the security review", "at": "00:01:05"}],
    "action_items": [{"task": "Run the security review", "owner": "Mei", "due": "none"}],
    "decisions": ["Mei owns the review"], "dates": [], "people": ["Mei"],
}
PART_NOTES = {
    "summary": "The team assigns the security review to Mei.",
    "key_points": [{"point": "Mei owns the security review", "at": "00:01:05"}],
    "action_items": [{"task": "Run the security review", "owner": "Mei", "due": "none"}],
    "decisions": ["Mei owns the review"], "dates": [], "people": ["Mei"],
}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "runs.sqlite")


def test_tool_args_pass_structures_through_and_render_text():
    ctx = {"inputs": {"title": "T"}, "steps": {"a": {"output": {"x": [1, 2]}}}}
    out = resolve_args({"whole": "{{ steps.a.output }}", "text": "Title: {{ inputs.title }}", "n": 3}, ctx)
    assert out == {"whole": {"x": [1, 2]}, "text": "Title: T", "n": 3}


def test_step_needs_tool_or_model():
    with pytest.raises(WorkflowError, match="pick one"):
        parse({"name": "x", "steps": [{"id": "a", "tool": "t", "model": "m", "prompt": "p"}]})
    with pytest.raises(WorkflowError, match="missing 'model'"):
        parse({"name": "x", "steps": [{"id": "a", "prompt": "p"}]})
    with pytest.raises(WorkflowError, match="undeclared input"):
        parse({"name": "x", "inputs": [], "steps": [{"id": "a", "tool": "t", "args": {"s": "{{ inputs.nope }}"}}]})


def test_transcript_note_workflow_end_to_end(store, tmp_path):
    calls = {}

    def fake_transcribe(args, ctx):
        calls["transcribe"] = args
        ctx.note("Transcribing with parakeet...", percent=50)
        return TRANSCRIPTION

    def write_note(args, ctx):
        return media_tools.write_note({**args, "folder": str(tmp_path / "Transcripts")}, ctx)

    client = FakeClient({"sol-fast": [PART_NOTES, SUMMARY]})          # one part (a short recording), then the merge
    runner = Runner(client, store, tools={"transcribe": fake_transcribe, "write_note": write_note,
                                          "split_transcript": media_tools.split_transcript})
    run_id = runner.run(load(WORKFLOW), {"source": r"D:\rec\standup.wav", "title": "", "engine": "", "language": ""})

    run = store.run(run_id)
    assert run["status"] == "succeeded", run["error"]
    assert calls["transcribe"]["source"] == r"D:\rec\standup.wav"
    prompt = client.calls[0][1][0]["content"]                      # the part's notes see the transcript itself
    assert "[00:01:05] Mei owns the review." in prompt and "Title: Standup" in prompt and "Part 1 of 1" in prompt
    merge = client.calls[1][1][0]["content"]                       # the merge sees the part notes, not the transcript
    flat = " ".join(merge.split())                                 # YAML wraps long lines in the rendered notes
    assert "summary: The team assigns the security review to Mei." in flat and "[00:01:05] Mei owns the review." not in merge
    kinds = [(e["step"], e["kind"]) for e in store.events(run_id)]
    assert ("transcribe", "note") in kinds and ("note", "step_succeeded") in kinds

    note = Path(json.loads(run["outputs"])["note"]["path"])
    text = note.read_text(encoding="utf-8")
    assert note.name.endswith(" Standup.md")
    assert 'engine: "parakeet-tdt-0.6b-v3-q8_0"' in text
    assert "- [ ] Run the security review (owner: Mei)" in text  # due "none" is left out
    assert "- Mei owns the security review `00:01:05`" in text
    assert "[00:01:05] Mei owns the review." in text


def test_note_never_overwrites(store, tmp_path):
    class Ctx:
        run_id = 1
        def note(self, *a, **k): pass
    args = {"transcription": TRANSCRIPTION, "summary": SUMMARY, "summary_model": "sol-fast", "folder": str(tmp_path)}
    first = media_tools.write_note(args, Ctx())["path"]
    second = media_tools.write_note(args, Ctx())["path"]
    assert first != second and second.endswith("Standup (2).md")


def test_tool_failure_fails_the_run_with_reason(store):
    def broken(args, ctx):
        raise ToolError("media-api refused the job: HTTP 422 unknown engine")
    runner = Runner(FakeClient({}), store, tools={"transcribe": broken, "write_note": media_tools.write_note,
                                                  "split_transcript": media_tools.split_transcript})
    run_id = runner.run(load(WORKFLOW), {"source": "x", "title": "", "engine": "vosk", "language": ""})
    run = store.run(run_id)
    assert run["status"] == "failed"
    assert "HTTP 422" in run["error"]


def test_unknown_tool_fails_before_anything_runs(store):
    wf = parse({"name": "x", "steps": [{"id": "a", "tool": "nope"}]})
    run_id = Runner(FakeClient({}), store).run(wf, {})
    assert store.run(run_id)["status"] == "failed"
    assert store.events(run_id) == []


def test_wsl_paths_and_timestamps():
    assert media_tools.wsl_to_windows("/mnt/d/Output/Audio/a b.transcript.json") == r"D:\Output\Audio\a b.transcript.json"
    assert media_tools.timestamp(3725.9) == "01:02:05"
    assert media_tools.safe_filename('A: "b"/c?') == "A bc"


# ---- summarized in pieces (2026-10-07: sol-fast holds 8k tokens)
class _Ctx:
    def __init__(self):
        self.notes = []

    def note(self, msg, **data):
        self.notes.append((msg, data))


def _segments(n, words=40):
    return [{"start": i * 10.0, "end": i * 10.0 + 9.5, "text": " ".join(["word"] * words)} for i in range(n)]


def test_split_transcript_short_recording_is_one_part(monkeypatch):
    from sol_control_hud.chains import media_tools
    monkeypatch.setattr(media_tools, "load_registry", lambda: {"sol-fast": {"num_ctx": 8192}})
    out = media_tools.split_transcript({"transcription": {"segments": _segments(3)}, "model": "sol-fast"}, _Ctx())
    assert out["count"] == 1 and out["parts"][0]["n"] == 1 and out["parts"][0]["of"] == 1
    assert out["parts"][0]["start"] == "00:00:00" and out["parts"][0]["end"] == "00:00:29"


def test_split_transcript_cuts_on_segment_boundaries_within_budget(monkeypatch):
    from sol_control_hud.chains import media_tools
    monkeypatch.setattr(media_tools, "load_registry", lambda: {"sol-fast": {"num_ctx": 8192}})
    segs = _segments(400)                                  # ~80k chars: far over one 8k prompt
    out = media_tools.split_transcript({"transcription": {"segments": segs}, "model": "sol-fast"}, _Ctx())
    limit = (8192 - media_tools.PART_RESERVE_TOKENS) * media_tools.CHARS_PER_TOKEN
    assert out["count"] > 1 and all(len(p["text"]) <= limit for p in out["parts"])
    rebuilt = "\n".join(p["text"] for p in out["parts"]).splitlines()
    assert rebuilt == [f"[{media_tools.timestamp(s['start'])}] {s['text']}" for s in segs]     # nothing lost, in order
    assert [p["n"] for p in out["parts"]] == list(range(1, out["count"] + 1))
    # a bigger context gives fewer, bigger parts
    monkeypatch.setattr(media_tools, "load_registry", lambda: {"sol-fast": {"num_ctx": 16384}})
    assert media_tools.split_transcript({"transcription": {"segments": segs}, "model": "sol-fast"}, _Ctx())["count"] < out["count"]


def test_split_transcript_cuts_one_huge_segment(monkeypatch):
    from sol_control_hud.chains import media_tools
    monkeypatch.setattr(media_tools, "load_registry", lambda: {})
    seg = [{"start": 5.0, "end": 600.0, "text": "x" * 5000}]
    out = media_tools.split_transcript({"transcription": {"segments": seg}, "max_part_tokens": 300}, _Ctx())
    assert out["count"] > 1 and all(len(p["text"]) <= 900 for p in out["parts"])
    assert all(p["start"] == "00:00:05" for p in out["parts"])


def test_transcript_workflow_summarizes_in_pieces_on_sol_fast():
    from sol_control_hud.chains.workflow import load
    from sol_control_hud.paths import WORKFLOWS
    wf = load(WORKFLOWS / "transcript-note.yaml")
    steps = {s.id: s for s in wf.steps}
    assert list(steps) == ["transcribe", "parts", "part_notes", "summary", "note"]
    assert steps["part_notes"].for_each["items"] == "{{ steps.parts.output.parts }}"
    assert steps["part_notes"].model == steps["summary"].model == "sol-fast"
