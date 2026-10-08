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
import subprocess
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
from .widgets import SegmentedBar, Sparkline

from .ticker_base import (  # noqa: F401 - moved verbatim; old imports keep working
    ACCENT_AMBER, ACCENT_CYAN, ACCENT_GREEN, ACCENT_RED, AI_MODELS, BG_CARD,
    BG_COLOR, BORDER_COLOR, DEFAULT_FONT_SCALE, DEFAULT_SLIDES_ENABLED, DEFAULT_THEME, FONT_SCALES,
    HUD_WEB_URL, LLM_DIR, log_event, LOG_FILE, MULTI_HEIGHT, play_alert_sound,
    REPORTS_DIR, REVIEWS_DIR, SINGLE_HEIGHT, SLIDE_LABELS, SOL_LLM, TEXT_DIM,
    TEXT_MAIN, TEXT_MUTED, THEMES, WIDTH, WINDOW_TITLE,
)
from .ticker_win import (  # noqa: F401 - moved verbatim; old imports keep working
    _MONITORINFO, _START_PS, clamp_rect, covered_by_taskbar, dock_width, dpi_factor,
    get_screen_and_work_area, monitor_dpi, monitor_rects, raise_topmost, start_button_left, TASKBAR_CLASSES,
)
from .ticker_widgets import (  # noqa: F401 - moved verbatim; old imports keep working
    interpolate_color, SegmentLabel, Tooltip, truncate_text,
)
from .ticker_layout import TickerLayoutMixin
from .ticker_render import TickerRenderMixin


# Constants & Paths
SETTINGS_FILE = DATA_DIR / "ticker-settings.json"
LOCK_FILE = DATA_DIR / "ticker.lock"
TRIGGER_FILE = DATA_DIR / "ticker-activate.trigger"
STARTUP_PATH = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SOL Control HUD.lnk"


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


