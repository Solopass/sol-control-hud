"""Draw a box on the screen: a dimmed full-screen layer over every monitor; drag a rectangle, Esc cancels.

Used by the Exam review card ("Set box & start"). Coordinates are physical virtual-screen pixels (the hub is per-monitor
DPI aware), the same ones quiz_capture grabs.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable

MIN_SIDE = 40


def pick_box(parent: tk.Misc, on_done: Callable[[list[int]], None], on_cancel: Callable[[], None] | None = None,
             title: str = "Drag a box around the question and its answers  ·  Esc cancels") -> tk.Toplevel:
    from ..quiz_capture import virtual_screen
    vx, vy, vw, vh = virtual_screen()
    win = tk.Toplevel(parent)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.35)
    win.geometry(f"{vw}x{vh}+{vx}+{vy}")
    cv = tk.Canvas(win, bg="#000000", highlightthickness=0, cursor="crosshair")
    cv.pack(fill=tk.BOTH, expand=True)
    # the hint sits at the top of the main monitor (its top-left is 0,0 on the virtual screen)
    pw = parent.winfo_screenwidth()
    cv.create_text(-vx + pw // 2, -vy + 70, text=title, fill="#e2e8f0", font=("Segoe UI", 20, "bold"))
    st = {"x": 0, "y": 0, "rect": None, "label": None, "done": False}

    def finish(rect=None):
        if st["done"]:
            return
        st["done"] = True
        try:
            win.grab_release()
        except tk.TclError:
            pass
        win.destroy()
        if rect:
            parent.after(250, lambda: on_done(rect))      # let the dim layer leave the screen before the first look
        elif on_cancel:
            on_cancel()

    def press(e):
        st["x"], st["y"] = e.x_root, e.y_root
        for k in ("rect", "label"):
            if st[k]:
                cv.delete(st[k])
        st["rect"] = cv.create_rectangle(0, 0, 0, 0, outline="#38bdf8", width=3, fill="#ffffff")
        st["label"] = cv.create_text(0, 0, text="", fill="#38bdf8", anchor="sw", font=("Segoe UI", 12, "bold"))

    def drag(e):
        if not st["rect"]:
            return
        x0, y0 = min(st["x"], e.x_root) - vx, min(st["y"], e.y_root) - vy
        x1, y1 = max(st["x"], e.x_root) - vx, max(st["y"], e.y_root) - vy
        cv.coords(st["rect"], x0, y0, x1, y1)
        cv.coords(st["label"], x0, y0 - 4)
        cv.itemconfigure(st["label"], text=f"{x1 - x0} × {y1 - y0}")

    def release(e):
        if not st["rect"]:
            return
        x, y = min(st["x"], e.x_root), min(st["y"], e.y_root)
        w, h = abs(e.x_root - st["x"]), abs(e.y_root - st["y"])
        if w >= MIN_SIDE and h >= MIN_SIDE:
            finish([x, y, w, h])

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", release)
    win.bind("<Escape>", lambda e: finish())
    win.bind("<ButtonPress-3>", lambda e: finish())
    win.focus_force()
    try:
        win.grab_set()
    except tk.TclError:
        pass
    return win
