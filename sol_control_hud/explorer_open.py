"""Open a folder in Explorer and bring that window to the front.

Why: the HUD runs in the background, so Windows' foreground lock left a folder opened with `os.startfile` behind the
browser (only a flashing taskbar button), the same problem obsidian_open.py solves for Obsidian. Here the HUD notes
the Explorer windows that exist, opens the folder, waits for the new window (or, if Explorer reused one, a window
titled with the folder's name) and brings it forward with obsidian_open.bring_to_front.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .swallow import note as _swallowed

WAIT_S = 4.0
POLL_S = 0.1
REUSE_GRACE_S = 0.8          # only a title match (Explorer reused a window): give a new window this long to appear
EXPLORER_CLASS = "CabinetWClass"

_user32 = ctypes.windll.user32 if os.name == "nt" else None


def explorer_windows() -> list[tuple[int, str]]:
    """Visible Explorer folder windows, front-most first: (hwnd, title)."""
    if _user32 is None:
        return []
    found: list[tuple[int, str]] = []
    cls = ctypes.create_unicode_buffer(64)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if _user32.IsWindowVisible(hwnd) and _user32.GetClassNameW(hwnd, cls, 64) and cls.value == EXPLORER_CLASS:
            n = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(each, 0)
    return found


def pick(windows: list[tuple[int, str]], before: set[int], folder: Path) -> tuple[int | None, bool]:
    """(window to bring forward, whether it is surely ours). A window that wasn't there before wins; else one whose
    title is the folder's name or full path (Explorer reused an open window)."""
    for hwnd, _ in windows:
        if hwnd not in before:
            return hwnd, True
    names = {folder.name.lower(), str(folder).lower().rstrip("\\")}
    for hwnd, title in windows:
        t = title.lower()
        if t in names or t.split(" - ")[0] in names:
            return hwnd, False
    return None, False


def focus_when_ready(folder: Path, before: set[int], wait_s: float = WAIT_S, clock=time.monotonic, sleep=time.sleep,
                     windows=explorer_windows, front=None):
    """Wait for the folder's window, then bring it forward. Returns how (truthy), "" when Windows refused, False when
    no window was found."""
    if front is None:
        from .obsidian_open import bring_to_front as front
    start = clock()
    deadline = start + wait_s
    hwnd = None
    while clock() < deadline:
        found, sure = pick(windows(), before, folder)
        hwnd = found or hwnd                      # keep a candidate seen earlier
        if sure or (hwnd and clock() - start >= REUSE_GRACE_S):
            break                                 # a new window, or Explorer reused one: no need to wait it out
        sleep(POLL_S)
    return bool(hwnd) and front(hwnd)


def open_folder(path: Path | str) -> None:
    """Open the folder in Explorer, then (in the background) bring its window to the front."""
    folder = Path(path)
    before = {h for h, _ in explorer_windows()}
    os.startfile(str(folder))   # noqa: S606 - a folder the caller already checked

    def work():
        try:
            how = focus_when_ready(folder, before)
            from .hub import log
            log(f"explorer to front: {how or ('refused by Windows' if how == '' else 'no Explorer window')} ({folder.name})")
        except Exception:  # noqa: BLE001
            _swallowed("explorer_open.open_folder")
    threading.Thread(target=work, name="explorer-focus", daemon=True).start()
