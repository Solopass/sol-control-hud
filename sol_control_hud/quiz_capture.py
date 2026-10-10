"""Screen capture for the Quiz helper: one box of the screen with plain Win32 GDI (no Pillow, no new dependency).

The HUD process is per-monitor DPI aware (hub.main), so box coordinates are physical pixels on the virtual screen
(a monitor left of the main one has negative x) and match what Tk reports for the mouse.
"""
from __future__ import annotations

import ctypes
import struct
import zlib
from ctypes import wintypes

SRCCOPY = 0x00CC0020
HALFTONE = 4
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
THUMB_COLS = 160         # the change check compares a small grey copy of the box, not every pixel; at 96 columns
                         # text blurs so much that a whole new question barely registers
MAX_SIDE = 1600          # the model gets at most this many pixels on the long side (it scales down itself anyway)
DIFF_LEVEL = 16          # a thumbnail cell counts as changed when its grey level moved more than this (of 255)


class CaptureError(Exception):
    pass


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_api = None


def _win32():
    """Own WinDLL handles with full 64-bit handle types (the shared ctypes.windll ones truncate HDCs to int)."""
    global _api
    if _api is None:
        user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
        H = wintypes.HANDLE
        user32.GetDC.argtypes, user32.GetDC.restype = [wintypes.HWND], H
        user32.ReleaseDC.argtypes = [wintypes.HWND, H]
        user32.GetSystemMetrics.argtypes, user32.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [H], H
        gdi32.CreateCompatibleBitmap.argtypes, gdi32.CreateCompatibleBitmap.restype = [H, ctypes.c_int, ctypes.c_int], H
        gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [H, H], H
        gdi32.DeleteObject.argtypes = [H]
        gdi32.DeleteDC.argtypes = [H]
        gdi32.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi32.StretchBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi32.SetStretchBltMode.argtypes = [H, ctypes.c_int]
        gdi32.SetBrushOrgEx.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        _api = (user32, gdi32)
    return _api


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, width, height) of all monitors together."""
    user32, _ = _win32()
    return tuple(user32.GetSystemMetrics(i) for i in (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
                                                       SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN))


def grab(rect, out_w: int | None = None, out_h: int | None = None) -> tuple[int, int, bytes]:
    """The box (x, y, w, h) as top-down BGRA bytes, scaled to out_w x out_h (smoothly) when given."""
    x, y, w, h = (int(v) for v in rect)
    if w < 1 or h < 1:
        raise CaptureError("the box is empty")
    ow, oh = out_w or w, out_h or h
    user32, gdi32 = _win32()
    screen = user32.GetDC(None)
    if not screen:
        raise CaptureError("no screen to copy (locked or a secure desktop?)")
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, ow, oh)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if (ow, oh) == (w, h):
            ok = gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, SRCCOPY)
        else:
            gdi32.SetStretchBltMode(mem, HALFTONE)
            gdi32.SetBrushOrgEx(mem, 0, 0, None)
            ok = gdi32.StretchBlt(mem, 0, 0, ow, oh, screen, x, y, w, h, SRCCOPY)
        if not ok:
            raise CaptureError("Windows refused to copy the screen")
        bi = BITMAPINFOHEADER()
        bi.biSize, bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = ctypes.sizeof(bi), ow, -oh, 1, 32
        buf = ctypes.create_string_buffer(ow * oh * 4)
        gdi32.SelectObject(mem, old)            # GetDIBits wants the bitmap out of the DC
        old = None
        if gdi32.GetDIBits(mem, bmp, 0, oh, buf, ctypes.byref(bi), 0) != oh:
            raise CaptureError("couldn't read the copied pixels")
        return ow, oh, buf.raw
    finally:
        if old:
            gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def thumb_size(w: int, h: int, cols: int = THUMB_COLS) -> tuple[int, int]:
    cols = max(8, min(cols, w))
    return cols, max(4, round(h * cols / w))


def grey(bgra: bytes) -> bytes:
    """One grey byte per pixel (ITU-R 601 weights)."""
    b, g, r = bgra[0::4], bgra[1::4], bgra[2::4]
    return bytes((299 * rr + 587 * gg + 114 * bb) // 1000 for rr, gg, bb in zip(r, g, b))


def grab_thumb(rect) -> bytes:
    """A small grey copy of the box: enough to see that a new question came, cheap to take every few seconds."""
    tw, th = thumb_size(int(rect[2]), int(rect[3]))
    return grey(grab(rect, tw, th)[2])


def changed(a: bytes | None, b: bytes | None, level: int = DIFF_LEVEL) -> float:
    """Share of thumbnail cells (0..1) whose grey level moved by more than `level`. Different sizes = all changed."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > level) / len(a)


def png(w: int, h: int, bgra: bytes) -> bytes:
    """Top-down BGRA -> an RGB PNG file (stdlib zlib only)."""
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[i * stride:(i + 1) * stride]) for i in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def grab_png(rect, max_side: int = MAX_SIDE) -> bytes:
    """The box as a PNG for the model, scaled down if its long side is over max_side."""
    w, h = int(rect[2]), int(rect[3])
    k = min(1.0, max_side / max(w, h))
    ow, oh = max(1, round(w * k)), max(1, round(h * k))
    return png(*grab(rect, ow, oh))


def stitch_pngs_vertical(png_list: list[bytes]) -> bytes:
    """Stitches multiple PNG byte sequences vertically into a single seamless PNG."""
    valid_pngs = [p for p in png_list if p and p.startswith(b"\x89PNG\r\n\x1a\n")]
    if not valid_pngs:
        return b""
    if len(valid_pngs) == 1:
        return valid_pngs[0]

    raw_scanlines: list[bytes] = []
    base_w: int | None = None
    total_h = 0

    for p in valid_pngs:
        idx = 8
        w, h = 0, 0
        idat_chunks = []
        while idx < len(p):
            length = struct.unpack(">I", p[idx:idx + 4])[0]
            kind = p[idx + 4:idx + 8]
            cdata = p[idx + 8:idx + 8 + length]
            if kind == b"IHDR":
                w, h = struct.unpack(">II", cdata[:8])
            elif kind == b"IDAT":
                idat_chunks.append(cdata)
            idx += 12 + length

        if w <= 0 or h <= 0 or not idat_chunks:
            continue

        if base_w is None:
            base_w = w
        elif base_w != w:
            # Width mismatch: fallback to returning first image rather than corrupting scanlines
            return valid_pngs[0]

        try:
            decomp = zlib.decompress(b"".join(idat_chunks))
            raw_scanlines.append(decomp)
            total_h += h
        except Exception:
            continue

    if not raw_scanlines or base_w is None or total_h <= 0:
        return valid_pngs[0]

    combined_raw = b"".join(raw_scanlines)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", base_w, total_h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(combined_raw, 6))
        + chunk(b"IEND", b"")
    )
