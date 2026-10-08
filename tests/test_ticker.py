"""Unit tests for SOL Ticker HUD data aggregation, formatting, and settings persistence."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sol_control_hud.data.snapshot import (
    Snapshot,
    check_workspace_git,
    clean_log_line,
    count_note_words,
    format_multiline_rows,
    format_rate,
    format_slides,
    read_ai_models,
    read_chains_state,
    read_daily_note_status,
    read_last_finished_chain,
    read_llm_state,
    TickerCollector,
)


def test_format_slides_healthy():
    snap = Snapshot(
        gpu_name="AMD Radeon RX 9070 XT",
        gpu_load=24.2,
        vram_used_gb=6.5,
        vram_total_gb=16.0,
        vram_evicted=False,
        vram_tight=False,
        ram_used_gb=32.0,
        ram_total_gb=96.0,
        ram_percent=33.3,
        cpu_percent=4.5,
        ai_model="sol-fast",
        ai_state="loaded",
        ai_mode="desk",
        ai_reason="test",
        gpu_locked=False,
        chain_running=None,
        chain_step=None,
        chain_pending_count=0,
        services={"Router": True, "Embed": True, "HUD": False, "Media": False},
        sampled_at=1000.0,
    )

    slides = format_slides(snap)
    assert len(slides) >= 4

    # HW slide
    assert slides[0]["tag"] == "HW"
    assert "GPU 24%" in slides[0]["text"]
    assert "VRAM 6.5/16G" in slides[0]["text"]
    assert "RAM 32.0G" in slides[0]["text"]
    assert slides[0]["color"] == "#38bdf8"

    # AI slide
    assert slides[1]["tag"] == "AI"
    assert "sol-fast (loaded)" in slides[1]["text"]
    assert "Desk mode" in slides[1]["text"]
    assert slides[1]["color"] == "#4ade80"  # loaded is green

    # RUN slide
    assert slides[2]["tag"] == "RUN"
    assert "Chains: idle" in slides[2]["text"]

    # SVC slide
    assert slides[3]["tag"] == "SVC"
    assert "Router ●" in slides[3]["text"]
    assert "HUD ○" in slides[3]["text"]


def test_format_slides_evicted():
    snap = Snapshot(
        gpu_load=50.0,
        vram_used_gb=15.2,
        vram_total_gb=16.0,
        vram_evicted=True,
        vram_spill_impact="slow",
        ai_model="sol-fast",
        ai_state="sleeping",
        ai_mode="away",
    )
    slides = format_slides(snap)
    assert "EVICTED!" in slides[0]["text"]
    assert slides[0]["color"] == "#f87171"  # red


def test_spill_without_a_slow_answer_is_amber_not_red():
    for impact, word in (("unknown", "spill? speed not measured"), ("fine", "spill, speed OK")):
        s = Snapshot(gpu_load=50.0, vram_used_gb=15.2, vram_total_gb=16.0, vram_evicted=True, vram_spill_impact=impact)
        slide = format_slides(s)[0]
        assert word in slide["text"] and "EVICTED" not in slide["text"]
        assert slide["color"] == "#fbbf24"  # amber


def test_format_multiline_rows():
    snap = Snapshot(
        gpu_load=15.0,
        vram_used_gb=5.0,
        vram_total_gb=16.0,
        ram_used_gb=30.0,
        ram_total_gb=96.0,
        cpu_percent=5.0,
        ai_model="sol-smart",
        ai_state="sleeping",
        ai_mode="desk",
        chain_running="summarize-notes",
        chain_step="Step 1/3",
        chain_pending_count=2,
        gpu_locked=True,
        services={"Router": True, "HUD": True},
    )
    rows = format_multiline_rows(snap)
    assert len(rows) == 5
    assert rows[0]["title"] == "HARDWARE"
    assert rows[1]["title"] == "LOCAL AI"
    assert rows[2]["title"] == "CHAINS"
    assert rows[3]["title"] == "SERVICES"
    assert rows[4]["title"] == "SYSTEM"

    # Verify chain details
    chain_row = rows[2]
    items_dict = {k: v for k, v, _ in chain_row["items"]}
    assert "summarize-notes [Step 1/3]" in items_dict["Current"]
    assert items_dict["Pending"] == "2"
    assert items_dict["GPU Lock"] == "Held"


def test_read_llm_state(tmp_path: Path):
    state_file = tmp_path / "state.json"
    # Missing file returns defaults
    mode, reason, until = read_llm_state(state_file)
    assert mode == "desk"
    assert reason is None
    assert until is None

    # Valid JSON
    state_file.write_text(json.dumps({"mode": "away", "reason": "user clicked away", "until": "2026-09-25T22:30:00"}), encoding="utf-8")
    mode, reason, until = read_llm_state(state_file)
    assert mode == "away"
    assert reason == "user clicked away"
    assert until == "2026-09-25T22:30:00"

    # Corrupt JSON
    state_file.write_text("{corrupt", encoding="utf-8")
    mode, reason, until = read_llm_state(state_file)
    assert mode == "desk"


def test_read_chains_state(tmp_path: Path):
    chains_file = tmp_path / "chains.json"
    # Missing file returns defaults
    running, step, pending = read_chains_state(chains_file)
    assert running is None
    assert step is None
    assert pending == 0

    # Valid running dict
    data = {
        "running": {"name": "test-pipeline", "step": 2, "total_steps": 4},
        "pending": ["next-job"],
    }
    chains_file.write_text(json.dumps(data), encoding="utf-8")
    running, step, pending = read_chains_state(chains_file)
    assert running == "test-pipeline"
    assert step == "Step 2/4"
    assert pending == 1


def test_read_ai_models_offline():
    with patch("httpx.Client.get", side_effect=Exception("Connection refused")):
        model, state, generating, ctx_size = read_ai_models()
        assert model is None
        assert state == "offline"
        assert generating is False
        assert ctx_size is None


def test_read_ai_models_loaded():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": "sol-fast", "status": {"value": "loaded"}, "meta": {"n_ctx": 32768}},
            {"id": "sol-smart", "status": {"value": "unloaded"}},
        ]
    }
    mock_slots = MagicMock()
    mock_slots.status_code = 200
    mock_slots.json.return_value = [{"id": 0, "is_processing": False, "n_ctx": 32768}]

    def mock_get(url, *args, **kwargs):
        if "slots" in url:
            return mock_slots
        return mock_resp

    with patch("httpx.Client.get", side_effect=mock_get):
        model, state, generating, ctx_size = read_ai_models()
        assert model == "sol-fast"
        assert state == "loaded"
        assert generating is False
        assert ctx_size == 32768


def test_read_ai_models_generating():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": "sol-fast", "status": {"value": "loaded"}, "meta": {"n_ctx": 32768}},
        ]
    }
    mock_slots = MagicMock()
    mock_slots.status_code = 200
    mock_slots.json.return_value = [{"id": 0, "is_processing": True, "n_ctx": 32768}]

    asked = []

    def mock_get(url, *args, **kwargs):
        asked.append(url)
        if "slots" in url:
            return mock_slots
        return mock_resp

    with patch("httpx.Client.get", side_effect=mock_get):
        model, state, generating, ctx_size = read_ai_models()
        assert model == "sol-fast"
        assert state == "loaded"
        assert ctx_size == 32768
    # never asks the model itself: /slots counts as use and kept models from ever unloading (09-26)
    assert not any("slots" in u for u in asked)
    assert generating is False


def test_generating_comes_from_the_gpu_not_the_model():
    from sol_control_hud.data.snapshot import ai_busy
    llama = [{"name": "llama-server", "dedicated_gb": 9.4}, {"name": "brave", "dedicated_gb": 0.6}]
    assert ai_busy("loaded", 92.0, llama)
    assert not ai_busy("loaded", 12.0, llama)                                   # loaded but idle
    assert not ai_busy("sleeping", 92.0, llama)
    assert not ai_busy("loaded", 92.0, [{"name": "brave", "dedicated_gb": 0.6}])   # something else is busy
    assert not ai_busy("loaded", None, llama)


def test_nothing_loaded_is_not_shown_as_a_model():
    with patch("httpx.Client.get") as get:
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"data": [{"id": "sol-fast", "status": {"value": "unloaded"}}]}
        assert read_ai_models()[:2] == (None, "unloaded")
    slides = format_slides(Snapshot(ai_state="unloaded", ai_model=None, sampled_at=1.0))
    ai = next(s for s in slides if s["tag"] == "AI")
    assert ai["text"].startswith("No model loaded  ·  ") and "sol-fast" not in ai["text"]


def test_collector_collect_once():
    c = TickerCollector()
    snap = c.collect_once()
    assert isinstance(snap, Snapshot)
    assert snap.ram_total_gb > 0
    assert snap.sampled_at > 0


def test_truncate_text():
    import tkinter as tk
    from tkinter import font as tkfont
    from sol_control_hud.views.ticker import truncate_text

    root = tk.Tk()
    root.withdraw()
    try:
        f = tkfont.Font(family="Segoe UI", size=9)
        # Short text not truncated
        assert truncate_text("Short text", f, 300) == "Short text"

        # Long text truncated with ellipsis
        long_text = "This is a very long text string that will certainly exceed seventy pixels in width"
        res = truncate_text(long_text, f, 70)
        assert res.endswith("…")
        assert f.measure(res) <= 70
    finally:
        root.destroy()


def test_ui_dimensions_and_target_mapping():
    from sol_control_hud.views.ticker import MULTI_HEIGHT, SINGLE_HEIGHT, WIDTH

    assert WIDTH == 450
    assert SINGLE_HEIGHT == 30
    assert MULTI_HEIGHT == 168


def test_read_last_finished_chain(tmp_path: Path):
    log_file = tmp_path / "chains.log"
    # Missing file returns None
    assert read_last_finished_chain(log_file) is None

    # Empty file returns None
    log_file.write_text("", encoding="utf-8")
    assert read_last_finished_chain(log_file) is None

    # Log with completed chain
    content = (
        "2026-09-25T20:00:00 starting pipeline-a\n"
        "2026-09-25T20:01:00 pipeline-a: finished in 60s\n"
        "2026-09-25T20:10:00 daily-digest.md: drafted response\n"
    )
    log_file.write_text(content, encoding="utf-8")
    assert read_last_finished_chain(log_file) == "daily-digest (drafted)"

    # Log with finished chain
    content2 = "2026-09-25T20:00:00 nightly-review: finished\n"
    log_file.write_text(content2, encoding="utf-8")
    assert read_last_finished_chain(log_file) == "nightly-review (finished)"


def test_format_slides_vram_and_chain_history():
    snap = Snapshot(
        gpu_name="AMD Radeon RX 9070 XT",
        gpu_load=20.0,
        vram_used_gb=10.0,
        vram_total_gb=16.0,
        vram_top_process="llama-server 8.2G",
        vram_processes=[{"name": "llama-server", "dedicated_gb": 8.2, "pid": 1234}],
        chain_running=None,
        chain_last_finished="nightly-review (finished)",
        services={"Router": True},
    )
    slides = format_slides(snap)
    hw_slide = next(s for s in slides if s["tag"] == "HW")
    assert "llama-server 8.2G" in hw_slide["text"]
    assert "Top VRAM: llama-server 8.2G" in hw_slide["detail"]

    run_slide = next(s for s in slides if s["tag"] == "RUN")
    assert "Last: nightly-review (finished)" in run_slide["text"]

    multiline = format_multiline_rows(snap)
    hw_row = next(r for r in multiline if r["title"] == "HARDWARE")
    hw_items = {k: v for k, v, _ in hw_row["items"]}
    assert "llama-server 8.2G" in hw_items["VRAM"]

    chain_row = next(r for r in multiline if r["title"] == "CHAINS")
    chain_items = {k: v for k, v, _ in chain_row["items"]}
    assert "Last: nightly-review (finished)" in chain_items["Current"]


def test_slide_filtering_and_defaults():
    from sol_control_hud.views.ticker import DEFAULT_SLIDES_ENABLED

    for tag in ("HW", "AI", "RUN", "SVC", "DISK", "SYS"):
        assert tag in DEFAULT_SLIDES_ENABLED
        assert DEFAULT_SLIDES_ENABLED[tag] is True

    snap = Snapshot(services={"Router": True})
    all_slides = format_slides(snap)
    assert len(all_slides) >= 4

    # Filter with only HW and AI enabled
    filter_map = {tag: (tag in ("HW", "AI")) for tag in DEFAULT_SLIDES_ENABLED}
    filtered = [s for s in all_slides if filter_map.get(s["tag"], True)]
    tags = [s["tag"] for s in filtered]
    # ALERT (here: the router is offline in this snapshot) can't be filtered away: it's what needs you
    assert tags == ["HW", "AI", "ALERT"]


def test_interpolate_color():
    from sol_control_hud.views.ticker import interpolate_color

    # Exact endpoints
    assert interpolate_color("#000000", "#ffffff", 0.0) == "#000000"
    assert interpolate_color("#000000", "#ffffff", 1.0) == "#ffffff"
    # Clamping
    assert interpolate_color("#000000", "#ffffff", -0.5) == "#000000"
    assert interpolate_color("#000000", "#ffffff", 1.5) == "#ffffff"
    # Midpoint
    assert interpolate_color("#000000", "#ffffff", 0.5) == "#7f7f7f"


def test_clean_log_line_and_error_parsing(tmp_path: Path):
    assert clean_log_line("2026-09-25T12:34:56 pipeline-a: finished") == "pipeline-a: finished"
    assert clean_log_line("2026-09-25 12:34:56 [info] pipeline-a: finished") == "pipeline-a: finished"
    assert clean_log_line("simple message") == "simple message"

    log_file = tmp_path / "chains.log"
    # Test error logging
    log_file.write_text("2026-09-25T21:00:00 pipeline-err: error OutOfMemoryError\n", encoding="utf-8")
    assert read_last_finished_chain(log_file) == "pipeline-err (failed)"

    # Test completed logging with run ID
    log_file.write_text("2026-09-25T21:10:00 pipeline-b: run 123 completed\n", encoding="utf-8")
    assert read_last_finished_chain(log_file) == "pipeline-b (completed)"


def test_event_checking_alerts():
    import tkinter as tk
    from sol_control_hud.views.ticker import TickerApp, ACCENT_GREEN, ACCENT_RED, ACCENT_CYAN

    root = tk.Tk()
    root.withdraw()
    try:
        app = TickerApp(root)
        app.trigger_alert = MagicMock()

        # 1. Chain completed
        prev = Snapshot(chain_running="digest", sampled_at=100.0)
        curr = Snapshot(chain_running=None, chain_last_finished="digest (completed)", sampled_at=102.0)
        app._check_events(prev, curr)
        app.trigger_alert.assert_called_with(ACCENT_GREEN, "chain_completed")

        # 2. Chain failed
        app.trigger_alert.reset_mock()
        prev = Snapshot(chain_running="digest", sampled_at=100.0)
        curr = Snapshot(chain_running=None, chain_last_finished="digest (failed)", sampled_at=102.0)
        app._check_events(prev, curr)
        app.trigger_alert.assert_called_with(ACCENT_RED, "chain_failed")

        # 3. VRAM evicted transition
        app.trigger_alert.reset_mock()
        prev = Snapshot(vram_evicted=False, sampled_at=100.0)
        curr = Snapshot(vram_evicted=True, vram_spill_impact="unknown", sampled_at=102.0)
        app._check_events(prev, curr)
        app.trigger_alert.assert_not_called()                  # the counter alone: no red pulse
        slow = Snapshot(vram_evicted=True, vram_spill_impact="slow", sampled_at=104.0)
        app._check_events(curr, slow)
        app.trigger_alert.assert_called_with(ACCENT_RED, "vram_evicted")

        # 4. System crash
        app.trigger_alert.reset_mock()
        prev = Snapshot(unexpected_reboots=0, gpu_resets=0, sampled_at=100.0)
        curr = Snapshot(unexpected_reboots=1, gpu_resets=0, sampled_at=102.0)
        app._check_events(prev, curr)
        app.trigger_alert.assert_called_with(ACCENT_RED, "system_crash")

        # 5. AI mode transition
        app.trigger_alert.reset_mock()
        prev = Snapshot(ai_mode="desk", sampled_at=100.0)
        curr = Snapshot(ai_mode="away", sampled_at=102.0)
        app._check_events(prev, curr)
        app.trigger_alert.assert_called_with(ACCENT_CYAN, "ai_mode")
    finally:
        root.destroy()


def test_ai_generating_formatting():
    snap = Snapshot(
        ai_model="sol-fast",
        ai_state="loaded",
        ai_mode="desk",
        ai_generating=True,
        ai_ctx_size=32768,
        services={"Router": True},
    )
    slides = format_slides(snap)
    ai_slide = next(s for s in slides if s["tag"] == "AI")
    assert "sol-fast ⚡" in ai_slide["text"]
    assert "generating ⚡" in ai_slide["text"]
    assert "Active inference on sol-fast (ctx: 32k)" in ai_slide["detail"]
    assert ai_slide["color"] == "#4ade80"

    rows = format_multiline_rows(snap)
    ai_row = next(r for r in rows if r["title"] == "LOCAL AI")
    ai_dict = {k: (v, c) for k, v, c in ai_row["items"]}
    assert ai_dict["Model"] == ("sol-fast ⚡", "#4ade80")
    assert ai_dict["State"] == ("generating ⚡", "#4ade80")


def test_auto_hide_logic():
    import tkinter as tk
    from sol_control_hud.views.ticker import TickerApp

    root = tk.Tk()
    root.withdraw()
    try:
        app = TickerApp(root)
        app.auto_hide_fullscreen = True

        # When game is active in state.json
        app.latest_snap = Snapshot(ai_mode="off", ai_reason="game: Slay the Spire 2")
        with patch("sol_control_hud.views.ticker.is_foreground_fullscreen", return_value=False):
            assert app._check_auto_hide() is True
            assert app._is_hidden_for_fullscreen is True

        # When game exits back to desk mode and not fullscreen
        app.latest_snap = Snapshot(ai_mode="desk")
        with patch("sol_control_hud.views.ticker.is_foreground_fullscreen", return_value=False):
            assert app._check_auto_hide() is False
            assert app._is_hidden_for_fullscreen is False

        # When disabled in settings
        app.auto_hide_fullscreen = False
        app.latest_snap = Snapshot(ai_mode="off", ai_reason="game: Slay the Spire 2")
        with patch("sol_control_hud.views.ticker.is_foreground_fullscreen", return_value=True):
            assert app._check_auto_hide() is False
    finally:
        root.destroy()


def test_count_note_words():
    # YAML frontmatter
    text_yaml = "---\ntitle: Daily Note\ntags: [journal, sol]\n---\nHere are five words written."
    assert count_note_words(text_yaml) == 5

    # Key-value header metadata without ---
    text_kv = "hidden: false\ntitle:\ntags:\n\nNote:\nTesting note word count in unit test."
    assert count_note_words(text_kv) == 8  # "Note:" + 7 words

    # Plain text without metadata
    text_plain = "Simple three words"
    assert count_note_words(text_plain) == 3

    # Empty text
    assert count_note_words("") == 0


def test_read_daily_note_status(tmp_path: Path):
    import time
    # Non-existent
    exists, words, mod_time, p = read_daily_note_status(tmp_path)
    assert exists is False
    assert words == 0
    assert mod_time is None

    # Existing note
    year = time.strftime("%Y")
    today = time.strftime("%Y-%m-%d")
    note_dir = tmp_path / year
    note_dir.mkdir(parents=True)
    note_file = note_dir / f"{today}.md"
    note_file.write_text("hidden: false\n\nDaily log entry with seven words total.", encoding="utf-8")

    exists, words, mod_time, p = read_daily_note_status(tmp_path)
    assert exists is True
    assert words == 7
    assert mod_time is not None
    assert p == note_file


def test_check_workspace_git(tmp_path: Path):
    # Non-existent workspace
    count, dirty, total = check_workspace_git(tmp_path / "missing")
    assert count == 0
    assert dirty == []
    assert total == 0

    # Create mock repos
    repo_clean = tmp_path / "repo-clean"
    repo_clean.mkdir()
    (repo_clean / ".git").mkdir()

    repo_dirty = tmp_path / "repo-dirty"
    repo_dirty.mkdir()
    (repo_dirty / ".git").mkdir()

    # Not a repo
    not_repo = tmp_path / "not-repo"
    not_repo.mkdir()

    def fake_subprocess_run(cmd, **kwargs):
        assert "--no-optional-locks" in cmd          # never takes .git/index.lock (a commit at that moment would fail)
        cwd = cmd[cmd.index("-C") + 1]  # ["git", "--no-optional-locks", "-C", str(d), "status", "--porcelain"]
        mock = MagicMock()
        mock.returncode = 0
        if "repo-dirty" in cwd:
            mock.stdout = " M modified.py\n"
        else:
            mock.stdout = ""
        return mock

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        count, dirty, total = check_workspace_git(tmp_path)
        assert count == 1
        assert dirty == ["repo-dirty"]
        assert total == 2


def test_format_rate():
    assert format_rate(0.0) == "0 KB/s"
    assert format_rate(45.2) == "45 KB/s"
    assert format_rate(1024.0) == "1.0 MB/s"
    assert format_rate(2560.0) == "2.5 MB/s"


def test_format_slides_phase4():
    # 1. NOTE slide exists with >= 250 words
    snap = Snapshot(
        note_exists=True,
        note_words=300,
        note_time="16:45",
        git_dirty_count=2,
        git_dirty_repos=["media-api", "speedman"],
        git_total_repos=13,
        net_down_kb=1500.0,
        net_up_kb=80.0,
    )
    slides = format_slides(snap)
    note_slide = next(s for s in slides if s["tag"] == "NOTE")
    assert "Today: 300 words" in note_slide["text"]
    assert "Edited 16:45" in note_slide["text"]
    assert note_slide["color"] == "#4ade80"  # green for >= 250

    git_slide = next(s for s in slides if s["tag"] == "GIT")
    assert "2 repos dirty" in git_slide["text"]
    assert "media-api, speedman" in git_slide["text"]
    assert git_slide["color"] == "#fbbf24"  # amber for dirty

    net_slide = next(s for s in slides if s["tag"] == "NET")
    assert "↓ 1.5 MB/s" in net_slide["text"]
    assert "↑ 80 KB/s" in net_slide["text"]
    assert net_slide["color"] == "#4ade80"  # green for > 500 KB/s

    # 2. NOTE slide not exists & clean git
    snap2 = Snapshot(
        note_exists=False,
        note_words=0,
        git_dirty_count=0,
        git_total_repos=13,
        net_down_kb=20.0,
        net_up_kb=10.0,
    )
    slides2 = format_slides(snap2)
    note_slide2 = next(s for s in slides2 if s["tag"] == "NOTE")
    assert "No entry today" in note_slide2["text"]
    assert note_slide2["color"] == "#94a3b8"

    git_slide2 = next(s for s in slides2 if s["tag"] == "GIT")
    assert "All 13 repos clean" in git_slide2["text"]
    assert git_slide2["color"] == "#4ade80"

    net_slide2 = next(s for s in slides2 if s["tag"] == "NET")
    assert "↓ 20 KB/s" in net_slide2["text"]
    assert net_slide2["color"] == "#38bdf8"


def test_format_multiline_rows_phase4():
    snap = Snapshot(
        git_dirty_count=1,
        git_total_repos=13,
        note_exists=True,
        note_words=120,
        net_down_kb=50.0,
    )
    rows = format_multiline_rows(snap)
    sys_row = next(r for r in rows if r["title"] == "SYSTEM")
    items = {k: v for k, v, _ in sys_row["items"]}
    assert items["Git"] == "1 dirty"
    assert items["Note"] == "120w"
    assert "Net" in items


def test_ticker_app_visible_slides_phase4():
    import tkinter as tk
    from sol_control_hud.views.ticker import TickerApp, DEFAULT_SLIDES_ENABLED

    assert "NOTE" in DEFAULT_SLIDES_ENABLED
    assert "GIT" in DEFAULT_SLIDES_ENABLED
    assert "NET" in DEFAULT_SLIDES_ENABLED

    root = tk.Tk()
    root.withdraw()
    try:
        app = TickerApp(root)
        assert app.slides_enabled.get("NOTE") is True
        assert app.slides_enabled.get("GIT") is True
        assert app.slides_enabled.get("NET") is True

        # Toggle NOTE off
        app.toggle_slide_enabled("NOTE")
        assert app.slides_enabled["NOTE"] is False

        # Toggle NOTE back on
        app.toggle_slide_enabled("NOTE")
        assert app.slides_enabled["NOTE"] is True
    finally:
        root.destroy()


def test_read_daily_note_status_yesterday_fallback(tmp_path: Path):
    import time
    # Yesterday's note exists, today does not
    y_time = time.time() - 86400
    y_year = time.strftime("%Y", time.localtime(y_time))
    y_date = time.strftime("%Y-%m-%d", time.localtime(y_time))
    y_dir = tmp_path / y_year
    y_dir.mkdir(parents=True, exist_ok=True)
    y_file = y_dir / f"{y_date}.md"
    y_file.write_text("hidden: false\n\nYesterday entry with six words written.", encoding="utf-8")

    exists, words, mod_time, p = read_daily_note_status(tmp_path)
    assert exists is False
    assert words == 6  # yesterday's words returned as fallback context
    assert mod_time is None


def test_collector_invalidate_cache():
    collector = TickerCollector()
    collector._last_stability = 500.0
    collector._last_system = 500.0
    collector._last_git = 500.0
    collector.invalidate_cache()
    assert collector._last_stability == 0.0
    assert collector._last_system == 0.0
    assert collector._last_git == 0.0


def test_ticker_app_quick_actions(tmp_path: Path):
    import tkinter as tk
    from sol_control_hud.views.ticker import TickerApp

    root = tk.Tk()
    root.withdraw()
    try:
        app = TickerApp(root)
        # Test toggle_ai_mode
        app.latest_snap = Snapshot(ai_mode="desk")
        with patch.object(app, "switch_ai_mode") as mock_switch:
            app.toggle_ai_mode()
            mock_switch.assert_called_once_with("away")

        # while Away works the same click offers Stop AI work (never a hard switch to Desk that loses the job)
        app.latest_snap = Snapshot(ai_mode="away")
        with patch.object(app, "switch_ai_mode") as mock_switch, patch.object(app, "stop_ai_work") as mock_stop:
            app.toggle_ai_mode()
            mock_switch.assert_not_called()
            mock_stop.assert_called_once()

        # Test acknowledge_crashes: asks first, keeps the earlier note (the crash history)
        mock_ack = tmp_path / "stability-ack.json"
        mock_ack.write_text(json.dumps({"acknowledged_until": "2026-09-25T20:35:15", "note": "BSOD 0x116 history"}))
        with patch("sol_control_hud.views.ticker.ACK_FILE", mock_ack), patch.object(app, "refresh_data_now") as mock_refresh:
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app.acknowledge_crashes()
            mock_refresh.assert_not_called()                                  # said no: nothing changes
            assert "20:35:15" in mock_ack.read_text(encoding="utf-8")
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.acknowledge_crashes()
            mock_refresh.assert_called_once()
            ack_data = json.loads(mock_ack.read_text(encoding="utf-8"))
            assert ack_data["acknowledged_until"] > "2026-09-25T20:35:15"
            assert "BSOD 0x116 history" in ack_data["note"]
    finally:
        root.destroy()


def test_acquire_instance_lock(tmp_path: Path):
    from sol_control_hud.views import ticker
    lock_file = tmp_path / "test.lock"
    with patch.object(ticker, "LOCK_FILE", lock_file):
        # 1st lock succeeds
        f1 = ticker.acquire_instance_lock()
        assert f1 is not None
        try:
            # 2nd lock on the same file fails
            f2 = ticker.acquire_instance_lock()
            assert f2 is None
        finally:
            f1.close()


def test_activate_existing_instance(tmp_path: Path):
    from sol_control_hud.views import ticker
    trigger_file = tmp_path / "ticker-activate.trigger"
    with patch.object(ticker, "TRIGGER_FILE", trigger_file):
        with patch("ctypes.windll.user32.FindWindowW", return_value=12345) as mock_find:
            with patch("ctypes.windll.user32.ShowWindow") as mock_show:
                with patch("ctypes.windll.user32.SetWindowPos") as mock_pos:
                    with patch("ctypes.windll.user32.SetForegroundWindow") as mock_fore:
                        ticker.activate_existing_instance()
                        assert trigger_file.exists()
                        mock_find.assert_called_once_with(None, ticker.WINDOW_TITLE)
                        mock_show.assert_called_once_with(12345, 9)
                        mock_pos.assert_called_once()
                        mock_fore.assert_called_once_with(12345)


def test_ticker_app_activation_trigger(tmp_path: Path):
    import tkinter as tk
    from sol_control_hud.views import ticker

    trigger_file = tmp_path / "ticker-activate.trigger"

    root = tk.Tk()
    root.withdraw()
    try:
        with patch.object(ticker, "TRIGGER_FILE", trigger_file):
            app = ticker.TickerApp(root)
            trigger_file.write_text("123456.78", encoding="utf-8")
            with patch.object(app, "trigger_alert") as mock_alert:
                with patch.object(app, "_apply_geometry") as mock_geo:
                    app._is_hidden_for_fullscreen = True
                    app._check_activation_trigger()
                    assert not trigger_file.exists()
                    assert app._is_hidden_for_fullscreen is False
                    mock_geo.assert_called()
                    mock_alert.assert_called_once_with(ticker.ACCENT_CYAN, "activated")
    finally:
        root.destroy()


def test_ticker_app_reset_position():
    import tkinter as tk
    from sol_control_hud.views import ticker

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        app.docked = True
        app.settings["docked"] = True
        app._reset_position()
        assert app.docked is False
        assert app.settings["docked"] is False
    finally:
        root.destroy()


def test_themes_definition():
    from sol_control_hud.views.ticker import THEMES, DEFAULT_THEME

    expected_themes = [
        "cyber-cyan",
        "high-contrast",
        "amber-terminal",
        "emerald-matrix",
        "nordic-frost",
        "dracula-synth",
    ]
    for key in expected_themes:
        assert key in THEMES, f"Missing theme {key}"
        t = THEMES[key]
        for field in ("name", "bg", "border", "card", "badge_bg", "text_main", "text_muted", "text_dim", "accent_primary"):
            assert field in t, f"Missing field {field} in theme {key}"

    assert DEFAULT_THEME == "cyber-cyan"


def test_ticker_app_theme_switching(tmp_path: Path):
    import tkinter as tk
    from sol_control_hud.views import ticker

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        assert app.theme_name == "cyber-cyan"

        # Switch to Amber Terminal
        app.set_theme("amber-terminal")
        assert app.theme_name == "amber-terminal"
        assert app.settings["theme"] == "amber-terminal"
        assert app.container.cget("bg") == ticker.THEMES["amber-terminal"]["bg"]
        assert app.root.cget("bg") == ticker.THEMES["amber-terminal"]["border"]

        # Switch to High Contrast OLED
        app.set_theme("high-contrast")
        assert app.theme_name == "high-contrast"
        assert app.container.cget("bg") == "#000000"
        assert app.single_text.cget("fg") == "#ffffff"

        # Switch to Dracula Synthwave
        app.set_theme("dracula-synth")
        assert app.theme_name == "dracula-synth"
        assert app.btn_web.cget("fg") == ticker.THEMES["dracula-synth"]["accent_primary"]

        # Fallback on invalid theme name
        app.set_theme("unknown-colorway")
        assert app.theme_name == "cyber-cyan"
    finally:
        root.destroy()


def test_font_scaling_definitions_and_runtime(monkeypatch, tmp_path):
    from sol_control_hud.views import ticker

    # 1. Verify FONT_SCALES definitions
    assert "small" in ticker.FONT_SCALES
    assert "normal" in ticker.FONT_SCALES
    assert "large" in ticker.FONT_SCALES
    assert ticker.FONT_SCALES["normal"]["font_single"] == 9
    assert ticker.FONT_SCALES["small"]["font_single"] == 8
    assert ticker.FONT_SCALES["large"]["font_single"] == 10

    # 2. Test live application scaling
    import tkinter as tk
    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)

        # Default scale is normal
        assert app.font_scale == "normal"
        assert app.font_single.cget("size") == 9
        assert app.font_row.cget("size") == 8

        # Switch to small
        app.set_font_scale("small")
        assert app.font_scale == "small"
        assert app.settings["font_scale"] == "small"
        assert app.font_single.cget("size") == 8
        assert app.font_row.cget("size") == 7

        # Switch to large
        app.set_font_scale("large")
        assert app.font_scale == "large"
        assert app.settings["font_scale"] == "large"
        assert app.font_single.cget("size") == 10
        assert app.font_row.cget("size") == 9

        # Fallback on unknown
        app.set_font_scale("huge-nonexistent")
        assert app.font_scale == "normal"
    finally:
        root.destroy()


def test_switch_ai_model_dispatch(monkeypatch, tmp_path):
    import tkinter as tk
    from sol_control_hud.views import ticker
    import time

    calls = []
    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None, **kwargs):
            calls.append((url, json))

    import httpx
    monkeypatch.setattr(httpx, "Client", DummyClient)

    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        app.latest_snap.ai_model = "sol-fast"

        # Switch to sol-smart
        app.switch_ai_model("sol-smart")
        time.sleep(0.05)
        assert any(url == "http://127.0.0.1:11440/models/load" and json == {"model": "sol-smart"} for url, json in calls)

        # Unload
        calls.clear()
        app.switch_ai_model("unload")
        time.sleep(0.05)
        assert any(url == "http://127.0.0.1:11440/models/unload" and json == {"model": "sol-fast"} for url, json in calls)
    finally:
        root.destroy()


def test_chain_enumeration_and_launch(monkeypatch, tmp_path):
    import tkinter as tk
    from sol_control_hud.views import ticker

    chains_dir = tmp_path / "Chains"
    chains_dir.mkdir()
    (chains_dir / "Code review.md").write_text("---\nstatus: draft\n---\nPrompt", encoding="utf-8")
    (chains_dir / "Weekly digest.md").write_text("---\nstatus: draft\n---\nPrompt", encoding="utf-8")
    (chains_dir / "Idea sorter request.md").write_text("ignore request", encoding="utf-8")
    (chains_dir / ".hidden.md").write_text("ignore hidden", encoding="utf-8")

    # 1. Test get_available_chains
    chains = ticker.get_available_chains(chains_dir)
    assert len(chains) == 2
    names = [c[0] for c in chains]
    assert "Code review" in names
    assert "Weekly digest" in names
    assert "Idea sorter request" not in names

    # 2. Test launch_chain
    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)
    monkeypatch.setattr(ticker, "CHAINS_DIR", chains_dir)

    launched = []
    def dummy_set_status(path, status):
        launched.append((path, status))

    import sol_control_hud.chains.chain_note as cn
    monkeypatch.setattr(cn, "set_chain_status", dummy_set_status)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        target_path = chains_dir / "Code review.md"
        app.launch_chain(target_path)
        assert (target_path, "queued") in launched
    finally:
        root.destroy()


def test_gpu_telemetry_slides_and_hotspot_alert(monkeypatch, tmp_path):
    import tkinter as tk
    from sol_control_hud.views import ticker
    from sol_control_hud.data import snapshot as ticker_data
    from sol_control_hud.data.snapshot import Snapshot, format_slides, format_multiline_rows

    # 1. Test normal GPU temperature & fan RPM in format_slides
    snap_normal = Snapshot(
        gpu_name="AMD Radeon RX 9070 XT",
        gpu_load=30.0,
        gpu_temp=56,
        gpu_hotspot=58,
        gpu_mem_temp=76,
        gpu_fan_rpm=1100,
        vram_used_gb=8.0,
        vram_total_gb=16.0,
        sampled_at=100.0,
    )
    slides = format_slides(snap_normal)
    hw_slide = next(s for s in slides if s["tag"] == "HW")
    assert "56°C" in hw_slide["text"]
    assert "1100rpm" in hw_slide["text"]
    assert "Hotspot 58°C" in hw_slide["detail"]
    assert "Mem 76°C" in hw_slide["detail"]
    assert "Fan 1100 RPM" in hw_slide["detail"]
    assert hw_slide["color"] == "#38bdf8"

    # 2. Test high hotspot alert (>= HOTSPOT_ALERT_C, 95°C: 85 alarmed on every AI run) in format_slides
    snap_hot = Snapshot(
        gpu_name="AMD Radeon RX 9070 XT",
        gpu_load=90.0,
        gpu_temp=75,
        gpu_hotspot=97,
        gpu_fan_rpm=2200,
        vram_used_gb=12.0,
        vram_total_gb=16.0,
        sampled_at=102.0,
    )
    slides_hot = format_slides(snap_hot)
    hw_hot = next(s for s in slides_hot if s["tag"] == "HW")
    assert "🔥97°C!" in hw_hot["text"]
    assert "🔥" not in format_slides(Snapshot(gpu_temp=70, gpu_hotspot=90, sampled_at=1.0))[0]["text"]   # normal under load
    assert hw_hot["color"] == "#f87171"  # Alert red

    # 3. Test multi-line rows formatting
    rows = format_multiline_rows(snap_normal)
    hw_row = next(r for r in rows if r["title"] == "HARDWARE")
    items_dict = {k: v for k, v, _ in hw_row["items"]}
    assert "56°C/58°C" in items_dict["GPU"]
    assert items_dict["Fan"] == "1100 RPM"

    # 4. Test _check_events alert triggering on hotspot crossing 85°C
    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        alerts = []
        app.trigger_alert = lambda col, evt: alerts.append((col, evt))

        # From normal to hot triggers alert
        app._check_events(snap_normal, snap_hot)
        assert any(evt == "gpu_hotspot_alert" for _, evt in alerts)

        # Remaining hot does not trigger duplicate alert
        alerts.clear()
        app._check_events(snap_hot, snap_hot)
        assert not any(evt == "gpu_hotspot_alert" for _, evt in alerts)
    finally:
        root.destroy()


def test_media_collector_and_slides(monkeypatch, tmp_path):
    import tkinter as tk
    from sol_control_hud.views import ticker
    from sol_control_hud.data import snapshot as ticker_data
    from sol_control_hud.data.collectors.media import clean_app_name, MediaInfo
    from sol_control_hud.data.snapshot import Snapshot, format_slides

    # 1. Test clean_app_name helper
    assert clean_app_name("Spotify.exe") == "Spotify"
    assert clean_app_name("Brave._crx_agimnkijcamfeangaknmldooml") == "Brave"
    assert clean_app_name("msedge.exe") == "Edge"
    assert clean_app_name("Firefox") == "Firefox"
    assert clean_app_name("foobar2000.exe") == "foobar2000"
    assert clean_app_name(None) == "Media"

    # 2. Test format_slides when media is Playing
    snap_playing = Snapshot(
        media_status="Playing",
        media_title="Voyager",
        media_artist="Daft Punk",
        media_app="Spotify",
        media_playing=True,
    )
    slides = format_slides(snap_playing)
    media_slide = next((s for s in slides if s["tag"] == "MEDIA"), None)
    assert media_slide is not None
    assert "▶ Voyager — Daft Punk · Spotify" in media_slide["text"]
    assert media_slide["color"] == "#4ade80"

    # 3. Test format_slides when media is Paused
    snap_paused = Snapshot(
        media_status="Paused",
        media_title="Voyager",
        media_artist="Daft Punk",
        media_app="Spotify",
        media_playing=False,
    )
    slides_p = format_slides(snap_paused)
    media_p = next((s for s in slides_p if s["tag"] == "MEDIA"), None)
    assert media_p is not None
    assert "⏸ Voyager — Daft Punk (paused)" in media_p["text"]
    assert media_p["color"] == "#94a3b8"

    # 4. Test format_slides when no media is active
    snap_none = Snapshot(media_status="none", media_title=None)
    slides_n = format_slides(snap_none)
    assert not any(s["tag"] == "MEDIA" for s in slides_n)

    # 5. Test click target focus invocation
    focused = []
    import sol_control_hud.data.collectors.media as m_mod
    monkeypatch.setattr(m_mod, "focus_media_app", lambda app, title: focused.append((app, title)))

    fake_settings = tmp_path / "ticker-settings.json"
    fake_trigger = tmp_path / "ticker-activate.trigger"
    monkeypatch.setattr(ticker, "SETTINGS_FILE", fake_settings)
    monkeypatch.setattr(ticker, "TRIGGER_FILE", fake_trigger)

    root = tk.Tk()
    root.withdraw()
    try:
        app = ticker.TickerApp(root)
        app.latest_snap = snap_playing
        app._open_slide_target("MEDIA")
        assert ("Spotify", "Voyager") in focused
    finally:
        root.destroy()












def test_switching_ai_mode_from_the_ticker_really_runs_the_script(monkeypatch, tmp_path):
    """Regression (found 10-08 by pyflakes during the file split): ticker.py never imported `subprocess` (since its first
    commit, 7b4de81), and the NameError was swallowed by the runner's `except Exception`, so Desk/Away from the ticker
    silently did nothing."""
    import types
    from sol_control_hud.views import ticker
    script = tmp_path / "sol-llm.ps1"
    script.write_text("# fake")
    monkeypatch.setattr(ticker, "SOL_LLM", script)
    monkeypatch.setattr(ticker, "threading", types.SimpleNamespace(Thread=lambda target, daemon: types.SimpleNamespace(start=target)))
    ran = []
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: ran.append(cmd))
    me = types.SimpleNamespace(trigger_alert=lambda *a: None, refresh_data_now=lambda: None)
    ticker.TickerApp.switch_ai_mode(me, "away")
    assert ran and ran[0][-2:] == [str(script), "away"]
