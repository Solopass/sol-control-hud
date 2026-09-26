"""Ticker HUD fixes (2026-09-25): real GPU-lock state, no probing of socket-activated WSL services, crash badge."""
import subprocess
import sys
import textwrap
from pathlib import Path

from sol_control_hud.data import snapshot as td
from sol_control_hud.chains.gpulock import gpu_lock, is_held
from sol_control_hud.data.snapshot import Snapshot, format_multiline_rows, format_slides


def test_lock_is_held_only_while_a_process_holds_it(tmp_path):
    lock = tmp_path / "gpu.lock"
    assert not is_held(lock)                       # no file yet
    with gpu_lock("me", path=lock):
        pass
    assert lock.exists() and not is_held(lock)     # the file stays after use: that is NOT "held"
    code = textwrap.dedent(f"""
        import sys, time; sys.path.insert(0, {str(Path(__file__).parents[1])!r})
        from pathlib import Path
        from sol_control_hud.chains.gpulock import gpu_lock
        with gpu_lock("job", path=Path({str(lock)!r})):
            print("held", flush=True); time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        assert is_held(lock)
        assert is_held(lock)                       # checking doesn't take it away from the holder
    finally:
        p.kill(); p.wait()


def test_wsl_services_are_never_probed(monkeypatch):
    probed = []
    monkeypatch.setattr(td, "check_port", lambda port, **kw: probed.append(port) or True)
    monkeypatch.setattr(td, "chain_runner_alive", lambda: True)
    monkeypatch.setattr(td, "wsl_running", lambda: False)
    services = td.probe_services()
    assert sorted(probed) == [7900, 11440, 11443]            # media-api 8080 / speedman 8081 / voice 8765 / omni 8082: no
    assert services == {"Router": True, "Embed": True, "HUD": True, "Chains": True, "WSL": False}
    row = format_multiline_rows(Snapshot(services=services))[3]
    assert [(k, v) for k, v, _ in row["items"]][-1] == ("WSL", "off")


def test_unexpected_reboots_show_as_a_crash_badge():
    healthy = format_slides(Snapshot(vram_used_gb=5, vram_total_gb=16))[0]
    crashed = format_slides(Snapshot(vram_used_gb=5, vram_total_gb=16, unexpected_reboots=1))[0]
    assert "CRASH" not in healthy["text"] and healthy["color"] == "#38bdf8"
    assert "[CRASH x1!]" in crashed["text"] and crashed["color"] == "#f87171"
    assert "VRAM 5.0/16G" in crashed["text"]                  # the VRAM part is still there with a crash badge


def test_clamp_keeps_the_window_on_its_own_monitor():
    from sol_control_hud.views.ticker import clamp_rect
    main = (0, 0, 2560, 1440)
    assert clamp_rect(148, 1302, 450, 146, main, 1392 - 146) == (148, 1246)        # was over the taskbar / off-screen
    assert clamp_rect(2400, 100, 450, 146, main, 1246) == (2110, 100)               # off the right edge
    upper = (-528, -2160, 2032, 0)                                                  # the 4K monitor above the main one
    assert clamp_rect(0, -1200, 450, 146, upper, -146) == (0, -1200)                # allowed there, not pulled down
