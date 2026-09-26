"""Tests never touch the live app's data folder (settings, logs, the run history, the running marker).

Since the switch-over (2026-09-26) `data/` in this repo is the live machine's state: a test that saved ticker settings
there turned `docked` off in the real settings. `paths.DATA_DIR` is read once at import, so this has to be set before
anything from `sol_control_hud` is imported.
"""
import os
import tempfile

os.environ["SOL_CONTROL_DATA"] = tempfile.mkdtemp(prefix="sol-control-hud-tests-")
