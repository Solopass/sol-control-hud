"""Which graphics chip each everyday app uses: the Intel UHD 770 (power saving) or the AMD card (high performance).

**Decided 2026-10-08: everything is set to `amd`.** Measured both ways (reports\\gpu-routing-trade-2026-10-08.md):
pinning apps to the Intel chip changed sol-fast by nothing (87.2 vs 86.5 tok/s warm, no eviction either way) and
left 0.1 GB *less* VRAM for the card, because it moves where an app **renders**, not where its **window surfaces**
live - those have to sit on the adapter that drives the monitor. Unpinning Obsidian cost the Radeon 16 MB. The real
non-AI consumers are `dwm` (3-3.9 GB, immovable) and Brave (~1 GB, and it was never on the Intel).

The original case for pinning (2026-10-07) was that browsers, Discord and Steam held ~5.7 GB and pushed part of
sol-fast into system RAM. That spill is real, but app routing does not fix it: the levers that work are closing
Brave and the uptime/engine VRAM ceiling (reports\\vram-cap-2026-09-27.md - the Vulkan path caps near 12.2 GB at
60 h uptime where ROCm reaches 14.4 GB). That 10-07 note also quoted "~140 tok/s" as healthy for sol-fast; the
HUD's own baseline is **100** (`speed.answer_speed()`), which made the spill look worse than it was.

Prefer `amd` over `auto` for anything that matters: on this hybrid machine "let Windows decide" still left Discord
partly on the Intel after a restart, and the Intel drives no monitor here - everything drawn on it is rendered on
the weaker chip and copied back across to be shown (`data\\collectors\\displays.py` warns when that happens).

This writes the same per-app choice as Windows Settings > System > Display > Graphics: a value per program path
under HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences, "GpuPreference=N;" (0 = let Windows decide,
1 = power saving, 2 = high performance). No admin needed. An app reads it only when it starts. The Intel chip has
to be enabled in the BIOS first (iGPU Multi-Monitor); until then the choice changes nothing.

Only the apps listed here are ever touched, by their own install paths: never a path from the page. Other values
in the key (an ASUS tool's) are left alone, and so are other settings inside a value string.
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

import psutil

REG_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"
CHOICES = {"auto": 0, "intel": 1, "amd": 2}
NAMES = {0: "auto", 1: "intel", 2: "amd"}
INTEL, AMD = 0x8086, 0x1002

PF, PF86 = os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
LOCAL = os.environ.get("LOCALAPPDATA", "")

# label, the program files to set (all of them; Steam's VRAM is in its web helper), what it's for
APPS: list[dict] = [
    {"key": "brave", "label": "Brave", "paths": [rf"{PF}\BraveSoftware\Brave-Browser\Application\brave.exe"],
     "about": "main browser"},
    {"key": "vivaldi", "label": "Vivaldi", "paths": [rf"{PF}\Vivaldi\Application\vivaldi.exe"], "about": "browser"},
    {"key": "edge", "label": "Edge", "paths": [rf"{PF86}\Microsoft\Edge\Application\msedge.exe"], "about": "browser"},
    {"key": "chrome", "label": "Chrome", "paths": [rf"{PF}\Google\Chrome\Application\chrome.exe"], "about": "browser"},
    {"key": "discord", "label": "Discord", "paths": ["<discord>"], "about": "voice, video, screen share"},
    {"key": "steam", "label": "Steam (the window, not games)",
     "paths": [rf"{PF86}\Steam\steam.exe", rf"{PF86}\Steam\bin\cef\cef.win64\steamwebhelper.exe"],
     "about": "store and library; games are separate programs and stay on AMD"},
    {"key": "obsidian", "label": "Obsidian", "paths": [rf"{PF}\Obsidian\Obsidian.exe",
                                                      rf"{LOCAL}\Programs\Obsidian\Obsidian.exe"], "about": "notes"},
    {"key": "vscode", "label": "VS Code", "paths": [rf"{PF}\Microsoft VS Code\Code.exe",
                                                    rf"{LOCAL}\Programs\Microsoft VS Code\Code.exe"], "about": "editor"},
    {"key": "antigravity", "label": "Antigravity", "paths": [rf"{LOCAL}\Programs\antigravity\Antigravity.exe"],
     "about": "editor"},
]


def _discord_path() -> str | None:
    """Discord installs every update into a new app-<version> folder; the newest one is the one that runs."""
    found = glob.glob(os.path.join(LOCAL, "Discord", "app-*", "Discord.exe"))

    def version(p: str) -> tuple:
        m = re.search(r"app-([\d.]+)", p)
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()
    return max(found, key=version) if found else None


def app_paths(app: dict) -> list[str]:
    out = []
    for p in app["paths"]:
        p = _discord_path() if p == "<discord>" else p
        if p and Path(p).is_file():
            out.append(p)
    return out


# ---------------------------------------------------------------- the registry (injectable for tests)
class Registry:
    def read(self) -> dict[str, str]:
        import winreg
        out = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    out[name] = str(value)
                    i += 1
        except OSError:
            pass
        return out

    def write(self, name: str, value: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def parse_pref(value: str | None) -> int | None:
    m = re.search(r"GpuPreference=(\d+)", value or "")
    return int(m.group(1)) if m else None


def with_pref(value: str | None, pref: int) -> str:
    """Set GpuPreference inside a value string, keeping any other settings Windows stored there."""
    parts = [p for p in (value or "").split(";") if p.strip() and not p.strip().startswith("GpuPreference=")]
    return ";".join([f"GpuPreference={pref}", *[p.strip() for p in parts]]) + ";"


# ---------------------------------------------------------------- what the Settings panel shows
def intel_available(adapters=None) -> bool:
    if adapters is None:
        from .data.collectors.budget import adapters as list_adapters
        adapters = list_adapters()
    return any(a.get("vendor") == INTEL for a in adapters)


def _running_paths() -> set[str]:
    out = set()
    for p in psutil.process_iter(["exe"]):
        exe = p.info.get("exe")
        if exe:
            out.add(exe.lower())
    return out


def state(reg: Registry | None = None, adapters=None, running: set[str] | None = None) -> dict:
    reg = reg or Registry()
    values = {k.lower(): v for k, v in reg.read().items()}
    running = _running_paths() if running is None else running
    rows = []
    for app in APPS:
        paths = app_paths(app)
        if not paths:
            continue                                   # not installed: not listed
        prefs = {parse_pref(values.get(p.lower())) for p in paths}
        pref = prefs.pop() if len(prefs) == 1 else -1  # -1: its programs disagree (e.g. set by hand)
        rows.append({"key": app["key"], "label": app["label"], "about": app["about"],
                     "choice": NAMES.get(pref if pref is not None else 0, "mixed"),
                     "running": any(p.lower() in running for p in paths)})
    return {"intel_available": intel_available(adapters), "apps": rows}


def set_choice(keys: list[str], choice: str, reg: Registry | None = None) -> list[str]:
    """Set `choice` for the listed apps (by key). Returns the labels changed."""
    if choice not in CHOICES:
        raise ValueError(f"unknown choice {choice!r}")
    reg = reg or Registry()
    current = {k.lower(): (k, v) for k, v in reg.read().items()}
    by_key = {a["key"]: a for a in APPS}
    changed = []
    for key in keys:
        app = by_key.get(key)
        if app is None:
            raise ValueError(f"unknown app {key!r}")
        for path in app_paths(app):
            name, old = current.get(path.lower(), (path, None))
            new = with_pref(old, CHOICES[choice])
            if new != old:
                reg.write(name, new)
        changed.append(app["label"])
    return changed


def reapply(saved: dict[str, str], reg: Registry | None = None) -> list[str]:
    """At HUD start: put each app's saved choice on its current path (Discord moves to a new folder on update)."""
    done = []
    for key, choice in (saved or {}).items():
        if choice in CHOICES and key in {a["key"] for a in APPS}:
            done += set_choice([key], choice, reg)
    return done
