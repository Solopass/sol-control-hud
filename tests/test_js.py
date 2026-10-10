"""Runs the dashboard's JavaScript tests (tests/js/*.test.js) with node in WSL, as part of the normal test run.
Skipped, not failed, when WSL or node isn't there."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mnt(p: Path) -> str:
    drive, rest = p.drive.rstrip(":").lower(), p.as_posix().split(":", 1)[1]
    return f"/mnt/{drive}{rest}"


def _node_ok() -> bool:
    if not shutil.which("wsl.exe"):
        return False
    try:
        return subprocess.run(["wsl.exe", "-e", "node", "--version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _node_ok(), reason="node in WSL not available")
def test_dashboard_js():
    files = sorted((ROOT / "tests" / "js").glob("*.test.js"))
    assert files, "no JS tests found"
    r = subprocess.run(["wsl.exe", "-e", "node", "--test", *[_mnt(f) for f in files]], capture_output=True, timeout=120)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, out[-3000:]
    assert "# fail 0" in out, out[-3000:]
