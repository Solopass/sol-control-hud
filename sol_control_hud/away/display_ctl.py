"""Display and power control for overnight and away runs.
Allows physical DPMS monitor standby while preventing system sleep,
or launching the low-emission Away progress screen.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_OFF = 2
MONITOR_LOW_POWER = 1
MONITOR_ON = -1

_execution_held = False


def keep_pc_awake(allow_display_sleep: bool = True) -> bool:
    """Keeps the system awake so background AI jobs don't sleep the PC.
    If allow_display_sleep=True, monitors are permitted to turn off."""
    global _execution_held
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if not allow_display_sleep:
        flags |= ES_DISPLAY_REQUIRED
    try:
        res = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        _execution_held = bool(res != 0)
        return _execution_held
    except Exception:
        return False


def release_pc_awake() -> bool:
    """Restores default Windows system sleep behavior."""
    global _execution_held
    try:
        res = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        _execution_held = False
        return bool(res != 0)
    except Exception:
        return False


def turn_monitors_off() -> bool:
    """Sends display standby message to power down physical screens."""
    try:
        keep_pc_awake(allow_display_sleep=True)
        ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
        return True
    except Exception:
        return False


def launch_away_screen() -> bool:
    """Spawns the dark, OLED-safe Away progress screen in background."""
    try:
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        if not Path(pyw).exists():
            pyw = sys.executable
        subprocess.Popen([pyw, "-m", "sol_control_hud.away.screen"],
                         creationflags=0x08000000,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
