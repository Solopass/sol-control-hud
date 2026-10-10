"""Tests for Solang YouTube Takeout parser and tool steps (15,000+ link scale)."""
import json
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sol_control_hud.chains import chain_note as cn
from sol_control_hud.chains import file_tools as ft
from sol_control_hud.chains import solang_tools as st
from sol_control_hud.chains.solang_tools import TakeoutParser, StateManager, is_likely_music


def test_is_likely_music_heuristics():
    # Strong music indicators
    assert is_likely_music("aespa 에스파 'Supernova' MV") is True
    assert is_likely_music("IVE 아이브 'HEYA' Official Music Video") is True
    assert is_likely_music("NewJeans - Hype Boy (Official Audio)") is True
    assert is_likely_music("LE SSERAFIM - Perfect Night [Color Coded Lyrics]") is True
    assert is_likely_music("IU - Love wins all (Live Performance)") is True
    assert is_likely_music("TWICE - FANCY", channel="JYP Entertainment") is True
    assert is_likely_music("Some Track", channel="SMTOWN") is True
    assert is_likely_music("Radiohead - Creep") is True

    # Obvious non-music items in watch history
    assert is_likely_music("Elden Ring Full Walkthrough Part 1") is False
    assert is_likely_music("How to build a PC - Complete Guide 2026") is False
    assert is_likely_music("MKBHD Review: iPhone 18 Pro Max") is False
    assert is_likely_music("Lex Fridman Podcast #450") is False


def test_parse_html_scale_15000_entries(tmp_path):
    """Verify parsing a mock Google Takeout watch-history.html with 15,000 entries.
    Ensures memory efficiency, speed (< 1.5s), deduplication, and music filtering.
    """
    html_file = tmp_path / "watch-history.html"
    
    # Generate 15,000 HTML entries with 500 unique songs repeated 20 times,
    # plus 5,000 non-music videos
    lines = ['<html><body><div class="mdl-grid">']
    
    unique_music_ids = [f"vid_{i:07d}" for i in range(500)]
    unique_nonmusic_ids = [f"non_{i:07d}" for i in range(500)]

    # Repeat music tracks (mimics real watch history listening patterns)
    for rep in range(20):
        for idx, vid in enumerate(unique_music_ids):
            title = f"Artist {idx % 50} - Hit Song {idx} (Official MV)"
            lines.append(
                f'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
                f'Watched <a href="https://www.youtube.com/watch?v={vid}">{title}</a><br>'
                f'<a href="https://www.youtube.com/channel/UC123">Music Channel</a><br>'
                f'Oct 10, 2026, 12:00:00 PM EDT</div>'
            )

    # Add 5,000 non-music videos
    for rep in range(10):
        for idx, vid in enumerate(unique_nonmusic_ids):
            title = f"Gaming Let's Play Episode {idx} - Full Walkthrough Gameplay"
            lines.append(
                f'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
                f'Watched <a href="https://www.youtube.com/watch?v={vid}">{title}</a><br>'
                f'<a href="https://www.youtube.com/channel/UC999">Gaming Channel</a><br>'
                f'Oct 10, 2026, 1:00:00 PM EDT</div>'
            )

    lines.append('</div></body></html>')
    html_content = "\n".join(lines)
    html_file.write_text(html_content, encoding="utf-8")

    # Time parsing and deduplication
    t0 = time.monotonic()
    parsed_items = TakeoutParser.parse_file(html_file, filter_music=True)
    duration = time.monotonic() - t0

    # Must be blazing fast (< 1.5 seconds)
    assert duration < 1.5, f"Parsing took too long: {duration:.2f}s"
    
    # Must have deduplicated to exactly 500 unique music songs
    assert len(parsed_items) == 500
    assert parsed_items[0]["videoId"] == unique_music_ids[0]
    assert "Official MV" in parsed_items[0]["title"]
    assert parsed_items[0]["url"] == f"https://www.youtube.com/watch?v={unique_music_ids[0]}"


