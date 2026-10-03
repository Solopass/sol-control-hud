from __future__ import annotations

import time
from pathlib import Path

from sol_control_hud.data.collectors import notes


def test_format_relative_time():
    now = 1700000000.0
    assert notes.format_relative_time(now - 10, now) == "just now"
    assert notes.format_relative_time(now - 300, now) == "5m ago"
    assert notes.format_relative_time(now - 7200, now) == "2h ago"
    assert "Yesterday" in notes.format_relative_time(now - 90000, now)


def test_discover_vaults():
    vaults = notes.discover_vaults()
    assert isinstance(vaults, dict)
    # On SOL, OBVLT should exist
    assert "OBVLT" in vaults
    assert vaults["OBVLT"].exists()


def test_scan_and_list_notes(tmp_path, monkeypatch):
    # Setup a mock vault
    vdir = tmp_path / "MockVault"
    vdir.mkdir()
    (vdir / "folder").mkdir()
    (vdir / ".obsidian").mkdir()
    (vdir / ".git").mkdir()

    # Note 1 (older)
    n1 = vdir / "Note1.md"
    n1.write_text("# Note 1\nContent", encoding="utf-8")
    t1 = time.time() - 3600
    import os
    os.utime(n1, (t1, t1))

    # Note 2 in subfolder (newer)
    n2 = vdir / "folder" / "Deep Note.md"
    n2.write_text("# Deep\nMore content", encoding="utf-8")
    t2 = time.time() - 60
    os.utime(n2, (t2, t2))

    # Hidden note that must be ignored
    (vdir / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (vdir / ".git" / "ignore.md").write_text("ignore", encoding="utf-8")
    (vdir / ".hidden.md").write_text("hidden", encoding="utf-8")

    monkeypatch.setattr(notes, "discover_vaults", lambda: {"Mock": vdir})

    # Scan
    found = notes.scan_vault_notes("Mock", force=True)
    assert len(found) == 2
    # Newer note first
    assert found[0]["title"] == "Deep Note"
    assert found[0]["file"] == "folder/Deep Note.md"
    assert found[0]["folder"] == "folder"
    assert found[1]["title"] == "Note1"
    assert found[1]["file"] == "Note1.md"
    assert found[1]["folder"] == ""

    # Test list_notes with query
    q_notes = notes.list_notes("Mock", query="deep")
    assert len(q_notes) == 1
    assert q_notes[0]["title"] == "Deep Note"

    # Test read_note_content
    res = notes.read_note_content("Mock", "folder/Deep Note.md")
    assert res["ok"] is True
    assert res["title"] == "Deep Note"
    assert "# Deep" in res["text"]

    # Test directory traversal guard
    bad_res = notes.read_note_content("Mock", "../../etc/passwd")
    assert bad_res["ok"] is False
    assert "outside vault" in bad_res["why"]


def test_open_note_dispatches(monkeypatch, tmp_path):
    vdir = tmp_path / "V"
    vdir.mkdir()
    (vdir / "Test.md").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(notes, "discover_vaults", lambda: {"V": vdir})

    called = []
    monkeypatch.setattr("os.startfile", lambda uri: called.append(uri))

    res = notes.open_note("V", "Test.md")
    assert res["ok"] is True
    assert len(called) == 1
    assert "obsidian://open?vault=V&file=Test.md" in called[0]


def test_save_and_create_note(monkeypatch, tmp_path):
    vdir = tmp_path / "V2"
    vdir.mkdir()
    monkeypatch.setattr(notes, "discover_vaults", lambda: {"V2": vdir})

    # Create note
    c_res = notes.create_note("V2", "NewFolder/MyNote.md", "# Hello World")
    assert c_res["ok"] is True
    assert (vdir / "NewFolder" / "MyNote.md").exists()
    assert (vdir / "NewFolder" / "MyNote.md").read_text(encoding="utf-8") == "# Hello World"

    # Cannot recreate existing note
    c_dup = notes.create_note("V2", "NewFolder/MyNote.md", "# Hello Again")
    assert c_dup["ok"] is False

    # Save note edit
    s_res = notes.save_note_content("V2", "NewFolder/MyNote.md", "# Updated Content")
    assert s_res["ok"] is True
    assert (vdir / "NewFolder" / "MyNote.md").read_text(encoding="utf-8") == "# Updated Content"

    # Open folder
    folder_calls = []
    monkeypatch.setattr("os.startfile", lambda p: folder_calls.append(p))
    f_res = notes.open_folder("V2", "NewFolder/MyNote.md")
    assert f_res["ok"] is True
    assert len(folder_calls) == 1
    assert str(vdir / "NewFolder") == folder_calls[0]

