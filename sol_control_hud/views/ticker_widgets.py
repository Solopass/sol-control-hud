"""Small Tk pieces the ticker draws with: the colored segment label, the tooltip, text and color helpers.
Split out of ticker.py on 2026-10-08 (moved verbatim); ticker.py re-exports every name."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from .ticker_base import BG_CARD, BORDER_COLOR, TEXT_MAIN
from .ticker_win import get_screen_and_work_area


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
        self._offset_x = 0
        self._anim_timer = None
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

    def set_segments(self, segments: list[tuple[str, str | None]], max_px: int | None = None, animate: bool = False) -> None:
        self._segments = list(segments)
        if max_px is not None:
            self.max_px = max_px
        if animate:
            self.animate_slide_in()
        else:
            self._offset_x = 0
            self._redraw()

    def animate_slide_in(self) -> None:
        if self._anim_timer is not None:
            try:
                self.after_cancel(self._anim_timer)
            except Exception:
                pass
            self._anim_timer = None

        offsets = [14, 7, 2, 0]
        step = 0

        def _step():
            nonlocal step
            if step < len(offsets):
                self._offset_x = offsets[step]
                step += 1
                self._redraw()
                if step < len(offsets):
                    self._anim_timer = self.after(22, _step)
                else:
                    self._anim_timer = None
        _step()

    def _redraw(self) -> None:
        self.delete("all")
        width = self.max_px or self.winfo_width() or 400
        y = max(self.winfo_height(), int(self.cget("height"))) // 2
        x, ell = getattr(self, "_offset_x", 0), self._font.measure("…")
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
