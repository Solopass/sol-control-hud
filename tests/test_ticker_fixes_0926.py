"""Ticker fixes (2026-09-26): chain results, Away progress, crash-ack history, staying visible when docked, media."""
import json
from datetime import datetime, timedelta

from sol_control_hud.away import screen as away_screen

from sol_control_hud.views import ticker

from sol_control_hud.data import snapshot as td
from sol_control_hud.data.collectors.media import parse_line
from sol_control_hud.data.snapshot import Snapshot, format_multiline_rows, format_slides


def test_last_chain_reads_the_runners_result_lines(tmp_path):
    log = tmp_path / "chains.log"
    log.write_text("2026-09-25T19:36:50 Idea sorter request.md: drafted Idea sorter.md (1 try)\n"
                   "2026-09-26T04:11:41 queued Code review.md: status: queued\n"
                   "2026-09-26T12:28:05 Code review.md: run 15 cancelled: cancelled by user\n", encoding="utf-8")
    assert td.read_last_finished_chain(log) == "Code review (cancelled)"    # was stuck on "Idea sorter (drafted)"
    log.write_text("2026-09-25T16:54:19 Summarize a folder.md: run 9 succeeded\n", encoding="utf-8")
    assert td.read_last_finished_chain(log) == "Summarize a folder (finished)"
    log.write_text("2026-09-25T16:54:19 Weekly digest.md: run 3 failed: model gone\n", encoding="utf-8")
    assert td.read_last_finished_chain(log) == "Weekly digest (failed)"


def test_away_progress_uses_the_away_screens_words(tmp_path, monkeypatch):
    now = datetime(2026, 9, 26, 3, 0).timestamp()
    iso = lambda ago: datetime.fromtimestamp(now - ago).isoformat(timespec="seconds")
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({"state": "running", "job": "hard-evals", "started": iso(600), "updated": iso(2),
                                    "done": 5, "total": 15, "detail": "gpt-oss-120b: parser test, run 2 of 3"}))
    monkeypatch.setattr(away_screen, "PROGRESS_F", progress)
    monkeypatch.setattr(away_screen, "CHAINS_F", tmp_path / "none.json")
    (tmp_path / "queue").mkdir()
    monkeypatch.setattr(away_screen, "QUEUE_DIR", tmp_path / "queue")   # empty queue: this is the only job
    eta = away_screen.EtaTracker()
    got = td.away_progress({"mode": "away", "present": True}, eta, now)
    assert got["away_line"] == "hard-evals — gpt-oss-120b: parser test, run 2 of 3"
    assert abs(got["away_fraction"] - 5 / 15) < 1e-9 and got["away_present"] and got["away_phase"] == "working"
    assert td.away_progress({"mode": "desk"}, eta, now) == {}

    s = Snapshot(ai_mode="away", sampled_at=1.0, **got)
    slides = format_slides(s)
    assert slides[0]["tag"] == "AWAY" and slides[0]["text"].startswith("33 %  ·  hard-evals")
    assert "you're here" in slides[0]["detail"]
    ai_row = next(r for r in format_multiline_rows(s) if r["title"] == "LOCAL AI")
    assert ("Job", "33% hard-evals — gpt-oss-120b: parser test, run 2 of 3", "#38bdf8") in ai_row["items"]
    assert not any(sl["tag"] == "AWAY" for sl in format_slides(Snapshot(ai_mode="desk", sampled_at=1.0)))


def test_crash_ack_keeps_the_history(tmp_path):
    ack = tmp_path / "stability-ack.json"
    ack.write_text(json.dumps({"acknowledged_until": "2026-09-25T20:35:15", "note": "09-25 TDR burst + BSOD 0x116"}))
    data = ticker.write_crash_ack(ack, now="2026-09-26T13:00:00")
    assert data["acknowledged_until"] == "2026-09-26T13:00:00"
    assert "09-25 TDR burst + BSOD 0x116" in data["note"] and "2026-09-25T20:35:15" in data["note"]
    assert json.loads(ack.read_text())["note"] == data["note"]
    fresh = ticker.write_crash_ack(tmp_path / "new.json", now="2026-09-26T13:00:00")   # no file yet: still fine
    assert fresh["acknowledged_until"] == "2026-09-26T13:00:00"


def test_crash_counts_include_gpu_resets():
    sys_slide = lambda s: next(x for x in format_slides(s) if x["tag"] == "SYS")
    assert "no new crashes" in sys_slide(Snapshot(backup_age_h=3.0, sampled_at=1.0))["text"]
    t = sys_slide(Snapshot(backup_age_h=3.0, gpu_resets=2, sampled_at=1.0))["text"]
    assert "2 GPU resets since last review" in t                     # said "0 crashes" before


def test_explorer_windows_never_count_as_fullscreen(monkeypatch):
    # Alt+Tab / Task View / Start are full-monitor Explorer windows: the ticker hid for them (09-26)
    assert "explorer.exe" in ticker.SHELL_PROCESSES and "startmenuexperiencehost.exe" in ticker.SHELL_PROCESSES
    monkeypatch.setattr(ticker, "foreground_owner", lambda hwnd: "explorer.exe")
    assert ticker.is_foreground_fullscreen() is False


def test_esc_no_longer_quits():
    import tkinter as tk
    root = tk.Tk(); root.withdraw()
    try:
        app = ticker.TickerApp(root)
        assert not root.bind("<Escape>")                              # an Esc meant for something else closed it
        assert hasattr(app, "_keep_on_top")
    finally:
        root.destroy()


def test_media_lines_parse():
    m = parse_line('{"app":"Spotify.exe","title":"Voyager","artist":"Daft Punk","status":"Playing"}')
    assert (m.title, m.clean_app, m.playing) == ("Voyager", "Spotify", True)
    assert parse_line('{"status":"none"}').status == "none"
    assert parse_line("garbage").status == "none"


def test_topmost_is_passed_as_a_real_handle(monkeypatch):
    # a plain -1 arrives as 0xFFFFFFFF on 64-bit Windows: SetWindowPos failed silently and the docked ticker stayed
    # under the taskbar (checked live 09-26)
    import ctypes
    from ctypes import wintypes
    calls = []
    monkeypatch.setattr(ctypes.windll.user32, "SetWindowPos", lambda *a: calls.append(a) or 1)
    ticker.raise_topmost(12345)
    after = calls[0][1]
    assert isinstance(after, wintypes.HWND) and after.value in (-1, 2 ** 64 - 1)
