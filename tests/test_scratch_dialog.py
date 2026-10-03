"""Unit tests for Quick Scratch / Note Capture Dialog."""
from pathlib import Path
import tkinter as tk

import pytest

from sol_control_hud.views import scratch_dialog, ticker
from sol_control_hud.views.scratch_dialog import QuickScratchDialog, append_scratch_note


def test_append_scratch_note_empty(tmp_path):
    ok, path, msg = append_scratch_note("   ", vault_dir=tmp_path)
    assert not ok
    assert "Empty note text" in msg


def test_append_scratch_note_daily_new_and_existing(tmp_path):
    vault_dir = tmp_path / "Polymatica Vault"
    # 1. New note creation
    ok, note_path, msg = append_scratch_note(
        "First thought of the day",
        target="daily",
        include_timestamp=True,
        vault_dir=vault_dir,
        timestamp_override="01:15",
    )
    assert ok
    assert note_path.exists()
    assert "- 01:15 First thought of the day\n" in note_path.read_text(encoding="utf-8")

    # 2. Append to existing note
    ok2, note_path2, msg2 = append_scratch_note(
        "Second thought later",
        target="daily",
        include_timestamp=False,
        vault_dir=vault_dir,
    )
    assert ok2
    content = note_path.read_text(encoding="utf-8")
    assert "First thought of the day" in content
    assert "Second thought later" in content


def test_append_scratch_note_private(tmp_path):
    obvlt_dir = tmp_path / "OBVLT"
    ok, scratch_path, msg = append_scratch_note(
        "Secret project idea",
        target="private",
        include_timestamp=True,
        obvlt_dir=obvlt_dir,
        timestamp_override="02:30",
    )
    assert ok
    assert scratch_path.exists()
    content = scratch_path.read_text(encoding="utf-8")
    assert "# 📓 Private Quick Scratch" in content
    assert "Secret project idea" in content


def test_quick_scratch_dialog_ui(tmp_path):
    vault_dir = tmp_path / "Polymatica Vault"
    saved = []

    root = tk.Tk()
    root.withdraw()
    try:
        theme = ticker.THEMES["cyber-cyan"]
        dlg = QuickScratchDialog(
            parent=root,
            theme=theme,
            on_saved=lambda p, text: saved.append((p, text)),
            vault_dir=vault_dir,
        )
        assert dlg.win.winfo_exists()

        # Type content and save
        dlg.text_input.insert("1.0", "Testing quick capture UI")
        dlg._on_save()

        assert len(saved) == 1
        assert saved[0][1] == "Testing quick capture UI"
    finally:
        root.destroy()
