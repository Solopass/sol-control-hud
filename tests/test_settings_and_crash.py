"""Unit tests for Settings Dialog, Crash Inspector Dialog, and Ticker Mini/Peek Mode."""
import json
from pathlib import Path
import tkinter as tk
from unittest.mock import MagicMock, patch

import pytest

from sol_control_hud.views import crash_dialog, settings_dialog, ticker
from sol_control_hud.views.crash_dialog import (
    CrashInspectorDialog,
    parse_unacknowledged_crashes,
)
from sol_control_hud.views.settings_dialog import PRESETS, SettingsDialog
from sol_control_hud.data.snapshot import Snapshot


def test_parse_unacknowledged_crashes_missing_file(tmp_path):
    missing = tmp_path / "nonexistent.md"
    assert parse_unacknowledged_crashes(missing) == []


def test_parse_unacknowledged_crashes_extracts_new_rows(tmp_path):
    report_file = tmp_path / "crash-watch.md"
    content = """# Crash Watch Log

| Time | Status | Event | Detail | Resume | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-09-25 08:30 | REVIEWED | Bugcheck 0x116 | VIDEO_TDR_FAILURE | yes | Known wake bug |
| 2026-10-02 04:15 | **NEW** | Event 41 | Kernel-Power | no | Unexpected reboot |
| 2026-10-02 09:00 | NEW | Bugcheck 0x116 | amdkmdag.sys | yes | Driver timeout |
| 2026-10-02 12:00 | IGNORED | Service hang | spoolsv | - | - |
"""
    report_file.write_text(content, encoding="utf-8")
    events = parse_unacknowledged_crashes(report_file)
    assert len(events) == 2
    assert events[0]["time"] == "2026-10-02 04:15"
    assert events[0]["event"] == "Event 41"
    assert events[0]["detail"] == "Kernel-Power"
    assert events[0]["resume"] == "no"

    assert events[1]["time"] == "2026-10-02 09:00"
    assert events[1]["event"] == "Bugcheck 0x116"
    assert events[1]["detail"] == "amdkmdag.sys"
    assert events[1]["resume"] == "yes"


def test_write_crash_ack(tmp_path):
    ack_file = tmp_path / "stability-ack.json"
    data1 = ticker.write_crash_ack(ack_file, now="2026-10-02T12:00:00")
    assert data1["acknowledged_until"] == "2026-10-02T12:00:00"
    assert "2026-10-02 12:00" in data1["note"]

    # Overwriting appends earlier note history
    data2 = ticker.write_crash_ack(ack_file, now="2026-10-02T14:30:00")
    assert data2["acknowledged_until"] == "2026-10-02T14:30:00"
    assert "Earlier (2026-10-02T12:00:00)" in data2["note"]


def test_crash_inspector_dialog_ack_action(monkeypatch, tmp_path):
    fake_ack = tmp_path / "stability-ack.json"
    monkeypatch.setattr(crash_dialog, "ACK_FILE", fake_ack)
    monkeypatch.setattr(ticker, "ACK_FILE", fake_ack)

    theme = ticker.THEMES["cyber-cyan"]
    cleared_called = []

    root = tk.Tk()
    root.withdraw()
    try:
        dlg = CrashInspectorDialog(
            parent=root,
            theme=theme,
            crash_count=2,
            on_cleared=lambda: cleared_called.append(True),
        )
        assert dlg.win.winfo_exists()
        # Trigger acknowledge action
        dlg._on_ack_clicked()
        assert fake_ack.exists()
        assert len(cleared_called) == 1
    finally:
        root.destroy()


