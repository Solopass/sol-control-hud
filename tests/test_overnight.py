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
         patch.object(display_ctl, "turn_monitors_off", return_value=True), \
         patch.object(display_ctl, "release_pc_awake", return_value=True):

        # Start with 2 chains
        res = overnight.start_overnight(["Chain A", "Chain B"], display_mode="dpms_off", chains_dir=chains_dir)
        assert res["ok"] is True
        st = overnight.get_overnight_state()
        assert st["active"] is True
        assert st["current"] == "Chain A"
        assert st["queue"] == ["Chain B"]

        # Advance: since Chain A is marked 'done', it should advance to Chain B
        st_adv = overnight.check_and_advance(chains_dir=chains_dir)
        assert st_adv["current"] == "Chain B"
        assert "Chain A" in st_adv["completed"]
        assert st_adv["queue"] == []

        # Advance again: Chain B is marked 'done', batch completes
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
