"""SOL Dongle HUD - Ambient Taskbar Ticker & Multi-line Status Widget.
Docks in the bottom-left corner of the Windows desktop above the taskbar.
Cycles live hardware stats, local AI model state, chains progress, and local ports.
Supports both single-line ticker mode and multi-line tile mode with instant switching.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import webbrowser

import msvcrt
try:
    import winsound
except ImportError:
    winsound = None

from ..data.snapshot import (HOTSPOT_ALERT_C, Snapshot, TickerCollector, attention, format_multiline_rows, format_slides,
                          rotation)
from ..paths import DATA_DIR, ROOT


def play_alert_sound(sound_type: int | None = None) -> None:
    if winsound is None:
        return
    st = winsound.MB_ICONASTERISK if sound_type is None else sound_type
    try:
        threading.Thread(target=winsound.MessageBeep, args=(st,), daemon=True).start()
    except Exception:
        pass

# Constants & Paths
SETTINGS_FILE = DATA_DIR / "ticker-settings.json"
LOCK_FILE = DATA_DIR / "ticker.lock"
TRIGGER_FILE = DATA_DIR / "ticker-activate.trigger"
STARTUP_PATH = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SOL Control HUD.lnk"
HUD_WEB_URL = "http://127.0.0.1:7900"
WINDOW_TITLE = "SOL Ticker HUD"


LOG_FILE = DATA_DIR / "ticker.log"


def log_event(msg: str) -> None:
    """data\\ticker.log: start, exit (and why), errors. pythonw has no console, so without this a ticker that vanished
    left no trace (09-26)."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def acquire_instance_lock():
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not LOCK_FILE.exists() or LOCK_FILE.stat().st_size == 0:
            LOCK_FILE.write_bytes(b"1")
        f = open(LOCK_FILE, "r+b")
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except (OSError, IOError):
        return None


def activate_existing_instance() -> None:
    """Signals the existing running instance to reveal itself and brings it to foreground."""
    try:
        TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRIGGER_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

    try:
        hdesk = ctypes.windll.user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            ctypes.windll.user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            SW_RESTORE = 9
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            ctypes.windll.user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


ACK_FILE = Path(r"D:\OBVLT\reports\stability-ack.json")
REVIEWS_DIR = Path(r"D:\OBVLT\1Notebook\Reviews")
REPORTS_DIR = Path(r"D:\OBVLT\reports")
LLM_DIR = Path(r"D:\AI\Cache\llm")
SOL_LLM = Path(r"D:\OBVLT\tools\sol-llm.ps1")


def write_crash_ack(path: Path, now: str | None = None) -> dict:
    """Move acknowledged_until to now, keeping the earlier note (the crash history) after the new one."""
    from datetime import datetime
    stamp = now or datetime.now().isoformat(timespec="seconds")
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    note = f"{stamp[:16].replace('T', ' ')}: events up to now marked reviewed from the ticker."
    if old.get("note"):
        note += f" Earlier ({old.get('acknowledged_until', '?')}): {old['note']}"
    data = {"acknowledged_until": stamp, "note": note}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def is_startup_enabled() -> bool:
    return STARTUP_PATH.exists()


def set_startup(enable: bool) -> bool:
    if not enable:
        if STARTUP_PATH.exists():
            try:
                STARTUP_PATH.unlink()
                return True
            except OSError:
                return False
        return True
    else:
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            sc = shell.CreateShortcut(str(STARTUP_PATH))
            sc.TargetPath = str(ROOT / ".venv" / "Scripts" / "pythonw.exe")
            sc.Arguments = "-m sol_control_hud"   # the whole app (it opens your saved views)
            sc.WorkingDirectory = str(ROOT)
            sc.Description = "SOL Control HUD: taskbar ticker + dashboard"
            sc.IconLocation = str(ROOT / "sol_control_hud" / "assets" / "sol.ico")
            sc.Save()
            return True
        except Exception:
            return False

# UI Dimensions (in logical pixels)
WIDTH = 450
SINGLE_HEIGHT = 30
MULTI_HEIGHT = 168

# Available Font Scales (Curated for Custom DPI / Readability)
FONT_SCALES: dict[str, dict[str, int | str]] = {
    "small": {
        "name": "Small (8pt)",
        "font_single": 8,
        "font_row": 7,
        "width": 420,
        "single_h": 28,
        "multi_h": 155,
        "max_single_px": 265,
        "max_row_px": 330,
    },
    "normal": {
        "name": "Normal (9pt - Default)",
        "font_single": 9,
        "font_row": 8,
        "width": 450,
        "single_h": 30,
        "multi_h": 168,
        "max_single_px": 290,
        "max_row_px": 350,
    },
    "large": {
        "name": "Large (10pt)",
        "font_single": 10,
        "font_row": 9,
        "width": 490,
        "single_h": 34,
        "multi_h": 186,
        "max_single_px": 325,
        "max_row_px": 390,
    },
}
DEFAULT_FONT_SCALE = "normal"

# Models available on the llama.cpp router (http://127.0.0.1:11440)
AI_MODELS: list[tuple[str, str]] = [
    ("sol-fast", "sol-fast (Gemma 4 12B · Fast · Vision)"),
    ("sol-smart", "sol-smart (gpt-oss-20b · Reasoning)"),
    ("sol-long", "sol-long (gpt-oss-20b · 65k Context)"),
]

_START_PS = (
    "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes; "
    "$A = [System.Windows.Automation.AutomationElement]; $S = [System.Windows.Automation.TreeScope]; "
    "$tray = $A::RootElement.FindFirst($S::Children, (New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, 'Shell_TrayWnd'))); "
    "if ($tray) { $b = $tray.FindFirst($S::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::AutomationIdProperty, 'StartButton'))); "
    "if ($b) { 'start ' + [int]$b.Current.BoundingRectangle.Left } }")


def start_button_left(timeout: float = 10.0) -> int | None:
    """Where the Windows 11 taskbar's Start button (the first of the centered icons) begins, in screen pixels.
    Windows 11 draws its taskbar in XAML, so the buttons aren't windows: UI Automation is the supported way to ask.
    One short hidden PowerShell (~0.3 s); the ticker asks at start, when you dock it, and every 10 minutes."""
    import subprocess
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", _START_PS], capture_output=True, text=True,
                             timeout=timeout, creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("start "):
            try:
                return int(line.split()[1])
            except ValueError:
                return None
    return None


def dock_width(x: int, w: int, start_left: int | None, min_w: int, margin: int = 8) -> int:
    """Width for a docked ticker at x so it ends `margin` px before the taskbar icons (never below min_w)."""
    if start_left is None or x >= start_left:
        return w                          # unknown, or the ticker sits right of the icons: leave it
    return max(min_w, min(w, start_left - margin - x))


def monitor_dpi(x: int, y: int) -> int:
    try:
        hmon = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
        dx, dy = wintypes.UINT(), wintypes.UINT()
        if ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return int(dx.value)
    except Exception:  # noqa: BLE001
        pass
    return 96


def dpi_factor(x: int, y: int, dpi=monitor_dpi) -> float:
    """How much bigger to draw at (x, y) than on the main monitor (Tk sized its fonts for the main one at start)."""
    return round(dpi(x, y) / max(1, dpi(0, 0)), 3)


def router_models(fetch=None) -> list[tuple[str, str, str]]:
    """(id, state, label) for the model menu: what the router serves right now (Desk models, or the Away models during
    Away) from GET :11440/models, which the router answers itself (never /slots: that wakes and keeps a model).
    Known models keep their descriptions; the fixed list is only the fallback when the router doesn't answer."""
    if fetch is None:
        from ..data.collectors.engines import llama_swap as fetch
    try:
        running = (fetch() or {}).get("running") or []
    except Exception:  # noqa: BLE001
        running = []
    labels = dict(AI_MODELS)
    if not running:
        return [(m, "", label) for m, label in AI_MODELS]
    return [(r["model"], r.get("state") or "", labels.get(r["model"], r["model"])) for r in running if r.get("model")]


CHAINS_DIR = Path(os.environ.get("SOL_CHAINS", r"D:\OBVLT\1Notebook\Chains"))


