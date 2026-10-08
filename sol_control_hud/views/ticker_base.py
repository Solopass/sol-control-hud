"""The ticker's look and limits: sizes, font scales, themes and their colors, slide labels, its log and alert sound.
Split out of ticker.py on 2026-10-08 (moved verbatim); ticker.py re-exports every name."""
from __future__ import annotations

from pathlib import Path
import threading
import time
try:
    import winsound
except ImportError:
    winsound = None
from ..paths import DATA_DIR


def play_alert_sound(sound_type: int | None = None) -> None:
    if winsound is None:
        return
    st = winsound.MB_ICONASTERISK if sound_type is None else sound_type
    try:
        threading.Thread(target=winsound.MessageBeep, args=(st,), daemon=True).start()
    except Exception:
        pass


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


REVIEWS_DIR = Path(r"D:\OBVLT\1Notebook\Reviews")
REPORTS_DIR = Path(r"D:\OBVLT\reports")
LLM_DIR = Path(r"D:\AI\Cache\llm")
SOL_LLM = Path(r"D:\OBVLT\tools\sol-llm.ps1")


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
    ("sol-fast", "sol-fast (Gemma 4 12B · Fast · 8k)"),
    ("sol-vision", "sol-vision (Gemma 4 12B · Images)"),     # split from sol-fast 2026-10-07: loads the vision module
    ("sol-smart", "sol-smart (gpt-oss-20b · Reasoning)"),
    ("sol-long", "sol-long (gpt-oss-20b · 65k Context)"),
]


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
    "APPS": True,
}

# The slides you can switch on/off (right-click menu and the dashboard's Settings)
SLIDE_LABELS: list[tuple[str, str]] = [
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
    ("APPS", "Running Projects & Media APIs"),
]

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
