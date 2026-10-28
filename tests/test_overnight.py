"""Tests for overnight chain batch orchestration and display control."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sol_control_hud.away import display_ctl
from sol_control_hud.chains import overnight


def test_display_ctl_keep_awake():
    with patch("ctypes.windll.kernel32.SetThreadExecutionState", return_value=1) as mock_set:
        assert display_ctl.keep_pc_awake() is True
        mock_set.assert_called_once()

    with patch("ctypes.windll.kernel32.SetThreadExecutionState", return_value=1) as mock_release:
        assert display_ctl.release_pc_awake() is True
        mock_release.assert_called_once()


def test_display_ctl_turn_monitors_off():
    with patch("ctypes.windll.user32.SendMessageW", return_value=0) as mock_send, \
         patch.object(display_ctl, "keep_pc_awake", return_value=True):
        assert display_ctl.turn_monitors_off() is True
        mock_send.assert_called_once_with(
            display_ctl.HWND_BROADCAST,
            display_ctl.WM_SYSCOMMAND,
            display_ctl.SC_MONITORPOWER,
            display_ctl.MONITOR_OFF,
        )


def test_overnight_state_lifecycle(tmp_path: Path):
    state_file = tmp_path / "overnight.json"
    with patch.object(overnight, "OVERNIGHT_STATE", state_file):
        # Empty / initial
        st = overnight.get_overnight_state()
        assert st["active"] is False

        # Save & read
        overnight.save_overnight_state({"active": True, "current": "Chain 1"})
        st2 = overnight.get_overnight_state()
        assert st2["active"] is True
        assert st2["current"] == "Chain 1"


def test_start_and_advance_overnight(tmp_path: Path):
    state_file = tmp_path / "overnight.json"
    chains_dir = tmp_path / "Chains"
    reports_dir = tmp_path / "Reports"
    chains_dir.mkdir()
    reports_dir.mkdir()

    # Create dummy chain notes
    (chains_dir / "Chain A.md").write_text("---\ntype: chain\nstatus: done\n---\n", encoding="utf-8")
    (chains_dir / "Chain B.md").write_text("---\ntype: chain\nstatus: done\n---\n", encoding="utf-8")

    with patch.object(overnight, "OVERNIGHT_STATE", state_file), \
         patch.object(overnight, "REPORTS_DIR", reports_dir), \
         patch.object(overnight, "LLM_DIR", tmp_path), \
         patch.object(overnight.chain_note, "CHAINS_DIR", chains_dir), \
         patch.object(display_ctl, "turn_monitors_off", return_value=True), \
         patch.object(display_ctl, "release_pc_awake", return_value=True) as released:

        # Start with 2 chains
        res = overnight.start_overnight(["Chain A", "Chain B"], display_mode="dpms_off", chains_dir=chains_dir)
        assert res["ok"] is True
        st = overnight.get_overnight_state()
        assert st["active"] is True
        assert st["current"] == "Chain A"
        assert st["queue"] == ["Chain B"]

        # start queued Chain A: while it's queued (or running) the batch must wait, not "finish" on the first tick
        assert "status: queued" in (chains_dir / "Chain A.md").read_text(encoding="utf-8")
        st_wait = overnight.check_and_advance(chains_dir=chains_dir)
        assert st_wait["active"] is True and st_wait["current"] == "Chain A" and st_wait["completed"] == []
        (chains_dir / "Chain A.md").write_text("---\ntype: chain\nstatus: running\n---\n", encoding="utf-8")
        assert overnight.check_and_advance(chains_dir=chains_dir)["current"] == "Chain A"
        assert not released.called

        # Chain A done: advance to Chain B (which gets queued)
        (chains_dir / "Chain A.md").write_text("---\ntype: chain\nstatus: done\n---\n", encoding="utf-8")
        st_adv = overnight.check_and_advance(chains_dir=chains_dir)
        assert st_adv["current"] == "Chain B"
        assert "Chain A" in st_adv["completed"]
        assert st_adv["queue"] == []

        # Chain B was queued by the advance: wait for it, then the batch completes
        assert overnight.check_and_advance(chains_dir=chains_dir)["current"] == "Chain B"
        (chains_dir / "Chain B.md").write_text("---\ntype: chain\nstatus: done\n---\n", encoding="utf-8")
        st_final = overnight.check_and_advance(chains_dir=chains_dir)
        assert st_final["active"] is False
        assert st_final["status"] == "completed"
        assert "Chain B" in st_final["completed"]

        # Report file should have been generated
        reports = list(reports_dir.glob("night-*.md"))
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "Night Run Summary" in content
        assert "Chain A" in content
        assert "Chain B" in content


def test_cancel_overnight(tmp_path: Path):
    state_file = tmp_path / "overnight.json"
    with patch.object(overnight, "OVERNIGHT_STATE", state_file), \
         patch.object(display_ctl, "release_pc_awake", return_value=True):
        overnight.save_overnight_state({"active": True, "current": "Chain A", "queue": ["Chain B"]})
        res = overnight.cancel_overnight()
        assert res["ok"] is True
        st = overnight.get_overnight_state()
        assert st["active"] is False
        assert st["status"] == "cancelled"
        assert st["queue"] == []
        assert st["current"] is None


def test_set_bedtime_config(tmp_path: Path):
    state_file = tmp_path / "overnight.json"
    with patch.object(overnight, "OVERNIGHT_STATE", state_file):
        res = overnight.set_bedtime_config(enabled=True, chains=["Chain X", "Chain Y"])
        assert res["ok"] is True
        assert "armed" in res["why"]

        st = overnight.get_overnight_state()
        assert st["bedtime_autostart"] is True
        assert st["selected_chains"] == ["Chain X", "Chain Y"]

        res2 = overnight.set_bedtime_config(enabled=False)
        assert res2["ok"] is True
        assert "disabled" in res2["why"]
        assert overnight.get_overnight_state()["bedtime_autostart"] is False


def test_trigger_bedtime_run(tmp_path: Path):
    state_file = tmp_path / "overnight.json"
    chains_dir = tmp_path / "Chains"
    chains_dir.mkdir()
    (chains_dir / "Chain X.md").write_text("---\ntype: chain\n---\n", encoding="utf-8")

    with patch.object(overnight, "OVERNIGHT_STATE", state_file), \
         patch.object(display_ctl, "keep_pc_awake", return_value=True):

        # When disabled, trigger fails
        overnight.save_overnight_state({"bedtime_autostart": False, "selected_chains": ["Chain X"]})
        res = overnight.trigger_bedtime_run(chains_dir=chains_dir)
        assert res["ok"] is False
        assert "toggled off" in res["why"]

        # When enabled, trigger starts batch with display_mode="none"
        overnight.save_overnight_state({"bedtime_autostart": True, "selected_chains": ["Chain X"]})
        res2 = overnight.trigger_bedtime_run(chains_dir=chains_dir)
        assert res2["ok"] is True
        assert res2["triggered_by"] == "monitor_off"

        st = overnight.get_overnight_state()
        assert st["active"] is True
        assert st["current"] == "Chain X"
        assert st["display_mode"] == "none"

        # Already active: subsequent trigger reports already active
        res3 = overnight.trigger_bedtime_run(chains_dir=chains_dir)
        assert res3["ok"] is False
        assert "already active" in res3["why"]


def test_check_monitor_off_trigger_latching(tmp_path: Path):
    state_file = tmp_path / "overnight.json"
    chains_dir = tmp_path / "Chains"
    chains_dir.mkdir()
    (chains_dir / "Chain X.md").write_text("---\ntype: chain\n---\n", encoding="utf-8")

    with patch.object(overnight, "OVERNIGHT_STATE", state_file), \
         patch.object(display_ctl, "keep_pc_awake", return_value=True):

        overnight.save_overnight_state({"bedtime_autostart": True, "selected_chains": ["Chain X"]})

        # When monitor off is inactive, returns None and unlatches
        with patch.object(overnight, "is_monitor_off_active", return_value=False):
            assert overnight.check_monitor_off_trigger(chains_dir=chains_dir) is None

        # When monitor off becomes active, fires once
        with patch.object(overnight, "is_monitor_off_active", return_value=True):
            res = overnight.check_monitor_off_trigger(chains_dir=chains_dir)
            assert res is not None
            assert res["ok"] is True

            # Subsequent check while still active latches and returns None
            res2 = overnight.check_monitor_off_trigger(chains_dir=chains_dir)
            assert res2 is None

