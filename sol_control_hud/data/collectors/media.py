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
from ...paths import DATA_DIR
from ...swallow import note as _swallowed

SCRIPT_PATH = Path(__file__).resolve().parent / "media_session.ps1"
# The same reader as a small Go binary (D:\Workspace\sol-net, cmd/sol-media; 2026-11-07): same flags and output,
# ~19 MB instead of a ~127 MB PowerShell host. Used when it exists; the .ps1 stays as the fallback.
EXE_PATH = Path(os.environ.get("SOL_MEDIA_EXE", r"D:\Workspace\sol-net\bin\sol-media.exe"))
EXE_QUICK_FAIL_S = 10.0    # the exe ended this soon without printing a line: a failed start (blocked, broken)
EXE_MAX_QUICK_FAILS = 3    # after this many in a row, use the .ps1 until the HUD restarts
# Media-key router (2026-11-07): the exe, run with -Keys, holds play/pause/next/previous/stop and sends each press to
# the session picked in the ticker menu (Media Keys). The pick lives in this file and is read on every key press.
# Targets: "youtube" (the YouTube app in Brave; default), "auto" (what plays, else Windows' choice), "app:<app id>".
# The .ps1 fallback can't route keys: then they work the Windows way.
TARGET_FILE = DATA_DIR / "media-target.json"
TARGET_YOUTUBE, TARGET_AUTO = "youtube", "auto"


YOUTUBE_APP_ID = "agimnkijcaahngcdmfeangaknmldooml"   # YouTube's installed web app; part of its session's app id


def session_label(app: str | None) -> str:
    """A media session's app, for menus: the YouTube app by name, everything else as clean_app_name does."""
    return "YouTube app (Brave)" if YOUTUBE_APP_ID in (app or "").lower() else clean_app_name(app)


def media_keys_menu(info: "MediaInfo", target: str) -> list[tuple[str | None, str | None]]:
    """The ticker's Media Keys submenu as (label, target) rows: target None = a disabled info line, label None = a
    separator. The current pick has a check mark."""
    def mark(value: str) -> str:
        return "✓ " if value == target else "   "
    rows: list[tuple[str | None, str | None]] = [
        (f"{mark(TARGET_YOUTUBE)}YouTube app (Brave)  ·  default", TARGET_YOUTUBE),
        (f"{mark(TARGET_AUTO)}Automatic (whatever is playing)", TARGET_AUTO),
    ]
    apps, seen = [], set()
    for s in info.sessions:
        app = s.get("app") or ""
        if app.lower() in seen or YOUTUBE_APP_ID in app.lower():
            continue                                  # the YouTube app is the first row already
        seen.add(app.lower())
        title = (s.get("title") or "").strip()
        title = f" · {title[:40]}" if title else ""
        apps.append((f"{mark('app:' + app)}{session_label(app)}{title}  ({s.get('status') or '?'})", "app:" + app))
    picked = target.startswith("app:") and target[4:].lower() not in seen
    if apps or picked:
        rows.append((None, None))
        rows.append(("Send them to:", None))
        rows.extend(apps)
        if picked:                                    # the picked app isn't running now: still show the pick
            rows.append((f"✓ {session_label(target[4:])}  (not running: keys go to Automatic)", target))
    rows.append((None, None))
    if info.keys == "taken":
        rows.append(("⚠ Another program holds the media keys", None))
    elif info.keys != "on":
        rows.append(("Router off: keys work the Windows way", None))
    if info.last_key:
        rows.append((f"Last: {info.last_key}", None))
    return rows


def get_media_target(path: Path | None = None) -> str:
    try:
        return json.loads((path or TARGET_FILE).read_text(encoding="utf-8")).get("target") or TARGET_YOUTUBE
    except (OSError, ValueError, AttributeError):
        return TARGET_YOUTUBE


def set_media_target(target: str, path: Path | None = None) -> None:
    p = path or TARGET_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"target": target}), encoding="utf-8")
    os.replace(tmp, p)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class MediaInfo:
    status: str = "none"  # "Playing", "Paused", "Stopped", "none"
    title: str | None = None
    artist: str | None = None
    app: str | None = None
    clean_app: str | None = None
    playing: bool = False
    # from the exe's key router only (empty with the .ps1)
    keys: str = ""                    # "on", "taken" (another program holds the keys), "" (router not running)
    target: str = ""
    last_key: str = ""                # "08:40:03 play/pause -> <app id>"
    sessions: tuple = ()              # ({"app", "title", "status"}, ...) every media session Windows has


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
    sessions = tuple(s for s in (data.get("sessions") or ()) if isinstance(s, dict) and s.get("app"))
    return MediaInfo(status=status, title=data.get("title"), artist=data.get("artist"), app=raw_app,
                     clean_app=clean_app_name(raw_app), playing=status == "Playing",
                     keys=data.get("keys") or "", target=data.get("target") or "",
                     last_key=data.get("last_key") or "", sessions=sessions)


class MediaCollector:
    """Polls Windows Media Control sessions in a background worker thread."""

    def __init__(self, interval: float = 3.5, exe_path: Path | None = None, script_path: Path | None = None):
        self.interval = interval
        self.exe_path = EXE_PATH if exe_path is None else exe_path
        self.script_path = SCRIPT_PATH if script_path is None else script_path
        self.target_file = TARGET_FILE
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest = MediaInfo()
        self._exe_fails = 0

    def using_exe(self) -> bool:
        return self._exe_fails < EXE_MAX_QUICK_FAILS and self.exe_path.exists()

    def command(self, every: float | None = None) -> list[str] | None:
        """The reader to run: the Go exe while it works, else the .ps1; None when neither exists.
        The long-running exe also routes the media keys (-Keys)."""
        exe = self.using_exe()
        if exe:
            cmd = [str(self.exe_path)]
        elif self.script_path.exists():
            cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script_path)]
        else:
            return None
        if every is not None:
            cmd += ["-Every", str(every), "-ParentPid", str(os.getpid())]
            if exe:
                cmd += ["-Keys", "-Target", str(self.target_file)]
        return cmd

    def note_exit(self, was_exe: bool, ran_s: float, lines: int) -> None:
        """After a reader ends: count quick failures of the exe; one good run resets the count."""
        if not was_exe:
            return
        if lines == 0 and ran_s < EXE_QUICK_FAIL_S:
            self._exe_fails += 1
            if self._exe_fails == EXE_MAX_QUICK_FAILS:
                try:   # swallow.note records the exception being handled, so give it one: logged once, now on the .ps1
                    raise RuntimeError(f"{self.exe_path.name} failed to start {EXE_MAX_QUICK_FAILS}x; using the .ps1")
                except RuntimeError:
                    _swallowed("media.MediaCollector.exe_fallback")
        else:
            self._exe_fails = 0

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
        """One sample from a fresh reader (tests / one-off use; the collector keeps one process running)."""
        cmd = self.command()
        if cmd is None:
            return MediaInfo()
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=5, creationflags=NO_WINDOW)
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
        while not self._stop.is_set():
            was_exe = self.using_exe()
            cmd = self.command(self.interval)
            if cmd is None:
                return
            started, lines = time.monotonic(), 0
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    if line.strip():
                        lines += 1
                        self._store(parse_line(line.strip()))
            except Exception:
                _swallowed("media.MediaCollector._run")
            finally:
                self._kill()
            self.note_exit(was_exe, time.monotonic() - started, lines)
            # a failed exe start is retried soon (it falls back to the .ps1 after a few); a reader that ran: 15 s
            self._stop.wait(2.0 if was_exe and lines == 0 else 15.0)

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
