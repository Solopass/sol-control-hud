"""Open a note in Obsidian and bring Obsidian to the front.

Why: the HUD runs in the background, and Windows only lets the app you're using move another window to the front
(the foreground lock). So `os.startfile("obsidian://...")` from the HUD opened the note but left Obsidian behind the
browser, flashing in the taskbar. Here the HUD waits for Obsidian's window to show the note (its title is
"<note> - <vault> - Obsidian <version>") and then brings that window forward the accepted way: it briefly joins the
foreground window's input queue, which lifts the lock for that call, and restores the window if it was minimized.

A file inside a known vault is opened by its obsidian:// link rather than its path: `.md` has no file association
on this PC, so opening the path asked Windows "how do you want to open this?".
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
import urllib.parse
from ctypes import wintypes
from pathlib import Path

import psutil

from .swallow import note as _swallowed

WAIT_S = 8.0                 # Obsidian not running: it starts, loads the vault, then opens the note
POLL_S = 0.15
SW_RESTORE = 9

_user32 = ctypes.windll.user32 if os.name == "nt" else None
_kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None


def note_uri(vault: str, rel_path: str) -> str:
    return (f"obsidian://open?vault={urllib.parse.quote(vault)}"
            f"&file={urllib.parse.quote(rel_path.replace(chr(92), '/'))}")


def uri_for_path(path: Path, vaults: dict[str, Path]) -> tuple[str, str, str] | None:
    """(uri, vault, note stem) when `path` is inside one of `vaults`, else None."""
    try:
        target = Path(path).resolve()
    except OSError:
        return None
    for name, root in vaults.items():
        try:
            rel = target.relative_to(Path(root).resolve())
        except (ValueError, OSError):
            continue
        rel_s = rel.as_posix()
        if rel_s.lower().endswith(".md"):
            rel_s = rel_s[:-3]
        return note_uri(name, rel_s), name, target.stem
    return None


def obsidian_windows() -> list[tuple[int, str]]:
    """Visible top-level Obsidian windows, front-most first: (hwnd, title)."""
    if _user32 is None:
        return []
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == "obsidian.exe"}
    if not pids:
        return []
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                n = _user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, buf, n + 1)
                    found.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(each, 0)
    return found


def pick(windows: list[tuple[int, str]], note: str | None, vault: str | None) -> tuple[int | None, bool]:
    """The window to bring forward, and whether it already shows the note ("<note> - <vault> - Obsidian ...")."""
    if note:
        for hwnd, title in windows:
            if title.startswith(f"{note} - ") and (not vault or f" - {vault} - " in title):
                return hwnd, True
    if vault:
        for hwnd, title in windows:
            if f" - {vault} - " in title or title.startswith(f"{vault} - "):
                return hwnd, False
    return (windows[0][0], False) if windows else (None, False)


KEYEVENTF_KEYUP = 0x0002
VK_NONAME, VK_MENU = 0xFC, 0x12     # an unused key code (no effect anywhere), then Alt as the last resort


def _try_front(hwnd: int) -> bool:
    fg = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = _kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and _user32.AttachThreadInput(me, fg_thread, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(me, fg_thread, False)
    return _user32.GetForegroundWindow() == hwnd


def bring_to_front(hwnd: int) -> str:
    """Restore if minimized and make it the foreground window. Returns how ("join-input", "unused-key", "alt") or ""
    when Windows refused every way.

    Windows lets a process take the foreground only if it got the last input event. Joining the foreground thread's
    input (tested 10-08) is not enough from a background process: with YouTube in front, Obsidian stayed behind. So
    when that fails, the HUD first sends itself a key event - an unused key code, then a bare Alt tap if Windows
    ignores that - which counts as its own input, and asks again."""
    if _user32 is None or not hwnd:
        return ""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    if _try_front(hwnd):
        return "join-input"
    for vk, how in ((VK_NONAME, "unused-key"), (VK_MENU, "alt")):
        _user32.keybd_event(vk, 0, 0, 0)
        _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        if _try_front(hwnd):
            return how
    return ""


def focus_when_ready(note: str | None, vault: str | None, wait_s: float = WAIT_S, clock=time.monotonic,
                     sleep=time.sleep, windows=obsidian_windows, front=bring_to_front):
    """Wait until Obsidian shows the note (or the time is up), then bring the best window forward. Returns how it
    got to the front (truthy), "" when Windows refused, False when no Obsidian window appeared."""
    deadline = clock() + wait_s
    hwnd = None
    while clock() < deadline:
        hwnd, showing = pick(windows(), note, vault)
        if showing:
            break
        sleep(POLL_S)
    return bool(hwnd) and front(hwnd)


def open_uri(uri: str, note: str | None, vault: str | None) -> None:
    """Hand the link to Obsidian, then (in the background) bring it to the front."""
    os.startfile(uri)   # noqa: S606 - an obsidian:// link built here from a vault name and a checked path

    def work():
        try:
            how = focus_when_ready(note, vault)
            from .hub import log                  # the HUD's own log: which way worked, or that none did
            log(f"obsidian to front: {how or ('refused by Windows' if how == '' else 'no Obsidian window')} ({note})")
        except Exception:
            _swallowed("obsidian_open.open_uri")
    threading.Thread(target=work, name="obsidian-focus", daemon=True).start()


def open_path(path: Path) -> bool:
    """Open a note file in Obsidian when it lives in a known vault (True), else with Windows' default (False)."""
    from .data.collectors.notes import discover_vaults
    hit = uri_for_path(path, discover_vaults())
    if hit:
        uri, vault, stem = hit
        open_uri(uri, stem, vault)
        return True
    os.startfile(str(path))   # noqa: S606 - a path the caller already checked
    return False
