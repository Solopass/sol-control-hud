"""Draw the SOL Control HUD icon (standard library only) -> sol_control_hud/assets/sol.ico + views/web/static/sol.svg.

A dark rounded square, a progress ring from cyan to green (the Away ring) with a gap at the top right, and an amber sun
in the middle: the app's own colors. Run it again after changing the design:
    .venv\\Scripts\\python.exe scripts\\make_icon.py
"""
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BG, CYAN, GREEN, AMBER = (9, 13, 22), (56, 189, 248), (74, 222, 128), (251, 191, 36)
SIZES = (16, 24, 32, 48, 64, 256)
GAP_FROM, GAP_TO = 20.0, 70.0          # degrees (clockwise from 12 o'clock) left open in the ring


def shade(x: float, y: float) -> tuple[float, float, float, float]:
    """Color at a point of the unit square (0..1): RGBA with alpha 0..1."""
    r = 0.22                                            # rounded corners
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return (0, 0, 0, 0)
    dx, dy = x - 0.5, y - 0.5
    d = math.hypot(dx, dy)
    if d <= 0.12:                                       # the sun
        return (*AMBER, 1.0)
    angle = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
    if 0.28 <= d <= 0.39 and not (GAP_FROM <= angle <= GAP_TO):
        k = ((angle - GAP_TO) % 360) / (360 - (GAP_TO - GAP_FROM))
        return (*(CYAN[i] + (GREEN[i] - CYAN[i]) * k for i in range(3)), 1.0)
    return (*BG, 1.0)


def render(size: int, ss: int = 4) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])                            # PNG filter byte: none
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    r, g, b, a = shade((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size)
                    acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a
            a = acc[3] / (ss * ss)
            rgb = [int(round(c / acc[3])) if acc[3] else 0 for c in acc[:3]]
            row += bytes([*rgb, int(round(a * 255))])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(images: list[tuple[int, bytes]]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(images))
    offset, entries, data = 6 + 16 * len(images), b"", b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return head + entries + data


def svg() -> str:
    # the same drawing as vectors (the ring as an arc path with a gradient)
    def pt(deg: float, r: float) -> str:
        a = math.radians(deg)
        return f"{50 + r * math.sin(a):.2f} {50 - r * math.cos(a):.2f}"
    r = 33.5
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="rgb{CYAN}"/>'
            f'<stop offset="1" stop-color="rgb{GREEN}"/></linearGradient></defs>'
            f'<rect width="100" height="100" rx="22" fill="rgb{BG}"/>'
            f'<path d="M {pt(GAP_TO, r)} A {r} {r} 0 1 1 {pt(GAP_FROM, r)}" fill="none" stroke="url(#g)" stroke-width="11"/>'
            f'<circle cx="50" cy="50" r="12" fill="rgb{AMBER}"/></svg>')


if __name__ == "__main__":
    (ROOT / "sol_control_hud" / "assets").mkdir(exist_ok=True)
    out = ROOT / "sol_control_hud" / "assets" / "sol.ico"
    out.write_bytes(ico([(s, render(s)) for s in SIZES]))
    (ROOT / "sol_control_hud" / "assets" / "sol-256.png").write_bytes(render(256))
    (ROOT / "sol_control_hud" / "views" / "web" / "static" / "sol.svg").write_text(svg(), encoding="utf-8")
    print("wrote", out, out.stat().st_size, "bytes")