def get_available_chains(chains_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Returns sorted list of (chain_name, chain_path) from 1Notebook/Chains."""
    cdir = chains_dir or CHAINS_DIR
    if not cdir.exists():
        return []
    chains: list[tuple[str, Path]] = []
    try:
        for f in cdir.glob("*.md"):
            name = f.stem
            if f.name.startswith((".", "$")) or name.endswith(" request") or name.lower() in ("readme", "template", "chain"):
                continue
            chains.append((name, f))
    except Exception:
        pass
    chains.sort(key=lambda x: x[0].lower())
    return chains


DEFAULT_SLIDES_ENABLED = {
    "HW": True,
    "AI": True,
    "RUN": True,
    "SVC": True,
    "DISK": True,
    "SYS": True,
    "NOTE": True,
    "GIT": True,
    "NET": True,
    "MEDIA": True,
}

# Available Color Themes (Curated for High Readability and Contrast)
THEMES: dict[str, dict[str, str]] = {
    "cyber-cyan": {
        "name": "Cyber Cyan (Default)",
        "bg": "#090d16",
        "border": "#252b3b",
        "card": "#131824",
        "badge_bg": "#1e293b",
        "text_main": "#ffffff",
        "text_muted": "#cbd5e1",
        "text_dim": "#94a3b8",
        "accent_primary": "#38bdf8",
        "accent_green": "#4ade80",
        "accent_amber": "#fbbf24",
        "accent_red": "#f87171",
    },
    "high-contrast": {
        "name": "High Contrast OLED",
        "bg": "#000000",
        "border": "#3e4756",
        "card": "#141414",
        "badge_bg": "#1f2937",
        "text_main": "#ffffff",
        "text_muted": "#f1f5f9",
        "text_dim": "#cbd5e1",
        "accent_primary": "#00f0ff",
        "accent_green": "#22c55e",
        "accent_amber": "#facc15",
        "accent_red": "#ef4444",
    },
    "amber-terminal": {
        "name": "Amber Terminal (CRT)",
        "bg": "#0c0a06",
        "border": "#3f2f18",
        "card": "#1c150c",
        "badge_bg": "#2b1e0f",
        "text_main": "#fffbeb",
        "text_muted": "#fde68a",
        "text_dim": "#f59e0b",
        "accent_primary": "#f59e0b",
        "accent_green": "#a3e635",
        "accent_amber": "#fbbf24",
        "accent_red": "#f87171",
    },
    "emerald-matrix": {
        "name": "Emerald Matrix",
        "bg": "#040f09",
        "border": "#173e27",
        "card": "#0a1c11",
        "badge_bg": "#12331f",
        "text_main": "#f0fdf4",
        "text_muted": "#86efac",
        "text_dim": "#34d399",
        "accent_primary": "#10b981",
        "accent_green": "#4ade80",
        "accent_amber": "#facc15",
        "accent_red": "#f87171",
    },
    "nordic-frost": {
        "name": "Nordic Frost",
        "bg": "#0c1424",
        "border": "#283b54",
        "card": "#162238",
        "badge_bg": "#21324c",
        "text_main": "#f8fafc",
        "text_muted": "#cbd5e1",
        "text_dim": "#94a3b8",
        "accent_primary": "#38bdf8",
        "accent_green": "#34d399",
        "accent_amber": "#fbbf24",
        "accent_red": "#f87171",
    },
    "dracula-synth": {
        "name": "Dracula Synthwave",
        "bg": "#110a1c",
        "border": "#3d1c5c",
        "card": "#1d122e",
        "badge_bg": "#311b4d",
        "text_main": "#fdf4ff",
        "text_muted": "#f0abfc",
        "text_dim": "#c084fc",
        "accent_primary": "#e879f9",
        "accent_green": "#4ade80",
        "accent_amber": "#facc15",
        "accent_red": "#f43f5e",
    },
}

DEFAULT_THEME = "cyber-cyan"
BG_COLOR = THEMES[DEFAULT_THEME]["bg"]
BORDER_COLOR = THEMES[DEFAULT_THEME]["border"]
BG_CARD = THEMES[DEFAULT_THEME]["card"]
TEXT_MAIN = THEMES[DEFAULT_THEME]["text_main"]
TEXT_MUTED = THEMES[DEFAULT_THEME]["text_muted"]
TEXT_DIM = THEMES[DEFAULT_THEME]["text_dim"]
ACCENT_CYAN = THEMES[DEFAULT_THEME]["accent_primary"]
ACCENT_GREEN = THEMES[DEFAULT_THEME]["accent_green"]
ACCENT_AMBER = THEMES[DEFAULT_THEME]["accent_amber"]
ACCENT_RED = THEMES[DEFAULT_THEME]["accent_red"]


def truncate_text(text: str, font: tkfont.Font, max_pixels: int) -> str:
    """Truncates text with an ellipsis ('…') so it fits within max_pixels."""
    if font.measure(text) <= max_pixels:
        return text
    ellipsis = "…"
    ellipsis_w = font.measure(ellipsis)
    avail = max_pixels - ellipsis_w
    if avail <= 0:
        return ellipsis
    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if font.measure(text[:mid]) <= avail:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis


def interpolate_color(color_a: str, color_b: str, factor: float) -> str:
    """Interpolate between color_a (factor=0.0) and color_b (factor=1.0) in RGB space."""
    factor = max(0.0, min(1.0, factor))
    try:
        r1, g1, b1 = int(color_a[1:3], 16), int(color_a[3:5], 16), int(color_a[5:7], 16)
        r2, g2, b2 = int(color_b[1:3], 16), int(color_b[3:5], 16), int(color_b[5:7], 16)
        r = int(r1 + (r2 - r1) * factor)
        g = int(g1 + (g2 - g1) * factor)
        b = int(b1 + (b2 - b1) * factor)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return color_b


class SegmentLabel(tk.Canvas):
    """A one-line label whose parts have their own colors (red = worse, green = better, ...: see ticker_data).
    Takes the Label calls the rest of the code uses (configure(text=, fg=, bg=), cget('fg'/'text')) and adds
    set_segments([(text, color), ...]). Too-long text ends in '…' like truncate_text."""

    def __init__(self, master, font: tkfont.Font, bg: str, fg: str, max_px: int = 0, **kw):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=font.metrics("linespace") + 2, **kw)
        self._font, self._fg, self._segments, self.max_px = font, fg, [], max_px
        self.bind("<Configure>", lambda e: self._redraw())

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._segments = [(str(kw.pop("text")), None)]
        if "fg" in kw:
            self._fg = kw.pop("fg")
        if "font" in kw:
            self._font = kw.pop("font")
        if cnf or kw:
            super().configure(cnf, **kw)
        self._redraw()

    config = configure

    def cget(self, key):
        if key == "fg":
            return self._fg
        if key == "text":
            return "".join(t for t, _ in self._segments)
        return super().cget(key)

    def set_segments(self, segments: list[tuple[str, str | None]], max_px: int | None = None) -> None:
        self._segments = list(segments)
        if max_px is not None:
            self.max_px = max_px
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        width = self.max_px or self.winfo_width() or 400
        y = max(self.winfo_height(), int(self.cget("height"))) // 2
        x, ell = 0, self._font.measure("…")
        for text, color in self._segments:
            w = self._font.measure(text)
            if x + w > width:                          # doesn't fit: cut this part, end with '…', stop
                room = width - x - ell
                cut = text
                while cut and self._font.measure(cut) > room:
                    cut = cut[:-1]
                self.create_text(x, y, anchor="w", text=cut.rstrip() + "…", fill=color or self._fg, font=self._font)
                return
            self.create_text(x, y, anchor="w", text=text, fill=color or self._fg, font=self._font)
            x += w


class Tooltip:
    """Lightweight tooltip for Tkinter widgets with a small hover delay."""
    def __init__(self, widget: tk.Widget, delay_ms: int = 350):
        self.widget = widget
        self.delay_ms = delay_ms
        self.tip_window = None
        self.text = ""
        self._timer = None
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<ButtonPress>", self._on_leave, add="+")
        self._lbl = None

    def set_text(self, text: str) -> None:
        self.text = text
        if self.tip_window and self._lbl:
            try:
                self._lbl.configure(text=text)
            except Exception:
                pass

    def _on_enter(self, event=None) -> None:
        self._cancel()
        if self.text:
            self._timer = self.widget.after(self.delay_ms, self.show)

    def _on_leave(self, event=None) -> None:
        self._cancel()
        self.hide()

    def _cancel(self) -> None:
        if self._timer:
            try:
                self.widget.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def show(self) -> None:
        if not self.text or self.tip_window:
            return
        try:
            (left, top, right, bottom), screen_w, screen_h = get_screen_and_work_area()
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() - 32
            x = max(left + 8, min(x, right - 320))
            if y < top + 10:
                y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

            self.tip_window = tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_attributes("-topmost", True)
            t_bg = getattr(self.widget, "_tip_bg", BG_CARD)
            t_fg = getattr(self.widget, "_tip_fg", TEXT_MAIN)
            t_bd = getattr(self.widget, "_tip_border", BORDER_COLOR)
            tw.configure(bg=t_bd)

            self._lbl = tk.Label(
                tw, text=self.text, justify=tk.LEFT,
                bg=t_bg, fg=t_fg, font=("Segoe UI", 8),
                relief=tk.FLAT, padx=6, pady=3,
                wraplength=420
            )
            self._lbl.pack(padx=1, pady=1)
            tw.wm_geometry(f"+{x}+{y}")
        except Exception:
            pass

    def hide(self) -> None:
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None
            self._lbl = None


def get_screen_and_work_area() -> tuple[tuple[int, int, int, int], int, int]:
    """Returns ((left, top, right, bottom), screen_w, screen_h)."""
    try:
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        return (rect.left, rect.top, rect.right, rect.bottom), screen_w, screen_h
    except Exception:
        return (0, 0, 1920, 1040), 1920, 1080


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def monitor_rects(x: int, y: int) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """(monitor rect, work area rect) of the monitor nearest to the point, as (left, top, right, bottom)."""
    try:
        hmon = ctypes.windll.user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO(); info.cbSize = ctypes.sizeof(_MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            m, w = info.rcMonitor, info.rcWork
            return (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)
    except Exception:
        pass
    (l, t, r, b), sw, sh = get_screen_and_work_area()
    return (0, 0, sw, sh), (l, t, r, b)


def clamp_rect(x: int, y: int, w: int, h: int, mon: tuple[int, int, int, int], max_y: int) -> tuple[int, int]:
    """Pure part of the clamp (tested): keep x inside the monitor, y between its top and max_y."""
    left, top, right, _ = mon
    return max(left, min(x, right - w)), max(top, min(y, max_y))


SHELL_PROCESSES = {"explorer.exe", "startmenuexperiencehost.exe", "searchhost.exe", "shellexperiencehost.exe",
                   "shellhost.exe", "textinputhost.exe", "lockapp.exe"}


def foreground_owner(hwnd: int) -> str:
    """Lower-case exe name of the process that owns the window ('' if unknown)."""
    try:
        import psutil
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def raise_topmost(hwnd: int) -> None:
    """Put the window back on top of the topmost band without taking focus. The taskbar is topmost too: every click on
    it (Start, tray, a taskbar button) put it above a docked ticker, which then looked gone (09-26)."""
    HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = -1, 0x0001, 0x0002, 0x0010
    try:
        # HWND_TOPMOST must go in as a handle: a plain -1 is passed as a 32-bit int, arrives as 0xFFFFFFFF and the call
        # fails (ERROR_INVALID_WINDOW_HANDLE) without raising anything
        ctypes.windll.user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception:
        pass


TASKBAR_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")


def covered_by_taskbar(hwnd: int) -> bool:
    """A taskbar sits above the window in z-order and overlaps it. Only then is a raise needed: raising every second
    regardless made the ticker flicker (09-26), and raising over menus or tooltips would hide them."""
    u = ctypes.windll.user32
    u.GetWindow.restype = wintypes.HWND
    u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    mine = wintypes.RECT()
    if not u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(mine)):
        return False
    h, steps, buf = u.GetWindow(wintypes.HWND(hwnd), 3), 0, ctypes.create_unicode_buffer(64)   # GW_HWNDPREV: above us
    while h and steps < 2000:
        u.GetClassNameW(h, buf, 64)
        if buf.value in TASKBAR_CLASSES and u.IsWindowVisible(h):
            r = wintypes.RECT()
            if u.GetWindowRect(h, ctypes.byref(r)) and r.left < mine.right and mine.left < r.right \
                    and r.top < mine.bottom and mine.top < r.bottom:
                return True
        h, steps = u.GetWindow(h, 3), steps + 1
    return False


def is_foreground_fullscreen() -> bool:
    """Returns True if the current foreground window is a fullscreen application or game."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        shell_hwnd = ctypes.windll.user32.GetShellWindow()
        if hwnd == shell_hwnd:
            return False

        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value
        # Ignore desktop, taskbars, notification popups, and lock screen
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"):
            return False
        # Alt+Tab, Task View, Win+Tab, the Start/Search hosts are full-monitor Explorer windows, not games: hiding for
        # them made the ticker vanish when you used the taskbar (09-26)
        if foreground_owner(hwnd) in SHELL_PROCESSES:
            return False

        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False

        hmon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return False
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return False

        m = info.rcMonitor
        # Window covers or exceeds monitor boundary
        return (rect.left <= m.left and rect.top <= m.top and
                rect.right >= m.right and rect.bottom >= m.bottom)
    except Exception:
        return False


class TickerApp:
    def __init__(self, root: tk.Tk, collector: TickerCollector | None = None, hub=None):
        """collector/hub: given when the SOL Control HUD hub runs this view (one collector shared with the dashboard;
        ✕ then hides the ticker into the tray instead of quitting). Alone (python -m ...views.ticker) it owns both."""
        self.root = root
        self.hub = hub
        self.user_hidden = False            # hidden by you (hub: ✕ / tray), not by the fullscreen auto-hide
        self.collector = collector or TickerCollector()
        self.collector.start()

        # State & Settings
        self.settings = self._load_settings()
        self.mode = self.settings.get("mode", "single")  # "single" or "multi"
        self.docked = self.settings.get("docked", False)
        self.theme_name = self.settings.get("theme", DEFAULT_THEME)
        if self.theme_name not in THEMES:
            self.theme_name = DEFAULT_THEME
        self.opacity = float(self.settings.get("opacity", 0.94))
        self.interval = self.settings.get("interval_seconds", 6)
        loaded_slides = self.settings.get("slides_enabled", {})
        self.slides_enabled = {k: loaded_slides.get(k, DEFAULT_SLIDES_ENABLED.get(k, True)) for k in DEFAULT_SLIDES_ENABLED}
        self.alerts_pulse = bool(self.settings.get("alerts_pulse", True))
        self.alerts_sound = bool(self.settings.get("alerts_sound", False))
        self.auto_hide_fullscreen = bool(self.settings.get("auto_hide_fullscreen", True))
        self._is_hidden_for_fullscreen = False
        self._pulse_timer = None
        self._pulse_step = 0
        self._pulse_color = self.get_theme()["accent_green"]
        self._prev_snap: Snapshot | None = None
        self.paused = False
        self.current_slide = 0
        self.slides: list[dict] = []
        self.multiline_rows: list[dict] = []
        self.latest_snap = Snapshot()

        # Drag tracking
        self._drag_x = 0
        self._drag_y = 0
        self._dragged = False
        self._press_pos = (0, 0)

        # Font scale
        self.font_scale = str(self.settings.get("font_scale", DEFAULT_FONT_SCALE))
        if self.font_scale not in FONT_SCALES:
            self.font_scale = DEFAULT_FONT_SCALE
        scale_info = FONT_SCALES[self.font_scale]

        # Fonts (named, so a move to a monitor with another scale rescales them all: _apply_dpi)
        self._dpi = 1.0
        self._fonts: dict[tuple[int, str], tkfont.Font] = {}
        self._start_left: int | None = None      # where the taskbar icons begin (docked width limit)
        self._start_left_new = False
        self.font_single = tkfont.Font(family="Segoe UI", size=int(scale_info["font_single"]))
        self.font_row = tkfont.Font(family="Segoe UI", size=int(scale_info["font_row"]))

        # Window setup
        self._configure_window()
        self._create_widgets()
        self._bind_events()

        # Position window
        self._apply_geometry(initial=True)

        # Clear stale activation trigger if present
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except OSError:
                pass

        # Initial data update
        self._on_data_tick()

        # Auto-rotation loop for single-line ticker
        self.root.after(int(self.interval * 1000), self._auto_rotate_slide)
        # Background collector poll loop
        self.root.after(1500, self._poll_collector)
        # Check for external activation triggers (e.g. shortcut launched while already running)
        self.root.after(350, self._check_activation_trigger)
        # Stay above the taskbar (it's topmost too and wins every time you click it)
        self.root.after(1000, self._keep_on_top)
        # Where the taskbar icons begin (a docked ticker ends before them), then every 10 minutes
        self.root.after(3000, self._refresh_start_left)

    def _keep_on_top(self) -> None:
        try:
            if (not self._is_hidden_for_fullscreen and not self.user_hidden and getattr(self, "hwnd", None)
                    and covered_by_taskbar(self.hwnd)
                    and not is_foreground_fullscreen()):
                raise_topmost(self.hwnd)
                self._raises = getattr(self, "_raises", 0) + 1
                if self._raises <= 3 or self._raises % 100 == 0:   # enough to see it working, never a flood
                    log_event(f"raised above the taskbar (#{self._raises})")
        except Exception:
            pass
        self.root.after(500, self._keep_on_top)   # cheap check (no redraw unless covered), so it can run twice a second

    def _font(self, size: int, weight: str = "normal") -> tkfont.Font:
        key = (size, weight)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(family="Segoe UI", size=max(1, round(size * self._dpi)), weight=weight)
        return self._fonts[key]

    def _apply_dpi(self, factor: float) -> bool:
        """Scale every font to the monitor the ticker is on (the 4K one runs at 150 % next to a 100 % main screen)."""
        if abs(factor - self._dpi) < 0.01:
            return False
        self._dpi = factor
        for (size, _), font in self._fonts.items():
            font.configure(size=max(1, round(size * factor)))
        info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        self.font_single.configure(size=max(1, round(int(info["font_single"]) * factor)))
        self.font_row.configure(size=max(1, round(int(info["font_row"]) * factor)))
        for w in [getattr(self, "single_text", None), *(v for _, v in getattr(self, "row_widgets", []))]:
            if w is not None:
                w.configure(height=w._font.metrics("linespace") + 2)
        return True

    def _refresh_start_left(self, again: bool = True) -> None:
        """Ask (in the background) where the taskbar icons begin; the poll loop picks it up. Every 10 min."""
        def work():
            self._start_left = start_button_left()
            self._start_left_new = True
        threading.Thread(target=work, name="taskbar-gap", daemon=True).start()
        if again:
            self.root.after(600_000, self._refresh_start_left)

    def get_theme(self) -> dict[str, str]:
        """Returns the dictionary for the currently selected color theme."""
        return THEMES.get(self.theme_name, THEMES[DEFAULT_THEME])

    def set_theme(self, name: str) -> None:
        """Sets the active theme, persists it, and immediately updates all UI elements live."""
        if name not in THEMES:
            name = DEFAULT_THEME
        self.theme_name = name
        self.settings["theme"] = name
        self._save_settings()
        self._apply_theme()

    def set_font_scale(self, scale: str) -> None:
        """Sets active font scale, persists it, and adjusts fonts and window geometry live."""
        if scale not in FONT_SCALES:
            scale = DEFAULT_FONT_SCALE
        self.font_scale = scale
        self.settings["font_scale"] = scale
        self._save_settings()
        scale_info = FONT_SCALES[scale]
        self.font_single.configure(size=int(scale_info["font_single"]))
        self.font_row.configure(size=int(scale_info["font_row"]))
        self.single_text.configure(height=self.font_single.metrics("linespace") + 2)
        for _, lbl_val in self.row_widgets:
            lbl_val.configure(height=self.font_row.metrics("linespace") + 2)
        self._apply_geometry()
        self._render()

    def switch_ai_model(self, model_id: str) -> None:
        """Asynchronously requests llama.cpp router on 127.0.0.1:11440 to load or unload a model."""
        import httpx

        def _do_switch():
            try:
                with httpx.Client(timeout=15.0) as client:
                    if model_id == "unload":
                        cur_model = self.latest_snap.ai_model
                        if cur_model:
                            client.post("http://127.0.0.1:11440/models/unload", json={"model": cur_model})
                    else:
                        client.post("http://127.0.0.1:11440/models/load", json={"model": model_id})
            except Exception:
                pass
            try:
                self.root.after(400, self._on_data_tick)
            except Exception:
                pass

        threading.Thread(target=_do_switch, name="model-switch-worker", daemon=True).start()
        pulse_col = self.get_theme()["accent_amber"] if model_id == "unload" else self.get_theme()["accent_primary"]
        self.trigger_alert(pulse_col, "model_switch")

    def launch_chain(self, chain_path: Path) -> None:
        """Queues a prompt chain for execution by the chain runner daemon."""
        try:
            from ..chains.chain_note import set_chain_status
            set_chain_status(chain_path, "queued")
        except Exception:
            pass
        self.trigger_alert(self.get_theme()["accent_green"], "chain_queued")
        self.root.after(300, self._on_data_tick)

    def _apply_theme(self) -> None:
        """Updates colors across all existing Tkinter widgets live without reloading."""
        t = self.get_theme()
        self.root.configure(bg=t["border"])
        self.container.configure(bg=t["bg"])
        self.single_frame.configure(bg=t["bg"])
        self.vram_meter.configure(bg=t["bg"])
        self.tag_label.configure(bg=t["badge_bg"])
        self.single_text.configure(bg=t["bg"], fg=t["text_main"])
        self.dots_label.configure(bg=t["bg"], fg=t["text_dim"])
        self.btn_expand.configure(bg=t["bg"], fg=t["text_muted"])
        self.btn_close_single.configure(bg=t["bg"], fg=t["text_dim"])
        self.multi_frame.configure(bg=t["bg"])
        self.header_frame.configure(bg=t["bg"])
        self.title_label.configure(bg=t["bg"], fg=t["text_muted"])
        self.mode_badge.configure(bg=t["bg"])
        self.crash_badge.configure(bg=t["bg"])
        self.btn_close_multi.configure(bg=t["bg"], fg=t["text_dim"])
        self.btn_collapse.configure(bg=t["bg"], fg=t["text_muted"])
        self.btn_web.configure(bg=t["bg"], fg=t["accent_primary"])
        self.sep.configure(bg=t["border"])
        for lbl_title, lbl_val in self.row_widgets:
            lbl_title.configure(bg=t["bg"], fg=t["text_dim"])
            lbl_val.configure(bg=t["bg"], fg=t["text_main"])
            parent_frame = lbl_title.master
            if parent_frame:
                parent_frame.configure(bg=t["bg"])
        for tt in (self.tag_tooltip, self.single_tooltip, self.dots_tooltip,
                   self.mode_badge_tooltip, self.crash_tooltip, *self.row_tooltips):
            tt.widget._tip_bg = t["card"]
            tt.widget._tip_fg = t["text_main"]
            tt.widget._tip_border = t["border"]
        self._render()

    def _load_settings(self) -> dict:
        defaults = {
            "mode": "single",
            "docked": False,
            "theme": DEFAULT_THEME,
            "font_scale": DEFAULT_FONT_SCALE,
            "opacity": 0.94,
            "interval_seconds": 6,
            "slides_enabled": dict(DEFAULT_SLIDES_ENABLED),
            "alerts_pulse": True,
            "alerts_sound": False,
            "auto_hide_fullscreen": True,
        }
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if "slides_enabled" in data and isinstance(data["slides_enabled"], dict):
                    loaded_slides = dict(DEFAULT_SLIDES_ENABLED)
                    loaded_slides.update(data["slides_enabled"])
                    data["slides_enabled"] = loaded_slides
                defaults.update(data)
                return defaults
            except (OSError, ValueError):
                pass
        return defaults

    def _save_settings(self) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.settings["mode"] = self.mode
            self.settings["docked"] = self.docked
            self.settings["theme"] = self.theme_name
            self.settings["font_scale"] = self.font_scale
            self.settings["opacity"] = self.opacity
            self.settings["interval_seconds"] = self.interval
            self.settings["slides_enabled"] = self.slides_enabled
            self.settings["alerts_pulse"] = self.alerts_pulse
            self.settings["alerts_sound"] = self.alerts_sound
            self.settings["auto_hide_fullscreen"] = self.auto_hide_fullscreen
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()
            if cur_x > 0 or cur_y > 0:
                self.settings["x"] = cur_x
                self.settings["y"] = cur_y
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _configure_window(self) -> None:
        t = self.get_theme()
        self.root.title(WINDOW_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.opacity)
        self.root.configure(bg=t["border"])

        # Apply WS_EX_TOOLWINDOW and clear WS_EX_APPWINDOW to ensure window
        # stays completely out of the Alt+Tab switcher.
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            self.hwnd = hwnd
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _apply_geometry(self, initial: bool = False) -> None:
        scale_info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        probe_x = int(self.settings.get("x") or 0) if initial else self.root.winfo_x()
        probe_y = int(self.settings.get("y") or 0) if initial else self.root.winfo_y()
        self._apply_dpi(dpi_factor(probe_x + 40, probe_y + 10) if (probe_x or probe_y) else 1.0)
        w = int(int(scale_info["width"]) * self._dpi)
        single_h = int(int(scale_info["single_h"]) * self._dpi)
        multi_h = int(int(scale_info["multi_h"]) * self._dpi)
        h = single_h if self.mode == "single" else multi_h

        (left, top, right, bottom), screen_w, screen_h = get_screen_and_work_area()
        taskbar_h = max(screen_h - bottom, 40)
        taskbar_y = bottom

        if self.docked:
            if self.mode == "single":
                target_h = min(single_h, taskbar_h - 10)
                y = taskbar_y + (taskbar_h - target_h) // 2
                h = target_h
            else:
                # In multi-mode while docked, pop up above the taskbar
                y = taskbar_y - h - 6
            default_x = left + 80
        else:
            default_x = left + 16
            y = bottom - h - 12

        if initial:
            saved_x = self.settings.get("x")
            saved_y = self.settings.get("y")
            if (saved_x is not None and saved_y is not None and
                    (saved_x > left or saved_y > top) and
                    left <= saved_x <= right - 100 and top <= saved_y <= screen_h - h):
                x = saved_x
                if not self.docked:
                    y = saved_y
            else:
                x = default_x
        else:
            x = self.root.winfo_x()
            if not self.docked:
                curr_y = self.root.winfo_y()
                curr_h = self.root.winfo_height() or (multi_h if self.mode == "single" else single_h)
                y = curr_y + (curr_h - h)
                if y < top:
                    y = top
                if y + h > bottom:
                    y = bottom - h

        if self.docked and self.mode == "single":
            w = dock_width(x, w, self._start_left, int(260 * self._dpi))   # end before the taskbar icons
        x, y = self._clamp(x, y, w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _clamp(self, x: int, y: int, w: int, h: int) -> tuple[int, int]:
        """Keep the whole window on the monitor it's on (any monitor, e.g. the 4K one above the main screen). The
        multi-line panel never covers that monitor's taskbar; the single line may sit inside it only if docked."""
        (mon, work) = monitor_rects(x + w // 2, y + h // 2)
        max_y = (mon[3] - h) if (self.mode == "single" and self.docked) else (work[3] - h)
        return clamp_rect(x, y, w, h, mon, max_y)

    def _create_widgets(self) -> None:
        t = self.get_theme()
        # Outer border wrapper: pad 1px so root border color (and pulse glow) frames the app
        self.container = tk.Frame(self.root, bg=t["bg"])
        self.container.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # --- Single-line View ---
        self.single_frame = tk.Frame(self.container, bg=t["bg"], height=SINGLE_HEIGHT)

        # VRAM visual meter (2px bar at bottom of single line)
        self.vram_meter = tk.Canvas(
            self.single_frame, height=2, bg=t["bg"], highlightthickness=0, bd=0
        )
        self.vram_meter.pack(side=tk.BOTTOM, fill=tk.X)

        # Tag badge
        self.tag_label = tk.Label(
            self.single_frame, text="HW", bg=t["badge_bg"], fg=t["accent_primary"],
            font=self._font(8, "bold"), padx=6, pady=2, cursor="hand2"
        )
        self.tag_label.pack(side=tk.LEFT, padx=(6, 8), pady=4)
        self.tag_tooltip = Tooltip(self.tag_label)

        # Content text
        self.single_text = SegmentLabel(self.single_frame, self.font_single, bg=t["bg"], fg=t["text_main"], cursor="hand2")
        self.single_text.configure(text="Gathering workstation metrics...")
        self.single_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=4)
        self.single_tooltip = Tooltip(self.single_text)

        # Slide indicator dots
        self.dots_label = tk.Label(
            self.single_frame, text="● ○ ○ ○", bg=t["bg"], fg=t["text_dim"],
            font=self._font(7), padx=4, cursor="hand2"
        )
        self.dots_label.pack(side=tk.LEFT, padx=4)
        self.dots_tooltip = Tooltip(self.dots_label)
        self.dots_tooltip.set_text("Click or Space to cycle slides")

        # Toggle to multiline button
        self.btn_expand = tk.Label(
            self.single_frame, text="⊞", bg=t["bg"], fg=t["text_muted"],
            font=self._font(10), padx=4, cursor="hand2"
        )
        self.btn_expand.pack(side=tk.LEFT, padx=(2, 4))
        self.btn_expand.bind("<Button-1>", lambda e: self.toggle_mode())

        # Close button
        self.btn_close_single = tk.Label(
            self.single_frame, text="✕", bg=t["bg"], fg=t["text_dim"],
            font=self._font(8), padx=6, cursor="hand2"
        )
        self.btn_close_single.pack(side=tk.RIGHT, padx=(0, 4))
        self.btn_close_single.bind("<Button-1>", lambda e: self.quit("✕ button"))

        # --- Multi-line View ---
        self.multi_frame = tk.Frame(self.container, bg=t["bg"], padx=8, pady=6)

        # Multi-line Header
        self.header_frame = tk.Frame(self.multi_frame, bg=t["bg"])
        self.header_frame.pack(fill=tk.X, pady=(0, 4))

        self.title_label = tk.Label(
            self.header_frame, text="SOL WORKSTATION", bg=t["bg"], fg=t["text_muted"],
            font=self._font(8, "bold")
        )
        self.title_label.pack(side=tk.LEFT)

        self.status_dot = tk.Label(
            self.header_frame, text="●", bg=t["bg"], fg=t["accent_green"],
            font=self._font(7)
        )
        self.status_dot.pack(side=tk.LEFT, padx=(4, 6))

        self.mode_badge = tk.Label(
            self.header_frame, text="[DESK]", bg=t["bg"], fg=t["accent_primary"],
            font=self._font(7, "bold"), cursor="hand2"
        )
        self.mode_badge.pack(side=tk.LEFT)
        self.mode_badge_tooltip = Tooltip(self.mode_badge)
        self.mode_badge.bind("<Button-1>", lambda e: self.toggle_ai_mode())

        # Crash warning (unexpected reboots / GPU driver resets since the baseline or the last acknowledgement)
        self.crash_badge = tk.Label(
            self.header_frame, text="", bg=t["bg"], fg=t["accent_red"],
            font=self._font(7, "bold")
        )
        self.crash_badge.pack(side=tk.LEFT, padx=(6, 0))
        self.crash_tooltip = Tooltip(self.crash_badge)

        # Header controls
        self.btn_close_multi = tk.Label(
            self.header_frame, text="✕", bg=t["bg"], fg=t["text_dim"],
            font=self._font(8), padx=4, cursor="hand2"
        )
        self.btn_close_multi.pack(side=tk.RIGHT)
        self.btn_close_multi.bind("<Button-1>", lambda e: self.quit("✕ button"))

        self.btn_collapse = tk.Label(
            self.header_frame, text="⊟", bg=t["bg"], fg=t["text_muted"],
            font=self._font(10), padx=6, cursor="hand2"
        )
        self.btn_collapse.pack(side=tk.RIGHT)
        self.btn_collapse.bind("<Button-1>", lambda e: self.toggle_mode())

        self.btn_web = tk.Label(
            self.header_frame, text="↗ Web HUD", bg=t["bg"], fg=t["accent_primary"],
            font=self._font(8), padx=6, cursor="hand2"
        )
        self.btn_web.pack(side=tk.RIGHT, padx=4)
        self.btn_web.bind("<Button-1>", lambda e: self.open_web_hud())

        # Separator line
        self.sep = tk.Frame(self.multi_frame, bg=t["border"], height=1)
        self.sep.pack(fill=tk.X, pady=(0, 6))

        # Data Rows (5 rows: HARDWARE, LOCAL AI, CHAINS, SERVICES, SYSTEM)
        self.row_widgets: list[tuple[tk.Label, tk.Label]] = []
        self.row_tooltips: list[Tooltip] = []
        for row_idx in range(5):
            rf = tk.Frame(self.multi_frame, bg=t["bg"], cursor="hand2")
            rf.pack(fill=tk.X, pady=1)

            lbl_title = tk.Label(
                rf, text="", bg=t["bg"], fg=t["text_dim"], font=self._font(7, "bold"),
                width=10, anchor="w", cursor="hand2"
            )
            lbl_title.pack(side=tk.LEFT)

            lbl_val = SegmentLabel(rf, self.font_row, bg=t["bg"], fg=t["text_main"], cursor="hand2")
            lbl_val.pack(side=tk.LEFT, fill=tk.X, expand=True)

            self.row_tooltips.append(Tooltip(lbl_val))
            self.row_widgets.append((lbl_title, lbl_val))

            # Bind row click to its target action
            idx_capture = row_idx
            for w in (rf, lbl_title, lbl_val):
                w.bind("<Button-1>", lambda e, idx=idx_capture: self._on_row_click(idx))
                w.bind("<Button-3>", self._show_context_menu)
                w.bind("<Double-Button-1>", lambda e: self.open_web_hud())

        # Display initial mode
        if self.mode == "single":
            self.single_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.multi_frame.pack(fill=tk.BOTH, expand=True)

    def _bind_events(self) -> None:
        # Dragging on container and frames
        for w in [self.container, self.single_frame, self.multi_frame, self.header_frame,
                 self.single_text, self.title_label]:
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._stop_drag)
            w.bind("<Double-Button-1>", lambda e: self.open_web_hud())
            w.bind("<Button-3>", self._show_context_menu)

        # Single click on ticker text advances slide; clicking tag opens slide target
        self.single_text.bind("<Button-1>", lambda e: self._on_text_click())
        self.tag_label.bind("<Button-1>", lambda e: self._on_tag_click())
        self.dots_label.bind("<Button-1>", lambda e: self._on_text_click())

        # Pause rotation on mouse hover
        self.root.bind("<Enter>", lambda e: self._on_mouse_enter())
        self.root.bind("<Leave>", lambda e: self._on_mouse_leave())

        # Keybindings
        self.root.bind("<t>", lambda e: self.toggle_mode())
        self.root.bind("<T>", lambda e: self.toggle_mode())
        self.root.bind("<space>", lambda e: self._manual_next_slide())
        # No Esc-to-quit: the ticker has keyboard focus after any click on it, so an Esc meant for something else
        # closed it for good (09-26). Quit with ✕ or right-click → Exit.

    def _start_drag(self, event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()
        self._dragged = False
        self._press_pos = (event.x_root, event.y_root)

    def _on_drag(self, event) -> None:
        dx = abs(event.x_root - self._press_pos[0])
        dy = abs(event.y_root - self._press_pos[1])
        if dx > 3 or dy > 3:
            self._dragged = True
        new_x = event.x_root - self._drag_x
        new_y = event.y_root - self._drag_y
        self.root.geometry(f"+{new_x}+{new_y}")

    def _stop_drag(self, event) -> None:
        w, h = self.root.winfo_width() or WIDTH, self.root.winfo_height() or SINGLE_HEIGHT
        x, y = self._clamp(self.root.winfo_x(), self.root.winfo_y(), w, h)
        if (x, y) != (self.root.winfo_x(), self.root.winfo_y()):
            self.root.geometry(f"+{x}+{y}")
        if abs(dpi_factor(x + w // 2, y + h // 2) - self._dpi) >= 0.01:
            self._apply_geometry()         # dropped on a monitor with another scale (the 4K one): redraw at its size
        self._save_settings()

    def _open_slide_target(self, tag: str) -> None:
        tag_u = (tag or "").upper()
        if "AI" in tag_u:
            webbrowser.open("http://127.0.0.1:11440")
        elif "MEDIA" in tag_u:
            from ..data.collectors.media import focus_media_app
            focus_media_app(self.latest_snap.media_app, self.latest_snap.media_title)
        elif any(k in tag_u for k in ("RUN", "CHAIN", "PIPE")):
            chains_dir = Path(r"D:\OBVLT\1Notebook\Chains\Results")
            if not chains_dir.exists():
                chains_dir = Path(r"D:\OBVLT\1Notebook\Chains")
            if chains_dir.exists():
                os.startfile(str(chains_dir))
        elif "NOTE" in tag_u:
            year = time.strftime("%Y")
            date_str = time.strftime("%Y-%m-%d")
            note_path = Path(r"D:\Polymatica Vault") / year / f"{date_str}.md"
            if not note_path.exists():
                try:
                    note_path.parent.mkdir(parents=True, exist_ok=True)
                    tmpl_path = Path(r"D:\Polymatica Vault\templates\Titled post.md")
                    init_content = tmpl_path.read_text(encoding="utf-8") if tmpl_path.exists() else "hidden: false\ntitle: \ntags: \nNote:\n\n"
                    note_path.write_text(init_content, encoding="utf-8")
                except Exception:
                    pass
            uri = f"obsidian://open?vault=Polymatica%20Vault&file={year}%2F{date_str}"
            opened = False
            try:
                os.startfile(uri)
                opened = True
            except Exception:
                try:
                    webbrowser.open(uri)
                    opened = True
                except Exception:
                    pass
            if not opened:
                if note_path.exists():
                    os.startfile(str(note_path))
                elif Path(r"D:\Polymatica Vault").exists():
                    os.startfile(r"D:\Polymatica Vault")
        elif "GIT" in tag_u:
            ws_dir = Path(r"D:\Workspace")
            if ws_dir.exists():
                os.startfile(str(ws_dir))
        elif "NET" in tag_u:
            try:
                subprocess.Popen(["cmd", "/c", "start", "ms-settings:network"], creationflags=0x08000000 if os.name == "nt" else 0)
            except Exception:
                pass
        elif any(k in tag_u for k in ("DISK", "SYS", "STORAGE", "SYSTEM")):
            if "SYS" in tag_u and (self.latest_snap.unexpected_reboots > 0 or self.latest_snap.gpu_resets > 0):
                rep_dir = Path(r"D:\OBVLT\reports")
                if rep_dir.exists():
                    os.startfile(str(rep_dir))
                    return
            if os.path.exists("D:\\"):
                os.startfile("D:\\")
            else:
                os.startfile(os.environ.get("USERPROFILE", "C:\\"))
        else:
            self.open_web_hud()

    def switch_ai_mode(self, mode: str) -> None:
        """Switches local AI mode (desk or away) via D:\\OBVLT\\tools\\sol-llm.ps1 asynchronously."""
        script = SOL_LLM
        if script.exists():
            def _runner():
                try:
                    subprocess.run(   # Away takes ~10-20 s (closes apps, loads the model): 15 s cut it off
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), mode],
                        capture_output=True,
                        timeout=120.0,
                        creationflags=0x08000000 if os.name == "nt" else 0,
                    )
                except Exception:
                    pass
                self.refresh_data_now()
            threading.Thread(target=_runner, daemon=True).start()
        self.trigger_alert(ACCENT_CYAN, "ai_mode")

    def stop_ai_work(self) -> None:
        """Ends Away the safe way, like the Away panel's button: the watcher puts the running job back in the queue."""
        from tkinter import messagebox
        if not messagebox.askyesno("Stop AI work", "Stop the Away work now?\n\nThe running job goes back in the "
                                   "queue (finished work is kept) and the AI returns to Desk mode.", parent=self.root):
            return
        try:
            (LLM_DIR / "away-stop.flag").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        except OSError:
            pass
        self.trigger_alert(self.get_theme()["accent_amber"], "ai_mode")

    def toggle_ai_mode(self) -> None:
        """Desk -> Away; while Away works, the same click offers Stop AI work (never a hard switch that loses the job)."""
        if self.latest_snap.ai_mode == "away":
            self.stop_ai_work()
        else:
            self.switch_ai_mode("away")

    def acknowledge_crashes(self) -> None:
        """Acknowledges unexpected reboots / GPU driver resets to clear the red warning badge."""
        # This file also decides whether Away may run (crash-watch refuses Away after a new, unreviewed crash), so it
        # asks first and keeps the earlier notes (09-26: one click replaced the whole crash history).
        from tkinter import messagebox
        crashes = self.latest_snap.unexpected_reboots + self.latest_snap.gpu_resets
        if not messagebox.askyesno(
                "Clear crash alert",
                f"Mark the {crashes} crash event(s) as reviewed?\n\nThis also lets Away mode run again. "
                "Check reports\\crash-watch.md first if you haven't.", parent=self.root, icon="warning"):
            return
        try:
            write_crash_ack(ACK_FILE)
            self.refresh_data_now()
        except Exception:
            pass

    def refresh_data_now(self) -> None:
        """Forces an immediate data collection across all feeds and updates the UI."""
        def _collect():
            if hasattr(self.collector, "invalidate_cache"):
                self.collector.invalidate_cache()
            self.collector.collect_once()
            self.root.after(0, self._on_data_tick)
        threading.Thread(target=_collect, daemon=True).start()

    def _on_tag_click(self) -> None:
        if self._dragged:
            return
        if self.slides:
            slide = self.slides[self.current_slide % len(self.slides)]
            if slide.get("tag") == "ALERT":
                self._open_slide_target(slide.get("target", "SYS"))    # opens what the first problem is about
            elif slide.get("tag") == "NIGHT":
                self.open_night_summary()
            else:
                self._open_slide_target(slide.get("tag", ""))

    def open_night_summary(self) -> None:
        """Open the newest review (1Notebook\\Reviews) or the reports folder, and stop showing this night's summary."""
        night = self.latest_snap.night
        if night:
            self.settings["night_seen"] = night["end_iso"]
            self._save_settings()
        reviews = sorted(REVIEWS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime) if REVIEWS_DIR.exists() else []
        started = night and time.mktime(time.strptime(night["end_iso"][:10], "%Y-%m-%d")) - 86400
        target = reviews[-1] if reviews and (not started or reviews[-1].stat().st_mtime >= started) else REPORTS_DIR
        try:
            os.startfile(str(target))
        except OSError:
            pass
        self._on_data_tick()

    def _on_row_click(self, row_idx: int) -> None:
        if self._dragged:
            return
        if row_idx < len(self.multiline_rows):
            title = self.multiline_rows[row_idx].get("title", "")
            self._open_slide_target(title)

    def _on_text_click(self) -> None:
        if not self._dragged:
            self._manual_next_slide()

    def _on_mouse_enter(self) -> None:
        self.paused = True
        self.dots_label.configure(fg=self.get_theme()["accent_primary"])

    def _on_mouse_leave(self) -> None:
        self.paused = False
        self.dots_label.configure(fg=self.get_theme()["text_dim"])

    def _show_context_menu(self, event) -> None:
        t = self.get_theme()
        menu = tk.Menu(self.root, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        mode_text = "Switch to Multi-line Mode (T)" if self.mode == "single" else "Switch to Single-line Mode (T)"
        dock_text = "Float Above Taskbar" if self.docked else "Dock in Taskbar"
        menu.add_command(label=mode_text, command=self.toggle_mode)
        menu.add_command(label=dock_text, command=self.toggle_dock)
        menu.add_command(label="Next Slide (Space)", command=self._manual_next_slide)
        if self.hub:
            menu.add_separator()
            menu.add_command(label="🌐 Open dashboard (browser)", command=self.hub.open_dashboard)
            menu.add_command(label="Hide ticker (stays in the tray)", command=self.hub.hide_ticker)
        menu.add_separator()

        # Quick Actions submenu
        actions_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        if self.latest_snap.ai_mode == "away":
            actions_menu.add_command(label="⏹ Stop AI work (job goes back in the queue)", command=self.stop_ai_work)
        else:
            actions_menu.add_command(label="⚡ Switch AI to Away Mode", command=lambda: self.switch_ai_mode("away"))
            actions_menu.add_command(label="🌙 AI Away + Sleep (sleeps when the work is done)",
                                     command=lambda: self.switch_ai_mode("away-sleep"))
        actions_menu.add_command(label="📝 Open Daily Note (Obsidian)", command=lambda: self._open_slide_target("NOTE"))
        actions_menu.add_command(label="📁 Open Workspace Folder", command=lambda: self._open_slide_target("GIT"))
        actions_menu.add_command(label="🌐 Open dashboard (:7900)", command=self.open_web_hud)
        actions_menu.add_command(label="🔄 Refresh Telemetry Now", command=self.refresh_data_now)
        crashes = self.latest_snap.unexpected_reboots + self.latest_snap.gpu_resets
        if crashes > 0:
            actions_menu.add_separator()
            actions_menu.add_command(label=f"✓ Clear Crash Alert (x{crashes})", command=self.acknowledge_crashes)
        menu.add_cascade(label="Quick Actions ⚡", menu=actions_menu)

        # Local AI Model submenu
        ai_model_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        for m_id, state, label in router_models():
            mark = "✓ " if state in ("loaded", "loading") else "   "
            ai_model_menu.add_command(
                label=f"{mark}{label}" + (f"  ·  {state}" if state and state not in ("loaded", "unloaded") else ""),
                command=lambda mid=m_id: self.switch_ai_model(mid),
            )
        ai_model_menu.add_separator()
        ai_model_menu.add_command(
            label="  Unload Active Model (Free VRAM)",
            command=lambda: self.switch_ai_model("unload"),
        )
        menu.add_cascade(label="Local AI Model 🧠", menu=ai_model_menu)

        # Run Chain submenu
        chains = get_available_chains()
        chain_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        if chains:
            for c_name, c_path in chains:
                chain_menu.add_command(
                    label=f"▶ {c_name}",
                    command=lambda p=c_path: self.launch_chain(p),
                )
        else:
            chain_menu.add_command(label="(No chains found in 1Notebook/Chains)", state=tk.DISABLED)
        chain_menu.add_separator()
        chain_menu.add_command(
            label="Open Chains Folder 📂",
            command=lambda: os.startfile(str(CHAINS_DIR)) if CHAINS_DIR.exists() else None,
        )
        menu.add_cascade(label="Run Chain ⚡", menu=chain_menu)
        menu.add_separator()

        # Color Theme submenu
        theme_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        for k, th in THEMES.items():
            mark = "✓ " if k == self.theme_name else "   "
            theme_menu.add_command(
                label=f"{mark}{th['name']}",
                command=lambda key=k: self.set_theme(key),
            )
        menu.add_cascade(label="Color Theme 🎨", menu=theme_menu)

        # Font Size submenu
        font_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        for k, sc in FONT_SCALES.items():
            mark = "✓ " if k == self.font_scale else "   "
            font_menu.add_command(
                label=f"{mark}{sc['name']}",
                command=lambda key=k: self.set_font_scale(key),
            )
        menu.add_cascade(label="Font Size 🔤", menu=font_menu)

        # Opacity submenu
        op_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        op_menu.add_command(label="100% (Solid)", command=lambda: self.set_opacity(1.0))
        op_menu.add_command(label="94% (Glass - Default)", command=lambda: self.set_opacity(0.94))
        op_menu.add_command(label="80% (Translucent)", command=lambda: self.set_opacity(0.80))
        menu.add_cascade(label="Window Opacity", menu=op_menu)

        # Cycle Speed submenu
        spd_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        spd_menu.add_command(label="Fast (3s)", command=lambda: self.set_interval(3))
        spd_menu.add_command(label="Normal (6s - Default)", command=lambda: self.set_interval(6))
        spd_menu.add_command(label="Slow (10s)", command=lambda: self.set_interval(10))
        menu.add_cascade(label="Cycle Speed", menu=spd_menu)

        # Visible Slides submenu
        slides_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        slide_labels = [
            ("HW", "Hardware (GPU/CPU/RAM)"),
            ("AI", "Local AI Model & Mode"),
            ("RUN", "Active/Last Chain"),
            ("SVC", "Services Status"),
            ("DISK", "Storage & Vaults"),
            ("SYS", "System & Uptime"),
            ("NOTE", "Daily Note (Polymatica)"),
            ("GIT", "Workspace Git Health"),
            ("NET", "Network Throughput"),
            ("MEDIA", "Media / Music Now-Playing"),
        ]
        for tag, label in slide_labels:
            is_on = self.slides_enabled.get(tag, True)
            mark = "✓ " if is_on else "   "
            slides_menu.add_command(
                label=f"{mark}{label}",
                command=lambda t_tag=tag: self.toggle_slide_enabled(t_tag),
            )
        menu.add_cascade(label="Visible Slides", menu=slides_menu)

        # Alerts & Notifications submenu
        alerts_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        p_mark = "✓ " if self.alerts_pulse else "   "
        s_mark = "✓ " if self.alerts_sound else "   "
        alerts_menu.add_command(label=f"{p_mark}Visual Border Pulse", command=self.toggle_alerts_pulse)
        alerts_menu.add_command(label=f"{s_mark}Audio Chime", command=self.toggle_alerts_sound)
        alerts_menu.add_separator()
        alerts_menu.add_command(label="Test Alert Pulse (Green)", command=lambda: self.trigger_alert(t["accent_green"], "test"))
        alerts_menu.add_command(label="Test Alert Pulse (Red)", command=lambda: self.trigger_alert(t["accent_red"], "test"))
        menu.add_cascade(label="Alerts & Notifications", menu=alerts_menu)

        # Auto-hide toggle
        ah_mark = "✓ " if self.auto_hide_fullscreen else "   "
        menu.add_command(label=f"{ah_mark}Auto-Hide in Games & Fullscreen", command=self.toggle_auto_hide_fullscreen)

        # Startup toggle
        st_state = "ON" if is_startup_enabled() else "OFF"
        menu.add_command(label=f"Start at login: {st_state}", command=self.toggle_startup_menu)

        menu.add_separator()
        menu.add_command(label="Open SOL HUD Web (:7900)", command=self.open_web_hud)
        menu.add_command(label="Edit Settings File (JSON)", command=self.open_settings_file)
        menu.add_command(label="Reset Position", command=self._reset_position)
        menu.add_separator()
        menu.add_command(label="Exit SOL Control HUD" if self.hub else "Exit", command=lambda: self.quit("menu Exit"))
        menu.tk_popup(event.x_root, event.y_root)

    def set_opacity(self, val: float) -> None:
        self.opacity = val
        self.root.attributes("-alpha", self.opacity)
        self.settings["opacity"] = self.opacity
        self._save_settings()

    def set_interval(self, secs: int) -> None:
        self.interval = secs
        self.settings["interval_seconds"] = secs
        self._save_settings()

    def toggle_slide_enabled(self, tag: str) -> None:
        current = self.slides_enabled.get(tag, True)
        # Prevent disabling the last remaining active slide
        if current and sum(1 for v in self.slides_enabled.values() if v) <= 1:
            return
        self.slides_enabled[tag] = not current
        self.settings["slides_enabled"] = self.slides_enabled
        self._save_settings()
        if self.latest_snap.sampled_at > 0:
            all_slides = format_slides(self.latest_snap)
            self.slides = [s for s in all_slides if self.slides_enabled.get(s.get("tag", ""), True)] or all_slides
            if self.slides:
                self.current_slide = self.current_slide % len(self.slides)
            self._render()

    def toggle_alerts_pulse(self) -> None:
        self.alerts_pulse = not self.alerts_pulse
        self.settings["alerts_pulse"] = self.alerts_pulse
        self._save_settings()

    def toggle_alerts_sound(self) -> None:
        self.alerts_sound = not self.alerts_sound
        self.settings["alerts_sound"] = self.alerts_sound
        self._save_settings()

    def trigger_alert(self, color: str | None = None, event_type: str = "info") -> None:
        """Triggers an ambient visual border pulse and optional audio chime."""
        t = self.get_theme()
        chosen_color = t["accent_green"] if color is None else color
        if self.alerts_sound and winsound is not None:
            st = winsound.MB_ICONEXCLAMATION if chosen_color == t["accent_red"] else winsound.MB_ICONASTERISK
            play_alert_sound(st)

        if not self.alerts_pulse:
            return

        if self._pulse_timer is not None:
            try:
                self.root.after_cancel(self._pulse_timer)
            except Exception:
                pass
            self._pulse_timer = None

        self._pulse_color = chosen_color
        self._pulse_step = 0
        self._animate_pulse()

    def _animate_pulse(self) -> None:
        total_steps = 12
        step = self._pulse_step
        base_border = self.get_theme()["border"]
        if step >= total_steps:
            self.root.configure(bg=base_border)
            self._pulse_timer = None
            return

        if step < 2:
            cur_color = self._pulse_color
        else:
            factor = (step - 2) / float(total_steps - 2)
            cur_color = interpolate_color(self._pulse_color, base_border, factor)

        self.root.configure(bg=cur_color)
        self._pulse_step += 1
        self._pulse_timer = self.root.after(50, self._animate_pulse)

    def _check_events(self, prev: Snapshot | None, curr: Snapshot) -> None:
        if prev is None or prev.sampled_at == 0:
            return

        # 1. Chain completed or failed
        if prev.chain_running and not curr.chain_running:
            last = (curr.chain_last_finished or "").lower()
            if "fail" in last or "error" in last:
                self.trigger_alert(ACCENT_RED, "chain_failed")
            else:
                self.trigger_alert(ACCENT_GREEN, "chain_completed")
        elif curr.chain_last_finished and curr.chain_last_finished != prev.chain_last_finished:
            last = curr.chain_last_finished.lower()
            if "fail" in last or "error" in last:
                self.trigger_alert(ACCENT_RED, "chain_failed")
            elif any(k in last for k in ("drafted", "finished", "completed")):
                self.trigger_alert(ACCENT_GREEN, "chain_completed")

        # 2. VRAM Eviction transition
        if curr.vram_evicted and not prev.vram_evicted:
            self.trigger_alert(ACCENT_RED, "vram_evicted")

        # 3. New crash / reset detected
        curr_crashes = curr.unexpected_reboots + curr.gpu_resets
        prev_crashes = prev.unexpected_reboots + prev.gpu_resets
        if curr_crashes > prev_crashes:
            self.trigger_alert(ACCENT_RED, "system_crash")

        # 4. AI Mode change (desk -> away -> off)
        if curr.ai_mode != prev.ai_mode:
            self.trigger_alert(ACCENT_CYAN, "ai_mode")

        # 5. GPU Hotspot temperature alert
        hot = HOTSPOT_ALERT_C
        if curr.gpu_hotspot and curr.gpu_hotspot >= hot and (not prev.gpu_hotspot or prev.gpu_hotspot < hot):
            self.trigger_alert(ACCENT_RED, "gpu_hotspot_alert")

    def toggle_auto_hide_fullscreen(self) -> None:
        self.auto_hide_fullscreen = not self.auto_hide_fullscreen
        self.settings["auto_hide_fullscreen"] = self.auto_hide_fullscreen
        self._save_settings()
        if not self.auto_hide_fullscreen and self._is_hidden_for_fullscreen:
            self._is_hidden_for_fullscreen = False
            self.root.deiconify()
            self._apply_geometry()

    def _check_auto_hide(self) -> bool:
        if self.user_hidden:
            return True                    # you hid it: the fullscreen logic must not bring it back
        if not self.auto_hide_fullscreen:
            if self._is_hidden_for_fullscreen:
                self._is_hidden_for_fullscreen = False
                self.root.deiconify()
                self._apply_geometry()
            return False

        # 1. Game mode in state.json
        is_game = self.latest_snap.ai_mode == "off" and (self.latest_snap.ai_reason or "").startswith("game:")
        # 2. Foreground fullscreen window
        is_fs = is_foreground_fullscreen()

        should_hide = is_game or is_fs
        if should_hide and not self._is_hidden_for_fullscreen:
            self._is_hidden_for_fullscreen = True
            self.root.withdraw()
        elif not should_hide and self._is_hidden_for_fullscreen:
            self._is_hidden_for_fullscreen = False
            self.root.deiconify()
            self._apply_geometry()
            self._render()
        return should_hide

    def open_settings_file(self) -> None:
        if not SETTINGS_FILE.exists():
            self._save_settings()
        try:
            os.startfile(str(SETTINGS_FILE))
        except Exception:
            pass

    def toggle_startup_menu(self) -> None:
        if self.hub:                       # one place for it (the tray and the dashboard show the same switch)
            self.hub.do_action("login_start", "off" if is_startup_enabled() else "on")
            return
        new_state = not is_startup_enabled()
        set_startup(new_state)

    def toggle_dock(self) -> None:
        self.docked = not self.docked
        if self.docked:
            self._refresh_start_left(again=False)
        self.settings["docked"] = self.docked
        self._apply_geometry()
        self._save_settings()

    def _reset_position(self) -> None:
        self.docked = False
        self.settings["docked"] = False
        (left, top, right, bottom), screen_w, screen_h = get_screen_and_work_area()
        h = SINGLE_HEIGHT if self.mode == "single" else MULTI_HEIGHT
        x = left + 16
        y = bottom - h - 12
        self.root.geometry(f"+{x}+{y}")
        self._save_settings()

    def _check_activation_trigger(self) -> None:
        """Polls for an activation trigger file written when another instance attempted to launch."""
        try:
            if TRIGGER_FILE.exists():
                try:
                    TRIGGER_FILE.unlink()
                except OSError:
                    pass
                if self._is_hidden_for_fullscreen:
                    self._is_hidden_for_fullscreen = False
                self.root.deiconify()
                self._apply_geometry()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.trigger_alert(ACCENT_CYAN, "activated")
        except Exception:
            pass
        finally:
            try:
                self.root.after(350, self._check_activation_trigger)
            except Exception:
                pass

    def toggle_mode(self) -> None:
        if self.mode == "single":
            self.mode = "multi"
            self.single_frame.pack_forget()
            self._apply_geometry()
            self.multi_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.mode = "single"
            self.multi_frame.pack_forget()
            self._apply_geometry()
            self.single_frame.pack(fill=tk.BOTH, expand=True)
        self._save_settings()
        self._render()

    def _manual_next_slide(self) -> None:
        if self.slides:
            self.current_slide = (self.current_slide + 1) % len(self.slides)
            self._render_single_slide()

    def _auto_rotate_slide(self) -> None:
        """Cycles the rotation order (ticker_data.rotation): what needs you first and again between every other slide,
        all-fine slides left out. An ALERT stays up twice as long."""
        hold = 1
        if not self.paused and not self._is_hidden_for_fullscreen and self.mode == "single" and self.slides:
            order = rotation(self.slides)
            self._rot_pos = (getattr(self, "_rot_pos", -1) + 1) % len(order)
            self.current_slide = order[self._rot_pos]
            self._render_single_slide()
            hold = 2 if self.slides[self.current_slide].get("level") == "alert" else 1
        self.root.after(int(self.interval * 1000 * hold), self._auto_rotate_slide)

    def _poll_collector(self) -> None:
        if self._start_left_new:           # the taskbar answer arrived (background thread): fit a docked ticker to it
            self._start_left_new = False
            if self.docked and not self.user_hidden and not self._is_hidden_for_fullscreen:
                self._apply_geometry()
        self._on_data_tick()
        is_hidden = self._check_auto_hide()
        poll_ms = 4000 if is_hidden else 2000
        self.root.after(poll_ms, self._poll_collector)

    def _on_data_tick(self) -> None:
        snap = self.collector.get_snapshot()
        if snap.sampled_at > 0:
            if self._prev_snap is not None and self._prev_snap.sampled_at > 0:
                self._check_events(self._prev_snap, snap)
            self._prev_snap = snap
            self.latest_snap = snap
            all_slides = [s for s in format_slides(snap)
                          if not (s["tag"] == "NIGHT" and snap.night and self.settings.get("night_seen") == snap.night["end_iso"])]
            self.slides = [s for s in all_slides if self.slides_enabled.get(s.get("tag", ""), True)] or all_slides
            self.multiline_rows = format_multiline_rows(snap)
            self._render()

    def _tc(self, raw: str | None) -> str | None:
        """A ticker_data color -> this theme's shade of it."""
        t = self.get_theme()
        return {"#38bdf8": t["accent_primary"], "#4ade80": t["accent_green"], "#fbbf24": t["accent_amber"],
                "#f87171": t["accent_red"], "#94a3b8": t["text_dim"], "#e2e8f0": t["text_main"]}.get(raw, raw)

    def _render(self) -> None:
        if self.mode == "single":
            self._render_single_slide()
        else:
            self._render_multiline()

    def _render_single_slide(self) -> None:
        if not self.slides:
            return
        t = self.get_theme()
        idx = self.current_slide % len(self.slides)
        slide = self.slides[idx]

        raw_col = slide.get("color", ACCENT_CYAN)
        col = t["text_muted"] if raw_col == "#94a3b8" else self._tc(raw_col)

        tag_text = "AI ⚡" if (slide["tag"] == "AI" and self.latest_snap.ai_generating) else slide["tag"]
        self.tag_label.configure(text=tag_text, fg=col, bg=t["badge_bg"])
        full_text = slide["text"]
        scale_info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        self.single_text.configure(bg=t["bg"], fg=t["text_main"])
        segs = slide.get("segments") or [(full_text, None)]
        # the text room scales with the monitor and shrinks when a docked ticker is narrowed to fit the taskbar gap
        full_w = int(int(scale_info["width"]) * self._dpi)
        cur_w = self.root.winfo_width() if self.root.winfo_width() > 1 else full_w
        room = int(int(scale_info["max_single_px"]) * self._dpi) - max(0, full_w - cur_w)
        self.single_text.set_segments([(txt, self._tc(c)) for txt, c in segs], max(80, room))

        detail = slide.get("detail", "")
        tip_content = f"{slide['tag']}: {full_text}\n{detail}" if detail else f"{slide['tag']}: {full_text}"
        self.single_tooltip.set_text(tip_content)
        self.tag_tooltip.set_text(f"Click to open {slide['tag']} target")

        # Render dots (e.g. ● ○ ○ ○)
        dots = ["●" if i == idx else "○" for i in range(len(self.slides))]
        dot_color = t["accent_primary"] if self.paused else t["text_dim"]
        self.dots_label.configure(text=" ".join(dots), fg=dot_color, bg=t["bg"])

        # Update 2px VRAM visual meter at bottom
        s = self.latest_snap
        used = s.vram_used_gb or 0.0
        total = s.vram_total_gb or 16.0
        ratio = min(max(used / total, 0.0), 1.0)
        curr_w = self.root.winfo_width() or int(scale_info["width"])
        vram_col = t["accent_red"] if s.vram_evicted else (t["accent_amber"] if s.vram_tight else t["accent_primary"])
        if s.ai_mode == "away" and s.away_fraction is not None:
            ratio, vram_col = min(max(s.away_fraction, 0.0), 1.0), t["accent_green"]   # while Away works: its progress
        bar_w = int(curr_w * ratio)
        self.vram_meter.delete("all")
        self.vram_meter.create_rectangle(0, 0, bar_w, 2, fill=vram_col, width=0)

    def _render_multiline(self) -> None:
        s = self.latest_snap
        t = self.get_theme()
        scale_info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        if s.ai_generating:
            self.mode_badge.configure(text=f"[{s.ai_mode.upper()} ⚡]", fg=t["accent_green"], bg=t["bg"])
        else:
            self.mode_badge.configure(text=f"[{s.ai_mode.upper()}]", fg=t["accent_primary"], bg=t["bg"])
        action = "Click to stop the AI work (the job goes back in the queue)" if s.ai_mode == "away" \
            else "Click to switch to AWAY mode"
        self.mode_badge_tooltip.set_text(f"Current AI mode: {s.ai_mode}\n{action}")
        crashes = s.unexpected_reboots + s.gpu_resets
        alerts = attention(s)
        if crashes:
            self.crash_badge.configure(text=f"CRASH x{crashes} · Away blocked", fg=t["accent_red"], bg=t["bg"], cursor="hand2")
            self.crash_tooltip.set_text(f"{crashes} unexpected reboot(s) / GPU reset(s) since the last review: Away "
                                        "won't start until they're reviewed.\nClick to view the stability reports · "
                                        "Right-click → Quick Actions to mark them reviewed.")
            self.crash_badge.bind("<Button-1>", lambda e: self._open_slide_target("SYS"))
        elif alerts:
            first = alerts[0][1] if len(alerts[0][1]) <= 34 else alerts[0][1][:33] + "…"   # the header is narrow
            self.crash_badge.configure(text=f"⚠ {first}" + (f" (+{len(alerts) - 1})" if len(alerts) > 1 else ""),
                                       fg=self._tc(alerts[0][0]), bg=t["bg"], cursor="hand2")
            self.crash_tooltip.set_text("\n".join(f"• {txt}" for _, txt, _ in alerts))
            self.crash_badge.bind("<Button-1>", lambda e, tag=alerts[0][2]: self._open_slide_target(tag))
        else:
            self.crash_badge.configure(text="", bg=t["bg"])
            self.crash_tooltip.set_text("")

        router_up = s.services.get("Router", False)
        self.status_dot.configure(fg=t["accent_green"] if router_up else t["accent_red"], bg=t["bg"])

        # Update 5 rows
        for i, (lbl_t, lbl_v) in enumerate(self.row_widgets):
            if i < len(self.multiline_rows):
                r = self.multiline_rows[i]
                lbl_t.configure(text=r["title"], fg=t["text_dim"], bg=t["bg"])
                # each item: its name dim, its value in its own color (red worse, green better, ...)
                parts, segs = [], []
                for k, v, c in r["items"]:
                    parts.append(f"{k}: {v}")
                    if segs:
                        segs.append(("  ·  ", t["text_dim"]))
                    segs += [(f"{k}: ", t["text_dim"]), (str(v), self._tc(c))]
                full_row = "  ·  ".join(parts)
                lbl_v.configure(bg=t["bg"], fg=t["text_main"])
                lbl_v.set_segments(segs, int(int(scale_info["max_row_px"]) * self._dpi))
                if i < len(self.row_tooltips):
                    self.row_tooltips[i].set_text(f"{r['title']}: {full_row}")

    def open_web_hud(self) -> None:
        if self.hub:                       # the hub serves the dashboard itself: just open it
            self.hub.open_dashboard()
            return
        open_script = ROOT / "open-hud.ps1"
        if not self.latest_snap.services.get("HUD", False) and open_script.exists():
            import subprocess
            subprocess.Popen(["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", str(open_script)],
                             creationflags=0x08000000)
        else:
            webbrowser.open(HUD_WEB_URL)

    def set_user_hidden(self, hidden: bool) -> None:
        """Hide or show the ticker window (the hub's ✕ / tray / dashboard button). The fullscreen auto-hide stays
        separate: it never brings back a ticker you hid."""
        self.user_hidden = hidden
        if hidden:
            self.root.withdraw()
        elif not self._is_hidden_for_fullscreen:
            self.root.deiconify()
            self._apply_geometry()
            self._render()

    def quit(self, why: str = "closed") -> None:
        if self.hub:
            if why == "✕ button":          # ✕ hides it; the hub keeps running in the tray (and serves the dashboard)
                self.hub.hide_ticker()
            else:
                self.hub.exit(why)
            return
        log_event(f"exit: {why}")
        if self._pulse_timer is not None:
            try:
                self.root.after_cancel(self._pulse_timer)
            except Exception:
                pass
            self._pulse_timer = None
        self.collector.stop()
        self._save_settings()
        try:
            if TRIGGER_FILE.exists():
                TRIGGER_FILE.unlink()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


_instance_lock = None


def main() -> None:
    global _instance_lock

    # Ensure thread is attached to interactive user desktop
    try:
        hdesk = ctypes.windll.user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            ctypes.windll.user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

    # Single-instance enforcement via Windows msvcrt file locking
    _instance_lock = acquire_instance_lock()
    if _instance_lock is None:
        # Instance already running: signal it to bring to front and pulse cyan
        activate_existing_instance()
        sys.exit(0)

    # Set DPI awareness BEFORE creating any Tk instance!
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    import traceback
    if sys.stderr is None or sys.stdout is None:   # pythonw: print()/warnings would raise on a None stream
        stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    sys.excepthook = lambda t, v, tb: log_event("crash: " + "".join(traceback.format_exception(t, v, tb)))
    log_event(f"start (pid {os.getpid()})")
    root = tk.Tk()
    # errors inside Tk callbacks: log them and keep running (Tk's default prints to a stderr pythonw doesn't have)
    root.report_callback_exception = lambda t, v, tb: log_event("error: " + "".join(traceback.format_exception(t, v, tb)))
    app = TickerApp(root)
    try:
        root.mainloop()
    finally:
        log_event("mainloop ended")


if __name__ == "__main__":
    main()