def test_parse_json_format(tmp_path):
    json_file = tmp_path / "watch-history.json"
    data = [
        {
            "header": "YouTube",
            "title": "Watched aespa 에스파 'Supernova' MV",
            "titleUrl": "https://www.youtube.com/watch?v=s1tAYM-Q4vQ",
            "subtitles": [{"name": "SMTOWN"}],
            "time": "2026-10-10T12:00:00Z"
        },
        {
            "header": "YouTube",
            "title": "Watched Python Async Tutorial in 20 Minutes",
            "titleUrl": "https://www.youtube.com/watch?v=pythontut01",
            "subtitles": [{"name": "Tech Guide"}],
            "time": "2026-10-10T11:00:00Z"
        }
    ]
    json_file.write_text(json.dumps(data), encoding="utf-8")

    items = TakeoutParser.parse_file(json_file, filter_music=True)
    assert len(items) == 1
    assert items[0]["videoId"] == "s1tAYM-Q4vQ"
    assert items[0]["title"] == "aespa 에스파 'Supernova' MV"


def test_parse_csv_format(tmp_path):
    csv_file = tmp_path / "Liked videos.csv"
    csv_content = """Video ID,Playlist Video Creation Timestamp,Title
s1tAYM-Q4vQ,2026-10-10T12:00:00Z,Supernova MV
non_valid_123,2026-10-10T11:00:00Z,Too Long ID
pythontut01,2026-10-10T10:00:00Z,Python Tutorial Full Guide
dQw4w9WgXcQ,2026-10-10T09:00:00Z,Rick Astley - Never Gonna Give You Up (Official Music Video)
"""
    csv_file.write_text(csv_content, encoding="utf-8")

    items = TakeoutParser.parse_file(csv_file, filter_music=True)
    assert len(items) == 2
    vids = {it["videoId"] for it in items}
    assert vids == {"s1tAYM-Q4vQ", "dQw4w9WgXcQ"}


def test_state_manager_checkpointing(tmp_path):
    state_file = tmp_path / "state.json"
    state = StateManager.load(state_file)
    assert state["offset"] == 0
    assert state["processed"] == {}

    state["source_file"] = "test.html"
    state["offset"] = 50
    state["processed"]["vid_1"] = {"status": "ingested", "title": "Song 1"}
    StateManager.save(state_file, state)

    reloaded = StateManager.load(state_file)
    assert reloaded["offset"] == 50
    assert "vid_1" in reloaded["processed"]
    assert reloaded["processed"]["vid_1"]["status"] == "ingested"


def test_extract_takeout_links_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "POLICY", ft.Policy(read_roots=[str(tmp_path)], write_roots=[str(tmp_path)]))
    
    # Create test text file with 30 IDs
    test_file = tmp_path / "links.txt"
    lines = [f"https://www.youtube.com/watch?v=vid_{i:07d}" for i in range(30)]
    test_file.write_text("\n".join(lines), encoding="utf-8")
    
    state_file = tmp_path / "test_state.json"

    # Batch 1: max 10
    res1 = st.extract_takeout_links({
        "file": str(test_file),
        "max_items": 10,
        "filter_music": False,
        "state_file": str(state_file)
    })
    assert res1["count"] == 10
    assert res1["offset"] == 0
    assert res1["next_offset"] == 10
    assert res1["remaining_unprocessed"] == 20

    # Batch 2: next 15 (auto resumes)
    res2 = st.extract_takeout_links({
        "file": str(test_file),
        "max_items": 15,
        "filter_music": False,
        "state_file": str(state_file)
    })
    assert res2["count"] == 15
    assert res2["offset"] == 10
    assert res2["next_offset"] == 25
    assert res2["remaining_unprocessed"] == 5


