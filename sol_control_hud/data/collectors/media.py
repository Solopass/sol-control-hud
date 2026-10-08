"""Windows Media Transport Controls (GSMTC) background collector.
Queries active media playback (Spotify, browser, media player) via native WinRT script.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from ...swallow import note as _swallowed

SCRIPT_PATH = Path(__file__).resolve().parent / "media_session.ps1"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class MediaInfo:
    status: str = "none"  # "Playing", "Paused", "Stopped", "none"
    title: str | None = None
    artist: str | None = None
    app: str | None = None
    clean_app: str | None = None
    playing: bool = False


def clean_app_name(raw: str | None) -> str:
    """Extracts a friendly human-readable name for the media application."""
    if not raw:
        return "Media"
    lower = raw.lower()
    if "spotify" in lower:
        return "Spotify"
    if "brave" in lower:
        return "Brave"
    if "chrome" in lower:
        return "Chrome"
    if "edge" in lower or "msedge" in lower:
        return "Edge"
    if "firefox" in lower:
        return "Firefox"
    if "vlc" in lower:
        return "VLC"
    if "foobar" in lower:
        return "foobar2000"
    if "zune" in lower or "wmplayer" in lower or "mediaplayer" in lower:
        return "Media Player"
    # Fallback: clean package prefix/suffix
    part = raw.split(".")[0].split("_")[0]
    return part.capitalize() if part else "Media"


def parse_line(line: str) -> MediaInfo:
    """One JSON line from media_session.ps1 -> MediaInfo (anything unreadable = nothing playing)."""
    try:
        data = json.loads(line)
    except ValueError:
        return MediaInfo()
    status = data.get("status") or "none"
    raw_app = data.get("app")
    return MediaInfo(status=status, title=data.get("title"), artist=data.get("artist"), app=raw_app,
                     clean_app=clean_app_name(raw_app), playing=status == "Playing")


class MediaCollector:
    """Polls Windows Media Control sessions in a background worker thread."""

    def __init__(self, interval: float = 3.5):
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest = MediaInfo()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="media-collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()

    def get_media_info(self) -> MediaInfo:
        with self._lock:
            return self._latest

    def poll_once(self) -> MediaInfo:
        """One sample from a fresh PowerShell (tests / one-off use; the collector keeps one process running)."""
        if not SCRIPT_PATH.exists():
            return MediaInfo()
        try:
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT_PATH)],
                capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
            )
            if res.returncode == 0 and res.stdout.strip():
                return self._store(parse_line(res.stdout.strip()))
        except Exception:
            _swallowed("media.MediaCollector.poll_once")
        return MediaInfo()

    def _store(self, info: MediaInfo) -> MediaInfo:
        with self._lock:
            self._latest = info
        return info

    def _run(self) -> None:
        # One long-running reader: it prints a line every `interval` s. Before 09-26 a new PowerShell started every
        # 3.5 s (~1000 processes an hour). If it dies, start it again after a pause.
        while not self._stop.is_set() and SCRIPT_PATH.exists():
            try:
                self._proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT_PATH),
                     "-Every", str(self.interval), "-ParentPid", str(os.getpid())],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    if line.strip():
                        self._store(parse_line(line.strip()))
            except Exception:
                _swallowed("media.MediaCollector._run")
            finally:
                self._kill()
            self._stop.wait(15.0)

    def _kill(self) -> None:
        p = getattr(self, "_proc", None)
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass


_COLLECTOR = MediaCollector()


def get_media_info() -> MediaInfo:
    """Helper to get latest media playback info without blocking."""
    return _COLLECTOR.get_media_info()


def start_media_collector() -> None:
    _COLLECTOR.start()


def stop_media_collector() -> None:
    _COLLECTOR.stop()


def focus_media_app(app_name: str | None = None, title: str | None = None) -> bool:
    """Attempts to bring the media player window into focus."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # Ensure attached to default desktop if needed
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)

        matched_hwnd = None
        target_app = (app_name or "").lower()
        target_title = (title or "").lower()

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_cb(hwnd, _):
            nonlocal matched_hwnd
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            w_title = buf.value.lower()

            if target_title and len(target_title) >= 4 and target_title[:12] in w_title:
                matched_hwnd = hwnd
                return False
            if target_app and target_app in w_title:
                matched_hwnd = hwnd
                return False
            return True

        cb = WNDENUMPROC(enum_cb)
        user32.EnumWindows(cb, 0)

        if matched_hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(matched_hwnd, SW_RESTORE)
            user32.SetForegroundWindow(matched_hwnd)
            return True
    except Exception:
        _swallowed("media.focus_media_app")
    return False
