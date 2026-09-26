"""The tray icon (Win32 Shell_NotifyIconW through ctypes: no extra packages).

Runs its own thread with a hidden window and message loop. Menu callbacks run on that thread: the hub only puts
commands on a queue there, the Tk thread does the work. Re-adds itself when Explorer restarts ("TaskbarCreated").
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_NULL, WM_DESTROY, WM_CLOSE, WM_USER = 0x0000, 0x0002, 0x0010, 0x0400
WM_TRAY = WM_USER + 20
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF = {"info": 0x1, "warning": 0x2, "error": 0x3}          # the icon Windows shows next to a notification
NIN_BALLOONUSERCLICK = WM_USER + 5                          # the user clicked the notification
MF_STRING, MF_CHECKED, MF_GRAYED, MF_SEPARATOR = 0x0, 0x8, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80

u32, sh32, k32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                wintypes.HINSTANCE, wintypes.LPVOID]
u32.CreatePopupMenu.restype = wintypes.HMENU
u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.HWND, wintypes.LPVOID]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
sh32.ExtractIconW.restype = wintypes.HICON
sh32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
k32.GetModuleHandleW.restype = wintypes.HMODULE


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


# A menu item: (label, callback, checked) or None for a separator. Built fresh each time the menu opens.
MenuItem = tuple[str, Callable[[], None], bool] | None


class Tray:
    def __init__(self, tooltip: str, menu: Callable[[], list[MenuItem]], on_click: Callable[[], None] | None = None,
                 icon: tuple[str, int] = ("shell32.dll", 238), on_notice_click: Callable[[], None] | None = None):
        self.tooltip, self.menu, self.on_click, self.icon_src = tooltip, menu, on_click, icon
        self.on_notice_click = on_notice_click
        self.hwnd = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._proc = WNDPROC(self._wndproc)   # keep a reference: ctypes callbacks die with their Python object

    def start(self) -> bool:
        self._thread.start()
        return self._ready.wait(5) and bool(self.hwnd)

    def stop(self) -> None:
        if self.hwnd:
            self._notify(NIM_DELETE)
            u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text[:127]
        if self.hwnd:
            self._notify(NIM_MODIFY)

    def notify(self, title: str, text: str, level: str = "info") -> bool:
        """A Windows notification from this tray icon (Windows 11 shows it like any app's; Focus/Do not disturb applies)."""
        if not self.hwnd:
            return False
        return self._notify(NIM_MODIFY, info=(title[:63], text[:255] or " ", NIIF.get(level, 0x1)))

    # ---- window thread
    def _run(self) -> None:
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._proc, hinst, "SolControlHudTray"
        u32.RegisterClassW(ctypes.byref(wc))
        self._taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
        self.hwnd = u32.CreateWindowExW(0, wc.lpszClassName, "SOL Control HUD tray", 0, 0, 0, 0, 0, None, None, hinst, None)
        self._hicon = sh32.ExtractIconW(hinst, self.icon_src[0], self.icon_src[1]) or u32.LoadIconW(None, 32512)
        if self.hwnd:
            self._notify(NIM_ADD)
        self._ready.set()
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _notify(self, action: int, info: tuple[str, str, int] | None = None) -> bool:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage, nid.hIcon, nid.szTip = WM_TRAY, self._hicon, self.tooltip[:127]
        if info:
            nid.uFlags |= NIF_INFO
            nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = info
        return bool(sh32.Shell_NotifyIconW(action, ctypes.byref(nid)))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == WM_RBUTTONUP:
                self._show_menu()
            elif event == WM_LBUTTONUP and self.on_click:
                self._safe(self.on_click)
            elif event == NIN_BALLOONUSERCLICK and self.on_notice_click:
                self._safe(self.on_notice_click)
            return 0
        if msg == getattr(self, "_taskbar_created", -1):   # Explorer restarted: the icon is gone, add it again
            self._notify(NIM_ADD)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        items = self._safe(self.menu) or []
        hmenu = u32.CreatePopupMenu()
        actions: dict[int, Callable[[], None]] = {}
        for i, item in enumerate(items, start=1):
            if item is None:
                u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            label, cb, checked = item
            u32.AppendMenuW(hmenu, MF_STRING | (MF_CHECKED if checked else 0), i, label)
            actions[i] = cb
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)          # without this the menu doesn't close when you click elsewhere
        cmd = u32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u32.DestroyMenu(hmenu)
        if cmd in actions:
            self._safe(actions[cmd])

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - a failing callback must never kill the tray thread
            return None