class TickerApp(TickerLayoutMixin, TickerRenderMixin):
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
        self._is_peeking = False
        self._peek_mode = "single"
        self._peek_cancel_timer = None

        # Apply custom collector intervals from settings
        if hasattr(self.collector, "set_intervals"):
            self.collector.set_intervals(
                stability=self.settings.get("stability_interval"),
                system=self.settings.get("scan_interval"),
                git=self.settings.get("scan_interval"),
                monitor_self=self.settings.get("monitor_self", True),
            )
        if hasattr(self.collector, "set_pace"):
            self.collector.set_pace(float(self.settings.get("poll_pace", 2.0)))

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

    def launch_chain(self, chain_path: Path, mode: str = "desk") -> None:
        """Queues a prompt chain for execution by the chain runner daemon (in Desk mode or scheduled for Away)."""
        try:
            from ..chains.chain_note import set_chain_status
            if mode == "away":
                set_chain_status(chain_path, "queued", schedule="on away")
            else:
                set_chain_status(chain_path, "queued")
        except Exception:
            pass
        self.trigger_alert(self.get_theme()["accent_green"], "chain_queued")
        self.root.after(300, self._on_data_tick)

    def open_crash_inspector(self) -> None:
        """Opens the 1-click Crash Inspector dialog."""
        from .crash_dialog import CrashInspectorDialog
        crashes = self.latest_snap.unexpected_reboots + self.latest_snap.gpu_resets
        CrashInspectorDialog(
            parent=self.root,
            theme=self.get_theme(),
            crash_count=crashes,
            on_cleared=self.refresh_data_now,
        )

    def open_settings_dialog(self) -> None:
        """Opens the full performance presets and configuration dialog."""
        from .settings_dialog import SettingsDialog
        SettingsDialog(
            parent=self.root,
            current_settings=self.settings,
            theme=self.get_theme(),
            font_scale=self.font_scale,
            on_save=self.apply_new_settings,
            get_live_overhead=self.get_overhead_stats,
        )

    def open_scratch_dialog(self) -> None:
        """Opens the Quick Scratch note capture dialog."""
        from .scratch_dialog import QuickScratchDialog
        def _on_scratch_saved(path, text):
            self.trigger_alert(self.get_theme()["accent_green"], "note_saved")
            self.refresh_data_now()
        QuickScratchDialog(
            parent=self.root,
            theme=self.get_theme(),
            on_saved=_on_scratch_saved,
        )

    def get_overhead_stats(self) -> dict:
        return {
            "cpu": self.latest_snap.self_cpu,
            "ram_mb": self.latest_snap.self_ram_mb,
            "latency_ms": self.latest_snap.self_latency_ms,
        }

    def apply_new_settings(self, new_s: dict) -> None:
        self.settings.update(new_s)
        self._save_settings()
        pace = float(new_s.get("poll_pace", 2.0))
        if hasattr(self.collector, "set_pace"):
            self.collector.set_pace(pace)
        if hasattr(self.collector, "set_intervals"):
            self.collector.set_intervals(
                stability=new_s.get("stability_interval"),
                system=new_s.get("scan_interval"),
                git=new_s.get("scan_interval"),
                monitor_self=new_s.get("monitor_self"),
            )
        self.interval = int(new_s.get("interval_seconds", 6))
        self.auto_hide_fullscreen = bool(new_s.get("auto_hide_fullscreen", True))
        self.alerts_pulse = bool(new_s.get("alerts_pulse", True))
        self.alerts_sound = bool(new_s.get("alerts_sound", False))
        if "opacity" in new_s:
            self.set_opacity(float(new_s["opacity"]))
        if "theme" in new_s:
            self.set_theme(new_s["theme"])
        self.refresh_data_now()


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
            "poll_pace": 2.0,
            "scan_interval": 60,
            "stability_interval": 300,
            "monitor_self": True,
            "slide_transitions": True,
            "show_sparklines": True,
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
            self.settings["poll_pace"] = float(self.settings.get("poll_pace", 2.0))
            self.settings["scan_interval"] = int(self.settings.get("scan_interval", 60))
            self.settings["stability_interval"] = int(self.settings.get("stability_interval", 300))
            self.settings["monitor_self"] = bool(self.settings.get("monitor_self", True))
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()
            if cur_x > 0 or cur_y > 0:
                self.settings["x"] = cur_x
                self.settings["y"] = cur_y
            SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            pass


        # No Esc-to-quit: the ticker has keyboard focus after any click on it, so an Esc meant for something else
        # closed it for good (09-26). Quit with ✕ or right-click → Exit.


    def _open_slide_target(self, tag: str) -> None:
        tag_u = (tag or "").upper()
        if tag_u in ("APPS", "REVIEW"):
            if self.hub:
                self.hub.open_dashboard()
            else:
                webbrowser.open("http://127.0.0.1:7900/")
        elif "AI" in tag_u:
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
            try:
                if self.root.winfo_exists():
                    self.root.after(0, self._on_data_tick)
            except Exception:
                pass
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
        if self.mode == "mini":
            self._on_mini_enter()

    def _on_mouse_leave(self) -> None:
        self.paused = False
        self.dots_label.configure(fg=self.get_theme()["text_dim"])
        if self.mode == "mini":
            self._on_mini_leave()

    def _on_mini_enter(self) -> None:
        if self._peek_cancel_timer is not None:
            try:
                self.root.after_cancel(self._peek_cancel_timer)
            except Exception:
                pass
            self._peek_cancel_timer = None
        if not self._is_peeking:
            self._is_peeking = True
            self._peek_mode = "single"
            self.mini_frame.pack_forget()
            self.single_frame.pack(fill=tk.BOTH, expand=True)
            self._apply_geometry()
            self._render()

    def _on_mini_leave(self) -> None:
        if self._is_peeking:
            if self._peek_cancel_timer is not None:
                try:
                    self.root.after_cancel(self._peek_cancel_timer)
                except Exception:
                    pass
            self._peek_cancel_timer = self.root.after(450, self._collapse_peek)

    def _collapse_peek(self) -> None:
        self._peek_cancel_timer = None
        if self.mode == "mini" and self._is_peeking:
            self._is_peeking = False
            self.single_frame.pack_forget()
            self.mini_frame.pack(fill=tk.BOTH, expand=True)
            self._apply_geometry()
            self._render()

    def _show_context_menu(self, event) -> None:
        t = self.get_theme()
        menu = tk.Menu(self.root, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        dock_text = "Float Above Taskbar" if self.docked else "Dock in Taskbar"

        # Display Mode submenu
        mode_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        for m_key, m_label in [("mini", "Mini Pill (Peek on Hover)"), ("single", "Single-Line Ticker"), ("multi", "Multi-Line Grid")]:
            mark = "✓ " if m_key == self.mode else "   "
            mode_menu.add_command(label=f"{mark}{m_label}", command=lambda k=m_key: self.set_mode(k))
        menu.add_cascade(label="Display Mode 🪟", menu=mode_menu)

        menu.add_command(label=dock_text, command=self.toggle_dock)
        menu.add_command(label="Next Slide (Space)", command=self._manual_next_slide)
        menu.add_command(label="Settings & Presets ⚙", command=self.open_settings_dialog)
        menu.add_command(label="Quick Scratch Note 📝", command=self.open_scratch_dialog)
        if self.hub:
            menu.add_separator()
            menu.add_command(label="🌐 Open dashboard (browser)", command=self.hub.open_dashboard)
            menu.add_command(label="Hide ticker (stays in the tray)", command=self.hub.hide_ticker)
        menu.add_separator()

        # Quick Actions submenu
        actions_menu = tk.Menu(menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
        actions_menu.add_command(label="📝 Quick Scratch / Note Capture", command=self.open_scratch_dialog)
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
        actions_menu.add_separator()
        actions_menu.add_command(label="⚡ Toggle Floating Exam HUD", command=lambda: self.hub.review_action("hud") if self.hub else None)
        actions_menu.add_command(label="🎯 Set Exam Box & Start", command=lambda: self.hub.review_action("pick") if self.hub else None)
        crashes = self.latest_snap.unexpected_reboots + self.latest_snap.gpu_resets
        if crashes > 0:
            actions_menu.add_separator()
            actions_menu.add_command(label=f"🩺 Inspect & Clear Crashes (x{crashes})", command=self.open_crash_inspector)
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
                c_sub = tk.Menu(chain_menu, tearoff=0, bg=t["card"], fg=t["text_main"], activebackground=t["border"])
                c_sub.add_command(
                    label="▶ Run Now (Desk)",
                    command=lambda p=c_path: self.launch_chain(p, mode="desk"),
                )
                c_sub.add_command(
                    label="🌙 Queue for Away Mode",
                    command=lambda p=c_path: self.launch_chain(p, mode="away"),
                )
                chain_menu.add_cascade(label=f"⚡ {c_name}", menu=c_sub)
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
        for tag, label in SLIDE_LABELS:
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
        if curr.vram_spill_impact == "slow" and prev.vram_spill_impact != "slow":
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

    def set_mode(self, new_mode: str) -> None:
        if new_mode not in ("mini", "single", "multi"):
            new_mode = "single"
        self.mode = new_mode
        self.settings["mode"] = self.mode
        self._is_peeking = False
        if self._peek_cancel_timer is not None:
            try:
                self.root.after_cancel(self._peek_cancel_timer)
            except Exception:
                pass
            self._peek_cancel_timer = None

        self.mini_frame.pack_forget()
        self.single_frame.pack_forget()
        self.multi_frame.pack_forget()

        if self.mode == "mini":
            self.mini_frame.pack(fill=tk.BOTH, expand=True)
        elif self.mode == "single":
            self.single_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.multi_frame.pack(fill=tk.BOTH, expand=True)

        self._apply_geometry()
        self._save_settings()
        self._render()

    def toggle_mode(self) -> None:
        next_mode = {"single": "multi", "multi": "mini", "mini": "single"}.get(self.mode, "single")
        self.set_mode(next_mode)

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
