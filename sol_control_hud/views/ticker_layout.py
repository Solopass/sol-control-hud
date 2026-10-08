"""TickerApp's window: size and place it, build its widgets, DPI, dragging, docking.
Split out of ticker.py on 2026-10-08 (methods moved verbatim; TickerApp inherits them)."""
from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import font as tkfont
from .widgets import SegmentedBar, Sparkline
from .ticker_base import DEFAULT_FONT_SCALE, FONT_SCALES, MULTI_HEIGHT, SINGLE_HEIGHT, WIDTH, WINDOW_TITLE
from .ticker_widgets import SegmentLabel, Tooltip
from .ticker_win import clamp_rect, dock_width, dpi_factor, get_screen_and_work_area, monitor_rects, start_button_left


class TickerLayoutMixin:
    """Methods of TickerApp (moved verbatim; TickerApp inherits them)."""

    def _font(self, size: int, weight: str = "normal") -> tkfont.Font:
        key = (size, weight)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(family="Segoe UI", size=max(1, round(size * self._dpi)), weight=weight)
        return self._fonts[key]

    def _apply_dpi(self, factor: float) -> bool:
        """Scale every font to the monitor the ticker is on (the 4K one runs at 150 % next to a 100 % main screen)."""
        if abs(factor - self._dpi) < 0.01:
            return False
        self._dpi = factor
        for (size, _), font in self._fonts.items():
            font.configure(size=max(1, round(size * factor)))
        info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        self.font_single.configure(size=max(1, round(int(info["font_single"]) * factor)))
        self.font_row.configure(size=max(1, round(int(info["font_row"]) * factor)))
        for w in [getattr(self, "single_text", None), *(v for _, v in getattr(self, "row_widgets", []))]:
            if w is not None:
                w.configure(height=w._font.metrics("linespace") + 2)
        return True

    def _refresh_start_left(self, again: bool = True) -> None:
        """Ask (in the background) where the taskbar icons begin; the poll loop picks it up. Every 10 min."""
        def work():
            self._start_left = start_button_left()
            self._start_left_new = True
        threading.Thread(target=work, name="taskbar-gap", daemon=True).start()
        if again:
            self.root.after(600_000, self._refresh_start_left)

    def _configure_window(self) -> None:
        t = self.get_theme()
        self.root.title(WINDOW_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.opacity)
        self.root.configure(bg=t["border"])

        # Apply WS_EX_TOOLWINDOW and clear WS_EX_APPWINDOW to ensure window
        # stays completely out of the Alt+Tab switcher.
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            self.hwnd = hwnd
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _apply_geometry(self, initial: bool = False) -> None:
        scale_info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        probe_x = int(self.settings.get("x") or 0) if initial else self.root.winfo_x()
        probe_y = int(self.settings.get("y") or 0) if initial else self.root.winfo_y()
        self._apply_dpi(dpi_factor(probe_x + 40, probe_y + 10) if (probe_x or probe_y) else 1.0)
        
        effective_mode = self._peek_mode if getattr(self, "_is_peeking", False) else self.mode
        single_h = int(int(scale_info["single_h"]) * self._dpi)
        multi_h = int(int(scale_info["multi_h"]) * self._dpi)

        if effective_mode == "mini":
            w = int(185 * self._dpi)
            h = min(single_h, int(26 * self._dpi))
        elif effective_mode == "single":
            w = int(int(scale_info["width"]) * self._dpi)
            h = single_h
        else:
            w = int(int(scale_info["width"]) * self._dpi)
            h = multi_h

        (left, top, right, bottom), screen_w, screen_h = get_screen_and_work_area()
        taskbar_h = max(screen_h - bottom, 40)
        taskbar_y = bottom

        if self.docked:
            if effective_mode in ("single", "mini"):
                target_h = min(h, taskbar_h - 10)
                y = taskbar_y + (taskbar_h - target_h) // 2
                h = target_h
            else:
                # In multi-mode while docked, pop up above the taskbar
                y = taskbar_y - h - 6
            default_x = left + 80
        else:
            default_x = left + 16
            y = bottom - h - 12

        if initial:
            saved_x = self.settings.get("x")
            saved_y = self.settings.get("y")
            if (saved_x is not None and saved_y is not None and
                    (saved_x > left or saved_y > top) and
                    left <= saved_x <= right - 100 and top <= saved_y <= screen_h - h):
                x = saved_x
                if not self.docked:
                    y = saved_y
            else:
                x = default_x
        else:
            x = self.root.winfo_x()
            if not self.docked:
                curr_y = self.root.winfo_y()
                curr_h = self.root.winfo_height() or h
                y = curr_y + (curr_h - h)
                if y < top:
                    y = top
                if y + h > bottom:
                    y = bottom - h

        if self.docked and effective_mode == "single":
            w = dock_width(x, w, self._start_left, int(260 * self._dpi))   # end before the taskbar icons
        x, y = self._clamp(x, y, w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _clamp(self, x: int, y: int, w: int, h: int) -> tuple[int, int]:
        """Keep the whole window on the monitor it's on. The multi-line panel never covers that monitor's taskbar; the single/mini line may sit inside it only if docked."""
        effective_mode = self._peek_mode if getattr(self, "_is_peeking", False) else self.mode
        (mon, work) = monitor_rects(x + w // 2, y + h // 2)
        max_y = (mon[3] - h) if (effective_mode in ("single", "mini") and self.docked) else (work[3] - h)
        return clamp_rect(x, y, w, h, mon, max_y)

    def _create_widgets(self) -> None:
        t = self.get_theme()
        # Outer border wrapper: pad 1px so root border color (and pulse glow) frames the app
        self.container = tk.Frame(self.root, bg=t["bg"])
        self.container.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # --- Single-line View ---
        self.single_frame = tk.Frame(self.container, bg=t["bg"], height=SINGLE_HEIGHT)

        # VRAM visual meter (2px segmented bar at bottom of single line)
        self.vram_meter = SegmentedBar(
            self.single_frame, height=2, bg=t["bg"]
        )
        self.vram_meter.pack(side=tk.BOTTOM, fill=tk.X)

        # Tag badge
        self.tag_label = tk.Label(
            self.single_frame, text="HW", bg=t["badge_bg"], fg=t["accent_primary"],
            font=self._font(8, "bold"), padx=6, pady=2, cursor="hand2"
        )
        self.tag_label.pack(side=tk.LEFT, padx=(6, 8), pady=4)
        self.tag_tooltip = Tooltip(self.tag_label)

        # Content text
        self.single_text = SegmentLabel(self.single_frame, self.font_single, bg=t["bg"], fg=t["text_main"], cursor="hand2")
        self.single_text.configure(text="Gathering workstation metrics...")
        self.single_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=4)
        self.single_tooltip = Tooltip(self.single_text)

        # Slide indicator dots
        self.dots_label = tk.Label(
            self.single_frame, text="● ○ ○ ○", bg=t["bg"], fg=t["text_dim"],
            font=self._font(7), padx=4, cursor="hand2"
        )
        self.dots_label.pack(side=tk.LEFT, padx=4)
        self.dots_tooltip = Tooltip(self.dots_label)
        self.dots_tooltip.set_text("Click or Space to cycle slides")

        # Toggle to multiline button
        self.btn_expand = tk.Label(
            self.single_frame, text="⊞", bg=t["bg"], fg=t["text_muted"],
            font=self._font(10), padx=4, cursor="hand2"
        )
        self.btn_expand.pack(side=tk.LEFT, padx=(2, 2))
        self.btn_expand.bind("<Button-1>", lambda e: self.set_mode("multi"))
        Tooltip(self.btn_expand).set_text("Switch to Multi-line Grid mode")

        # Toggle to mini pill button
        self.btn_mini = tk.Label(
            self.single_frame, text="▫", bg=t["bg"], fg=t["text_muted"],
            font=self._font(10), padx=4, cursor="hand2"
        )
        self.btn_mini.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_mini.bind("<Button-1>", lambda e: self.set_mode("mini"))
        Tooltip(self.btn_mini).set_text("Switch to Mini Pill mode (peeks on hover)")

        # Close button
        self.btn_close_single = tk.Label(
            self.single_frame, text="✕", bg=t["bg"], fg=t["text_dim"],
            font=self._font(8), padx=6, cursor="hand2"
        )
        self.btn_close_single.pack(side=tk.RIGHT, padx=(0, 4))
        self.btn_close_single.bind("<Button-1>", lambda e: self.quit("✕ button"))

        # --- Mini-line View (Taskbar Mini / Peek Mode) ---
        self.mini_frame = tk.Frame(self.container, bg=t["bg"], height=24)
        self.mini_dot = tk.Label(
            self.mini_frame, text="●", bg=t["bg"], fg=t["accent_green"],
            font=self._font(7)
        )
        self.mini_dot.pack(side=tk.LEFT, padx=(6, 4))
        self.mini_lbl = tk.Label(
            self.mini_frame, text="SOL AI", bg=t["bg"], fg=t["accent_primary"],
            font=self._font(8, "bold"), cursor="hand2"
        )
        self.mini_lbl.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_mini_expand = tk.Label(
            self.mini_frame, text="⊞", bg=t["bg"], fg=t["text_muted"],
            font=self._font(8), padx=4, cursor="hand2"
        )
        self.btn_mini_expand.pack(side=tk.RIGHT, padx=(0, 4))
        self.btn_mini_expand.bind("<Button-1>", lambda e: self.set_mode("single"))
        Tooltip(self.btn_mini_expand).set_text("Expand to Single-line Ticker")

        # --- Multi-line View ---
        self.multi_frame = tk.Frame(self.container, bg=t["bg"], padx=8, pady=6)

        # Multi-line Header
        self.header_frame = tk.Frame(self.multi_frame, bg=t["bg"])
        self.header_frame.pack(fill=tk.X, pady=(0, 4))

        self.title_label = tk.Label(
            self.header_frame, text="SOL WORKSTATION", bg=t["bg"], fg=t["text_muted"],
            font=self._font(8, "bold")
        )
        self.title_label.pack(side=tk.LEFT)

        self.status_dot = tk.Label(
            self.header_frame, text="●", bg=t["bg"], fg=t["accent_green"],
            font=self._font(7)
        )
        self.status_dot.pack(side=tk.LEFT, padx=(4, 6))

        self.mode_badge = tk.Label(
            self.header_frame, text="[DESK]", bg=t["bg"], fg=t["accent_primary"],
            font=self._font(7, "bold"), cursor="hand2"
        )
        self.mode_badge.pack(side=tk.LEFT)
        self.mode_badge_tooltip = Tooltip(self.mode_badge)
        self.mode_badge.bind("<Button-1>", lambda e: self.toggle_ai_mode())

        # Crash warning (unexpected reboots / GPU driver resets since the baseline or the last acknowledgement)
        self.crash_badge = tk.Label(
            self.header_frame, text="", bg=t["bg"], fg=t["accent_red"],
            font=self._font(7, "bold")
        )
        self.crash_badge.pack(side=tk.LEFT, padx=(6, 0))
        self.crash_tooltip = Tooltip(self.crash_badge)

        # Header controls
        self.btn_close_multi = tk.Label(
            self.header_frame, text="✕", bg=t["bg"], fg=t["text_dim"],
            font=self._font(8), padx=4, cursor="hand2"
        )
        self.btn_close_multi.pack(side=tk.RIGHT)
        self.btn_close_multi.bind("<Button-1>", lambda e: self.quit("✕ button"))

        self.btn_collapse = tk.Label(
            self.header_frame, text="⊟", bg=t["bg"], fg=t["text_muted"],
            font=self._font(10), padx=6, cursor="hand2"
        )
        self.btn_collapse.pack(side=tk.RIGHT)
        self.btn_collapse.bind("<Button-1>", lambda e: self.toggle_mode())

        self.btn_settings = tk.Label(
            self.header_frame, text="⚙", bg=t["bg"], fg=t["text_muted"],
            font=self._font(8), padx=4, cursor="hand2"
        )
        self.btn_settings.pack(side=tk.RIGHT, padx=2)
        self.btn_settings.bind("<Button-1>", lambda e: self.open_settings_dialog())
        Tooltip(self.btn_settings).set_text("Settings & Polling Presets")

        self.btn_scratch = tk.Label(
            self.header_frame, text="📝", bg=t["bg"], fg=t["text_muted"],
            font=self._font(8), padx=4, cursor="hand2"
        )
        self.btn_scratch.pack(side=tk.RIGHT, padx=2)
        self.btn_scratch.bind("<Button-1>", lambda e: self.open_scratch_dialog())
        Tooltip(self.btn_scratch).set_text("Quick Scratch / Note Capture")

        self.btn_web = tk.Label(
            self.header_frame, text="↗ Web HUD", bg=t["bg"], fg=t["accent_primary"],
            font=self._font(8), padx=6, cursor="hand2"
        )
        self.btn_web.pack(side=tk.RIGHT, padx=4)
        self.btn_web.bind("<Button-1>", lambda e: self.open_web_hud())

        # Separator line
        self.sep = tk.Frame(self.multi_frame, bg=t["border"], height=1)
        self.sep.pack(fill=tk.X, pady=(0, 6))

        # Data Rows (5 rows: HARDWARE, LOCAL AI, CHAINS, SERVICES, SYSTEM)
        self.row_widgets: list[tuple[tk.Label, tk.Label]] = []
        self.row_tooltips: list[Tooltip] = []
        for row_idx in range(5):
            rf = tk.Frame(self.multi_frame, bg=t["bg"], cursor="hand2")
            rf.pack(fill=tk.X, pady=1)

            lbl_title = tk.Label(
                rf, text="", bg=t["bg"], fg=t["text_dim"], font=self._font(7, "bold"),
                width=10, anchor="w", cursor="hand2"
            )
            lbl_title.pack(side=tk.LEFT)

            if row_idx == 0:
                self.spark_gpu = Sparkline(rf, width=42, height=14, bg=t["bg"], color=t["accent_primary"])
                self.spark_gpu.pack(side=tk.RIGHT, padx=4)
                Tooltip(self.spark_gpu).set_text("GPU Load History (last 60s)")
            elif row_idx == 3:
                self.spark_cpu = Sparkline(rf, width=42, height=14, bg=t["bg"], color=t["accent_green"])
                self.spark_cpu.pack(side=tk.RIGHT, padx=4)
                Tooltip(self.spark_cpu).set_text("CPU Usage History (last 60s)")

            lbl_val = SegmentLabel(rf, self.font_row, bg=t["bg"], fg=t["text_main"], cursor="hand2")
            lbl_val.pack(side=tk.LEFT, fill=tk.X, expand=True)

            self.row_tooltips.append(Tooltip(lbl_val))
            self.row_widgets.append((lbl_title, lbl_val))

            if row_idx == 0:
                self.vram_segmented = SegmentedBar(self.multi_frame, height=3, bg=t["border"])
                self.vram_segmented.pack(fill=tk.X, pady=(1, 2))
                Tooltip(self.vram_segmented).set_text("VRAM: AI Model (Cyan) · Other Apps (Purple) · Free (Dark)")

            # Bind row click to its target action
            idx_capture = row_idx
            for w in (rf, lbl_title, lbl_val):
                w.bind("<Button-1>", lambda e, idx=idx_capture: self._on_row_click(idx))
                w.bind("<Button-3>", self._show_context_menu)
                w.bind("<Double-Button-1>", lambda e: self.open_web_hud())

        # Display initial mode
        if self.mode == "mini":
            self.mini_frame.pack(fill=tk.BOTH, expand=True)
        elif self.mode == "single":
            self.single_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.multi_frame.pack(fill=tk.BOTH, expand=True)

    def _bind_events(self) -> None:
        # Dragging on container and frames
        for w in [self.container, self.single_frame, self.multi_frame, self.header_frame,
                  self.single_text, self.title_label, self.mini_frame, self.mini_lbl]:
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._stop_drag)
            w.bind("<Double-Button-1>", lambda e: self.open_web_hud())
            w.bind("<Button-3>", self._show_context_menu)

        # Single click on ticker text advances slide; clicking tag opens slide target
        self.single_text.bind("<Button-1>", lambda e: self._on_text_click())
        self.tag_label.bind("<Button-1>", lambda e: self._on_tag_click())
        self.dots_label.bind("<Button-1>", lambda e: self._on_text_click())

        # Pause rotation and trigger peek on mouse hover
        self.root.bind("<Enter>", lambda e: self._on_mouse_enter())
        self.root.bind("<Leave>", lambda e: self._on_mouse_leave())
        for w in [self.mini_frame, self.mini_dot, self.mini_lbl, self.btn_mini_expand]:
            w.bind("<Enter>", lambda e: self._on_mouse_enter())
            w.bind("<Leave>", lambda e: self._on_mouse_leave())

        # Keybindings
        self.root.bind("<t>", lambda e: self.toggle_mode())
        self.root.bind("<T>", lambda e: self.toggle_mode())
        self.root.bind("<space>", lambda e: self._manual_next_slide())

    def _start_drag(self, event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()
        self._dragged = False
        self._press_pos = (event.x_root, event.y_root)

    def _on_drag(self, event) -> None:
        dx = abs(event.x_root - self._press_pos[0])
        dy = abs(event.y_root - self._press_pos[1])
        if dx > 3 or dy > 3:
            self._dragged = True
        new_x = event.x_root - self._drag_x
        new_y = event.y_root - self._drag_y
        self.root.geometry(f"+{new_x}+{new_y}")

    def _stop_drag(self, event) -> None:
        w, h = self.root.winfo_width() or WIDTH, self.root.winfo_height() or SINGLE_HEIGHT
        x, y = self._clamp(self.root.winfo_x(), self.root.winfo_y(), w, h)
        if (x, y) != (self.root.winfo_x(), self.root.winfo_y()):
            self.root.geometry(f"+{x}+{y}")
        if abs(dpi_factor(x + w // 2, y + h // 2) - self._dpi) >= 0.01:
            self._apply_geometry()         # dropped on a monitor with another scale (the 4K one): redraw at its size
        self._save_settings()

    def set_opacity(self, val: float) -> None:
        self.opacity = val
        self.root.attributes("-alpha", self.opacity)
        self.settings["opacity"] = self.opacity
        self._save_settings()

    def toggle_dock(self) -> None:
        self.docked = not self.docked
        if self.docked:
            self._refresh_start_left(again=False)
        self.settings["docked"] = self.docked
        self._apply_geometry()
        self._save_settings()

    def _reset_position(self) -> None:
        self.docked = False
        self.settings["docked"] = False
        (left, top, right, bottom), screen_w, screen_h = get_screen_and_work_area()
        h = SINGLE_HEIGHT if self.mode == "single" else MULTI_HEIGHT
        x = left + 16
        y = bottom - h - 12
        self.root.geometry(f"+{x}+{y}")
        self._save_settings()
