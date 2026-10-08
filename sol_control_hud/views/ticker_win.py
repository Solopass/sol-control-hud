"""Windows plumbing for the ticker: monitors and work areas, DPI, the taskbar and its Start button, staying on top.
Split out of ticker.py on 2026-10-08 (moved verbatim); ticker.py re-exports every name."""
from __future__ import annotations

import ctypes
from ctypes import wintypes


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
