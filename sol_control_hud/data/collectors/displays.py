"""Which graphics adapters actually drive a monitor, and what is being rendered on one that doesn't.

Why: on 10-08 nine apps (Discord, Steam, Code, Obsidian, Antigravity, Edge, Chrome) were set to "power saving" in
Windows' Graphics settings. On this PC that means the Intel UHD 770 - an adapter with no monitor attached - so every
frame they drew was rendered on the weaker chip and then copied across to the Radeon to be shown. It felt like lag,
it was found by hand, and nothing on the machine said a word. This is the check that says it.

Two ways in, because neither alone is enough: the Windows setting (a registry read, true even while the app is shut)
and what is rendering right now (a pure function over the GPU sampler's counters, so it costs no extra sampling).
Adapters are joined to monitors by name, so two identical cards would share one answer - that can only under-report,
never invent a problem.
"""
from __future__ import annotations

import ctypes
import re
import winreg
from ctypes import wintypes
from typing import Callable

from . import budget, gpu

ATTACHED_TO_DESKTOP = 0x1
MAX_DISPLAY_DEVICES = 64          # EnumDisplayDevices is a loop until it says no: never trust it forever
SOFTWARE_VENDOR = 0x1414          # Microsoft's software renderer: never has a monitor, never a problem
GPU_PREFS = r"Software\Microsoft\DirectX\UserGpuPreferences"
# the one value in that key that is not an app: the global toggles (windowed-game optimizations, Auto HDR)
GLOBAL_VALUE = "DirectXUserGlobalSettings"
POWER_SAVING = 1
PREF_WORDS = {0: "let Windows decide", 1: "power saving", 2: "high performance"}
BUSY_PERCENT = 0.5                # below this a context is merely open, not working
EVERYWHERE = {"dwm", "system", "csrss", "amdrsserv", "amdrssrcext", "llama-server"}  # hold a context on every adapter


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


def attached_adapter_names() -> set[str]:
    """Adapter names that have a monitor attached to the desktop, e.g. {"AMD Radeon RX 9070 XT"}."""
    try:
        user32 = ctypes.WinDLL("user32")
    except OSError:
        return set()
    names: set[str] = set()
    for i in range(MAX_DISPLAY_DEVICES):
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        try:
            if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
                break
        except OSError:
            break
        if d.StateFlags & ATTACHED_TO_DESKTOP:
            names.add(d.DeviceString)
    return names


def adapters(attached: set[str] | None = None, listing: Callable[[], list[dict]] | None = None) -> list[dict]:
    """[{luid, name, vendor, has_display, software}] for every adapter Windows offers apps."""
    attached = attached_adapter_names() if attached is None else attached
    return [{**a, "has_display": a.get("name") in attached, "software": a.get("vendor") == SOFTWARE_VENDOR}
            for a in (listing or budget.adapters)()]


def gpu_preferences(key: str = GPU_PREFS) -> list[dict]:
    """Windows' per-app Graphics settings: [{app, preference, words}]. Empty when nothing has been pinned."""
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                name, value, _ = winreg.EnumValue(k, i)
                if name == GLOBAL_VALUE:
                    continue
                m = re.search(r"GpuPreference=(\d)", str(value))
                pref = int(m.group(1)) if m else 0
                out.append({"app": name.rsplit("\\", 1)[-1], "path": name,
                            "preference": pref, "words": PREF_WORDS.get(pref, str(pref))})
    except OSError:
        return []
    return out


def misplaced(util: dict[str, float], known: list[dict], name_of: Callable[[int], str]) -> list[dict]:
    """Pure: raw `\\GPU Engine(*)` values -> what is rendering 3D on an adapter with no monitor, busiest first.

    Only 3D counts: a stray decode or copy context on another chip is normal, 3D work there is not. Processes that
    keep a context on every adapter are left out - they are not the misconfiguration."""
    dark = {a["luid"]: a["name"] for a in known if not a.get("has_display") and not a.get("software")}
    if not dark:
        return []
    # by name, not by pid: Chromium apps spread one window over a dozen processes and would fill the card with
    # a row each, all saying the same thing about the same app
    totals: dict[tuple[str, str], float] = {}
    procs: dict[tuple[str, str], set[int]] = {}
    for path, value in util.items():
        luid = gpu._luid(path)
        if luid not in dark or gpu._engtype(path) != "3D":
            continue
        pid = gpu._pid(path)
        if pid is None:
            continue
        name = name_of(pid)
        if name.lower() in EVERYWHERE:
            continue
        key = (name, luid)
        totals[key] = totals.get(key, 0.0) + float(value)
        procs.setdefault(key, set()).add(pid)
    rows = [{"name": name, "percent": round(percent, 1), "processes": len(procs[(name, luid)]),
             "adapter": dark[luid], "busy": percent >= BUSY_PERCENT}
            for (name, luid), percent in totals.items()]
    rows.sort(key=lambda r: (-r["percent"], r["name"]))
    return rows


def report(util: dict[str, float] | None = None, name_of: Callable[[int], str] | None = None,
           known: list[dict] | None = None, prefs: list[dict] | None = None) -> dict:
    """What the dashboard card and sol-doctor read."""
    known = adapters() if known is None else known
    real = [a for a in known if not a.get("software")]
    dark = [a for a in real if not a.get("has_display")]
    prefs = gpu_preferences() if prefs is None else prefs
    power_saving = [p["app"] for p in prefs if p["preference"] == POWER_SAVING]
    rows = misplaced(util or {}, known, name_of or (lambda pid: f"pid {pid}"))
    return {
        "available": True,
        "adapters": [{"name": a["name"], "luid": a.get("luid"), "has_display": bool(a.get("has_display"))} for a in real],
        "dark": [a["name"] for a in dark],
        # a power-saving pin only matters when there is a second chip to land on and that chip has no monitor
        "setting_problem": bool(dark and power_saving and len(real) > 1),
        "power_saving_apps": power_saving,
        "rendering": rows,
        "busy": [r for r in rows if r["busy"]],
    }
