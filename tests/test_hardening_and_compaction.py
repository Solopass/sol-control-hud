"""Tests for structure-preserving context compaction and system stability hardening."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sol_control_hud.chains.runner import compact_text, Runner
from sol_control_hud.chains.workflow import Step
from sol_control_hud.notify import Notifier
from sol_control_hud.chains.solang_tools import StateManager
from sol_control_hud.chains.file_tools import archive_note
from sol_control_hud.metrics import Metrics
from sol_control_hud.data.collectors.adl import AdlReader, close_adl
from sol_control_hud.data.collectors import engines
from sol_control_hud.views import ticker


def test_compact_text_preserves_code_fences():
    # Long text with an open code block
    text = (
        "# Header\n\nSome introductory content that takes up space.\n\n"
        "```python\ndef calculate_score(items):\n    # Many lines of code\n"
        + "\n".join(f"    val_{i} = {i} * 2" for i in range(100))
        + "\n    return sum(val_0)\n"
    )
    compacted = compact_text(text, max_chars=200)
    assert len(compacted) <= 220
    # Must have matched/even number of triple backticks
    assert compacted.count("```") % 2 == 0
    assert "[Compact memory:" in compacted


def test_fit_model_preflight_compaction():
    runner = Runner(store=MagicMock(), client=MagicMock())
    step = Step(id="step1", prompt="prompt", max_tokens=256)
    messages = [{"role": "user", "content": "X" * 15000}]

    def mock_fits(model, msgs, max_tokens):
        content_len = len(msgs[0]["content"])
        if content_len > 6000:
            return False, 5000, 2048
        return True, 1000, 2048

    runner.client.fits = mock_fits
    chosen = runner._fit_model(run_id=1, step=step, model="sol-fast", messages=messages, task="test")
    assert chosen == "sol-fast"
    assert len(messages[0]["content"]) < 15000
    assert "[Compact memory:" in messages[0]["content"]


def test_notifier_held_bounded_cap(tmp_path: Path):
    from sol_control_hud.notify import Notice
    for name in ("away.jsonl", "chains.log"):
        (tmp_path / name).write_text("")
    notifier = Notifier(
        settings={"enabled": True},
        away_log=tmp_path / "away.jsonl",
        chains_log=tmp_path / "chains.log",
        clock=lambda: 1000.0,
    )
    # Feed 150 notices into held queue while busy
    extra_notices = [Notice("media_stuck", f"Title {i}", f"Text {i}", "warning", f"key_{i}") for i in range(150)]
    notifier.check(None, None, busy=True, extra=extra_notices)
    assert len(notifier.held) == 100
    assert notifier.held[0].key == "key_50"
    assert notifier.held[-1].key == "key_149"


def test_state_manager_atomic_save(tmp_path: Path):
    state_file = tmp_path / "state.json"
    StateManager.save(state_file, {"key1": "val1"})
    assert state_file.exists()
    assert not (tmp_path / "state.tmp").exists()

    loaded = StateManager.load(state_file)
    assert loaded.get("key1") == "val1"


def test_archive_note_atomic_write(tmp_path: Path):
    from sol_control_hud.chains.file_tools import Policy
    src = tmp_path / "note.md"
    src.write_text("Hello Obsidian note content", encoding="utf-8")
    archive_dir = tmp_path / "Archive"

    test_policy = Policy(read_roots=[tmp_path], write_roots=[tmp_path], repo_roots=[tmp_path])
    res = archive_note({"path": str(src), "dest_folder": str(archive_dir)}, policy=test_policy)
    dest = Path(res["archived"])
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "Archived Note" in content
    assert "Hello Obsidian note content" in content
    assert not src.exists()
    assert not (dest.with_suffix(".tmp")).exists()


def test_metrics_db_schema_and_lock(tmp_path: Path):
    db_file = tmp_path / "metrics.db"
    db = Metrics(db_file)
    # Verify table schema has 17 columns
    with sqlite3.connect(db_file) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(minutes)").fetchall()]
        assert "spilled" in cols
        assert "tps" in cols
        assert len(cols) == 17

    # Verify write synchronization lock exists and works
    assert isinstance(db._lock, type(threading.Lock()))
    db.close()


def test_adl_close_cleans_resources():
    reader = AdlReader()
    reader._initialized = True
    reader._supported = True
    reader._allocations = [MagicMock()]
    reader.close()
    assert reader._initialized is False
    assert reader._supported is False
    assert len(reader._allocations) == 0

    # Test module-level convenience function
    close_adl()


def test_engines_close_client():
    client1 = engines._client()
    assert client1 is not None
    engines.close_client()
    assert engines._HTTP is None


def test_ticker_release_instance_lock(tmp_path: Path):
    lock_file = tmp_path / "ticker.lock"
    with patch.object(ticker, "LOCK_FILE", lock_file):
        f = ticker.acquire_instance_lock()
        assert f is not None
        # Release explicit file
        ticker.release_instance_lock(f)
        # Should be able to acquire again
        f2 = ticker.acquire_instance_lock()
        assert f2 is not None
        ticker.release_instance_lock(f2)
