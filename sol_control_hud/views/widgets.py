"""Lightweight Visual Monitoring Widgets for SOL Control HUD.
Includes Sparkline for live 60s trendlines and SegmentedBar for VRAM / thermal representation.
Zero external dependencies; rendered entirely on native Tkinter Canvas.
"""
from __future__ import annotations

import tkinter as tk


class Sparkline(tk.Canvas):
    """A compact, ultra-light historical sparkline canvas (e.g. 50x16 px)."""

    def __init__(
        self,
        master,
        width: int = 46,
        height: int = 15,
        bg: str = "#090d16",
        color: str = "#38bdf8",
        dot_color: str | None = None,
        **kw,
    ):
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
            **kw,
        )
        self.w = width
        self.h = height
        self.color = color
        self.dot_color = dot_color or color
        self._data: list[float] = []
        self._min_val: float | None = None
        self._max_val: float | None = None

    def configure_theme(self, bg: str, color: str, dot_color: str | None = None) -> None:
        self.configure(bg=bg)
        self.color = color
        self.dot_color = dot_color or color
        self.redraw()

    def set_data(
        self,
        values: list[float],
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> None:
        self._data = list(values)
        self._min_val = min_val
        self._max_val = max_val
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        if not self._data:
            return

        w = self.winfo_width() if self.winfo_width() > 1 else getattr(self, "w", 46)
        h = self.winfo_height() if self.winfo_height() > 1 else getattr(self, "h", 15)

        vals = self._data
        if len(vals) < 2:
            y = h / 2.0
            self.create_line(0, y, w, y, fill=self.color, width=1)
            return

        min_y = min(vals) if self._min_val is None else self._min_val
        max_y = max(vals) if self._max_val is None else self._max_val

        # Guard against zero-spread (all values identical)
        if max_y <= min_y:
            y = h / 2.0
            self.create_line(0, y, w, y, fill=self.color, width=1)
            # Dot on the last point
            self.create_oval(w - 2, y - 1, w, y + 1, fill=self.dot_color, outline="")
            return

        pad_y = 2.0
        avail_h = max(2.0, h - (pad_y * 2.0))
        dx = w / float(len(vals) - 1)

        points = []
        for i, v in enumerate(vals):
            # Clamp between min_y and max_y
            clamped = max(min_y, min(max_y, float(v)))
            norm = (clamped - min_y) / (max_y - min_y)
            x = i * dx
            y = h - pad_y - (norm * avail_h)
            points.extend([x, y])

        if len(points) >= 4:
            self.create_line(*points, fill=self.color, width=1.2, smooth=True)

            # Bright endpoint dot on newest sample
            last_x = points[-2]
            last_y = points[-1]
            self.create_oval(
                last_x - 1.5,
                last_y - 1.5,
                last_x + 1.5,
                last_y + 1.5,
                fill=self.dot_color,
                outline="",
            )


class SegmentedBar(tk.Canvas):
    """A horizontal segmented bar (e.g. [AI Model | Other Apps | Free Headroom])."""

    def __init__(self, master, width: int = 200, height: int = 3, bg: str = "#090d16", **kw):
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
            **kw,
        )
        self.w = width
        self.h = height
        self._segments: list[tuple[float, str]] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def set_segments(self, segments: list[tuple[float, str]]) -> None:
        """segments: list of (fraction, color) where sum(fractions) <= 1.0."""
        self._segments = list(segments)
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        if not self._segments:
            return

        w = self.winfo_width() if self.winfo_width() > 1 else getattr(self, "w", 200)
        h = self.winfo_height() if self.winfo_height() > 1 else getattr(self, "h", 3)

        curr_x = 0.0
        for frac, color in self._segments:
            if frac <= 0.0:
                continue
            seg_w = max(1.0, frac * w)
            next_x = min(float(w), curr_x + seg_w)
            self.create_rectangle(
                curr_x, 0, next_x, h, fill=color, outline=""
            )
            curr_x = next_x
            if curr_x >= w:
                break