def test_settings_dialog_presets_and_save():
    theme = ticker.THEMES["cyber-cyan"]
    saved_data = []

    root = tk.Tk()
    root.withdraw()
    try:
        init_settings = {
            "poll_pace": 2.0,
            "interval_seconds": 6,
            "scan_interval": 60,
            "stability_interval": 300,
            "monitor_self": True,
            "auto_hide_fullscreen": True,
            "alerts_pulse": True,
            "alerts_sound": False,
            "opacity": 0.94,
        }
        live_overhead = {"cpu": 0.3, "ram_mb": 42.5, "latency_ms": 1.2}

        dlg = SettingsDialog(
            parent=root,
            current_settings=init_settings,
            theme=theme,
            font_scale="normal",
            on_save=lambda s: saved_data.append(s),
            get_live_overhead=lambda: live_overhead,
        )
        assert dlg.win.winfo_exists()
        assert dlg._active_preset.get() == "balanced"

        # Apply Eco preset
        dlg._apply_preset("eco")
        assert dlg._poll_pace_var.get() == PRESETS["eco"]["poll_pace"]
        assert dlg._slide_interval_var.get() == PRESETS["eco"]["interval_seconds"]
        assert dlg._scan_interval_var.get() == PRESETS["eco"]["scan_interval"]
        assert dlg._monitor_self_var.get() == PRESETS["eco"]["monitor_self"]

        # Apply Turbo preset
        dlg._apply_preset("turbo")
        assert dlg._poll_pace_var.get() == PRESETS["turbo"]["poll_pace"]
        assert dlg._slide_interval_var.get() == PRESETS["turbo"]["interval_seconds"]

        # Save and verify callback
        dlg._on_save_clicked()
        assert len(saved_data) == 1
        assert saved_data[0]["poll_pace"] == PRESETS["turbo"]["poll_pace"]
        assert saved_data[0]["interval_seconds"] == PRESETS["turbo"]["interval_seconds"]
        assert saved_data[0]["monitor_self"] is True
    finally:
        root.destroy()


def test_ticker_mini_mode_and_peek(monkeypatch, tmp_path):
    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        app.latest_snap = Snapshot(self_cpu=0.2, self_ram_mb=35.0, self_latency_ms=1.5)

        # 1. Switch to mini mode
        app.set_mode("mini")
        assert app.mode == "mini"
        assert app.mini_frame.winfo_manager() == "pack"
        assert app.single_frame.winfo_manager() == ""

        # 2. Hover peek expansion
        app._on_mini_enter()
        assert app._is_peeking is True
        assert app.single_frame.winfo_manager() == "pack"
        assert app.mini_frame.winfo_manager() == ""

        # 3. Collapse peek
        app._collapse_peek()
        assert app._is_peeking is False
        assert app.mini_frame.winfo_manager() == "pack"
        assert app.single_frame.winfo_manager() == ""

        # 4. Mode cycling (mini -> single -> multi -> mini)
        app.toggle_mode()
        assert app.mode == "single"
        app.toggle_mode()
        assert app.mode == "multi"
        app.toggle_mode()
        assert app.mode == "mini"

        # 5. Overhead stats helper
        stats = app.get_overhead_stats()
        assert stats["cpu"] == 0.2
        assert stats["ram_mb"] == 35.0
        assert stats["latency_ms"] == 1.5

        # 6. Apply new settings
        new_conf = {
            "poll_pace": 5.0,
            "interval_seconds": 8,
            "scan_interval": 300,
            "stability_interval": 600,
            "monitor_self": False,
        }
        app.apply_new_settings(new_conf)
        assert app.settings["poll_pace"] == 5.0
        assert app.interval == 8
        assert fake_settings.exists()
        saved = json.loads(fake_settings.read_text(encoding="utf-8"))
        assert saved["poll_pace"] == 5.0
        assert saved["monitor_self"] is False
    finally:
        root.destroy()


def test_ticker_launch_chain_modes(monkeypatch, tmp_path):
    from sol_control_hud.chains import chain_note
    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)
    monkeypatch.setattr(ticker, "CHAINS_DIR", tmp_path)
    monkeypatch.setattr(chain_note, "CHAINS_DIR", tmp_path)

    chain_file = tmp_path / "test_chain.md"
    chain_file.write_text("""---
title: Test Chain
status: draft
---
# Test Content
""", encoding="utf-8")

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        app.latest_snap = Snapshot()

        # Launch desk mode
        app.launch_chain(chain_file, mode="desk")
        content_desk = chain_file.read_text(encoding="utf-8")
        assert "status: queued" in content_desk

        # Launch away mode
        app.launch_chain(chain_file, mode="away")
        content_away = chain_file.read_text(encoding="utf-8")
        assert "schedule: on away" in content_away
        assert "status: queued" in content_away
    finally:
        root.destroy()