def test_chain_note_with_tools_compilation(tmp_path, monkeypatch):
    """Tests that a markdown chain note with tool steps compiles to a valid workflow."""
    chains_dir = tmp_path / "Chains"
    chains_dir.mkdir()
    monkeypatch.setattr(cn, "CHAINS_DIR", chains_dir)
    monkeypatch.setattr(cn, "VAULT", tmp_path)

    note_text = """---
type: chain
status: queued
model: sol-fast
inputs:
  export_file: D:\\Output\\watch-history.html
  batch_size: 20
  state_file: D:\\Output\\chains\\solang_takeout_state.json
save_to: 1Notebook/Digests/Solang Ingest - {{today}}.md
---
Takeout chain.

## Extract Links
tool: extract_takeout_links
file: {{export_file}}
max_items: {{batch_size}}
state_file: {{state_file}}
filter_music: true

## Ingest to Solang
tool: ingest_solang_tracks
items: {{Extract Links.items}}
state_file: {{state_file}}
auto_save: true

## Summary Report
check: at most 150 words
Here are the songs ingested into Solang:
{{Ingest to Solang}}

Write a concise digest of the newly imported Karaoke Studio projects.
"""
    note_p = chains_dir / "Ingest YouTube Takeout to Solang.md"
    note_p.write_text(note_text, encoding="utf-8")

    chain = cn.load_chain(note_p)
    assert len(chain.steps) == 3
    assert chain.steps[0].settings["tool"] == "extract_takeout_links"
    assert chain.steps[1].settings["tool"] == "ingest_solang_tracks"

    wf = cn.to_workflow(chain)
    step_ids = [s["id"] for s in wf["steps"]]
    assert step_ids == ["extract_links", "ingest_to_solang", "summary_report"]
    
    assert wf["steps"][0]["tool"] == "extract_takeout_links"
    assert wf["steps"][0]["args"]["file"] == "{{ inputs.export_file }}"
    assert wf["steps"][0]["args"]["max_items"] == "{{ inputs.batch_size }}"

    assert wf["steps"][1]["tool"] == "ingest_solang_tracks"
    assert wf["steps"][1]["args"]["items"] == "{{ steps.extract_links.output.items }}"

    assert wf["steps"][2]["model"] == "sol-fast"
    assert "{{ steps.ingest_to_solang.output }}" in wf["steps"][2]["prompt"]

    # Validate with check_chain
    tools = {**cn.TOOLS, **st.TOOLS}
    registry = {"sol-fast": {"num_ctx": 8192}, "sol-long": {"num_ctx": 65536}}
    problems = cn.check_chain(note_p, tools, registry)
    assert problems == []


def test_generate_karaoke_library_note(tmp_path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    p1 = {
        "id": "proj_1",
        "title": "Supernova",
        "artist": "aespa",
        "duration": 185,
        "lyrics": [{"line": 1}, {"line": 2}],
        "mediaSources": [
            {"id": "src_1", "name": "Official MV", "videoId": "v1"},
            {"id": "src_2", "name": "Dance Practice", "videoId": "v2"}
        ]
    }
    (projects_dir / "proj_1.kstudio").write_text(json.dumps(p1), encoding="utf-8")

    out_note = tmp_path / "1Notebook" / "Karaoke Library.md"
    content = st.generate_karaoke_library_note(output_path=out_note, projects_dir=projects_dir)

    assert "Supernova" in content
    assert "aespa" in content
    assert "Dance Practice" in content
    assert "http://127.0.0.1:3000/?project=proj_1" in content
    assert out_note.exists()


def test_queue_status_and_pause(tmp_path):
    state_file = tmp_path / "state.json"
    state = {
        "source_file": "watch.html",
        "total_found": 100,
        "offset": 45,
        "paused": False,
        "processed": {
            "v1": {"status": "ingested", "title": "Song 1"},
            "v2": {"status": "clustered_alternate", "title": "Song 1 (Live)"}
        }
    }
    st.StateManager.save(state_file, state)

    status = st.get_queue_status(state_file)
    assert status["total_found"] == 100
    assert status["offset"] == 45
    assert status["percent"] == 45.0
    assert status["ingested_count"] == 1
    assert status["clustered_count"] == 1
    assert status["paused"] is False

    st.set_queue_paused(True, state_file)
    status2 = st.get_queue_status(state_file)
    assert status2["paused"] is True

