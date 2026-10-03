"""Settings Dialog for SOL Control HUD.
Allows tuning resource polling frequencies (Eco, Balanced, Turbo presets),
custom sliders, theme and font scaling, and displays live HUD overhead telemetry.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PRESETS = {
    "eco": {
        "name": "🔋 Eco (Ultra-Light)",
        "desc": "Minimal overhead (<0.1% CPU). 5s telemetry, 8s slides, 5m background scans.",
        "poll_pace": 5.0,
        "interval_seconds": 8,
        "scan_interval": 300,
        "stability_interval": 600,
        "monitor_self": False,
    },
    "balanced": {
        "name": "⚖️ Balanced (Default)",
        "desc": "Optimal balance (~0.4% CPU). 2s telemetry, 6s slides, 60s background scans.",
        "poll_pace": 2.0,
        "interval_seconds": 6,
        "scan_interval": 60,
        "stability_interval": 300,
        "monitor_self": True,
    },
    "turbo": {
        "name": "⚡ Turbo (Real-Time)",
        "desc": "High responsiveness (~1% CPU). 1s telemetry, 3s slides, 30s background scans.",
        "poll_pace": 1.0,
        "interval_seconds": 3,
        "scan_interval": 30,
        "stability_interval": 120,
        "monitor_self": True,
    },
}


class SettingsDialog:
    """Dark-mode configuration dialog with polling presets and live resource usage."""

    def __init__(self, parent: tk.Tk | tk.Toplevel, current_settings: dict,
                 theme: dict[str, str], font_scale: str,
                 on_save: callable | None = None,
                 get_live_overhead: callable | None = None):
        self.parent = parent
        self.settings = dict(current_settings)
        self.theme = theme
        self.font_scale = font_scale
        self.on_save = on_save
        self.get_live_overhead = get_live_overhead

        self.win = tk.Toplevel(parent)
        self.win.title("SOL Ticker HUD Settings")
        self.win.configure(bg=theme["bg"])
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.transient(parent)

        self._active_preset = tk.StringVar(value=self._detect_preset())
        self._poll_pace_var = tk.DoubleVar(value=float(self.settings.get("poll_pace", 2.0)))
        self._slide_interval_var = tk.IntVar(value=int(self.settings.get("interval_seconds", 6)))
        self._scan_interval_var = tk.IntVar(value=int(self.settings.get("scan_interval", 60)))
        self._stability_interval_var = tk.IntVar(value=int(self.settings.get("stability_interval", 300)))
        self._monitor_self_var = tk.BooleanVar(value=bool(self.settings.get("monitor_self", True)))
        self._auto_hide_var = tk.BooleanVar(value=bool(self.settings.get("auto_hide_fullscreen", True)))
        self._pulse_var = tk.BooleanVar(value=bool(self.settings.get("alerts_pulse", True)))
        self._sound_var = tk.BooleanVar(value=bool(self.settings.get("alerts_sound", False)))
        self._opacity_var = tk.DoubleVar(value=float(self.settings.get("opacity", 0.94)))
        self._theme_var = tk.StringVar(value=str(self.settings.get("theme", "cyber-cyan")))
        self._transitions_var = tk.BooleanVar(value=bool(self.settings.get("slide_transitions", True)))
        self._sparklines_var = tk.BooleanVar(value=bool(self.settings.get("show_sparklines", True)))

        self._build_ui()
        self._position_window()
        self._update_overhead_meter()

    def _detect_preset(self) -> str:
        pace = float(self.settings.get("poll_pace", 2.0))
        if pace >= 4.5:
            return "eco"
        elif pace <= 1.2:
            return "turbo"
        return "balanced"

    def _apply_preset(self, preset_key: str) -> None:
        p = PRESETS.get(preset_key)
        if not p:
            return
        self._active_preset.set(preset_key)
        self._poll_pace_var.set(p["poll_pace"])
        self._slide_interval_var.set(p["interval_seconds"])
        self._scan_interval_var.set(p["scan_interval"])
        self._stability_interval_var.set(p["stability_interval"])
        self._monitor_self_var.set(p["monitor_self"])
        self._update_slider_labels()

    def _build_ui(self) -> None:
        t = self.theme
        f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_sub = tkfont.Font(family="Segoe UI", size=8)
        f_bold = tkfont.Font(family="Segoe UI", size=8, weight="bold")

        # Header Frame
        header = tk.Frame(self.win, bg=t["card"], padx=14, pady=10)
        header.pack(fill=tk.X)

        title_lbl = tk.Label(
            header, text="⚙️ TICKER HUD SETTINGS & PERFORMANCE",
            bg=t["card"], fg=t["accent_primary"], font=f_title
        )
        title_lbl.pack(side=tk.LEFT)

        btn_x = tk.Label(header, text="✕", bg=t["card"], fg=t["text_dim"], font=f_bold, cursor="hand2")
        btn_x.pack(side=tk.RIGHT)
        btn_x.bind("<Button-1>", lambda e: self.win.destroy())

        # Main Body Notebook/Container
        body = tk.Frame(self.win, bg=t["bg"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        # Section 1: Presets
        sec1 = tk.LabelFrame(
            body, text=" Performance & Polling Presets ",
            bg=t["bg"], fg=t["accent_primary"], font=f_bold, padx=10, pady=8
        )
        sec1.pack(fill=tk.X, pady=(0, 10))

        preset_btn_frame = tk.Frame(sec1, bg=t["bg"])
        preset_btn_frame.pack(fill=tk.X, pady=(0, 6))

        for key, p in PRESETS.items():
            btn = tk.Button(
                preset_btn_frame,
                text=p["name"],
                bg=t["card"],
                fg=t["text_main"],
                activebackground=t["border"],
                font=f_bold,
                relief=tk.RIDGE,
                padx=8,
                pady=4,
                cursor="hand2",
                command=lambda k=key: self._apply_preset(k)
            )
            btn.pack(side=tk.LEFT, padx=(0, 6), expand=True, fill=tk.X)

        self.preset_desc_lbl = tk.Label(
            sec1, text=PRESETS[self._active_preset.get()]["desc"],
            bg=t["bg"], fg=t["text_dim"], font=f_sub, wraplength=440, justify=tk.LEFT
        )
        self.preset_desc_lbl.pack(anchor="w", pady=(2, 6))

        # Sliders Frame
        sliders_frame = tk.Frame(sec1, bg=t["bg"])
        sliders_frame.pack(fill=tk.X)

        # 1. Hardware Poll Rate
        row1 = tk.Frame(sliders_frame, bg=t["bg"])
        row1.pack(fill=tk.X, pady=2)
        self.lbl_pace = tk.Label(row1, text=f"Telemetry Poll Rate: {self._poll_pace_var.get():.1f}s", bg=t["bg"], fg=t["text_main"], font=f_sub, width=28, anchor="w")
        self.lbl_pace.pack(side=tk.LEFT)
        s_pace = tk.Scale(
            row1, from_=0.5, to=10.0, resolution=0.5, orient=tk.HORIZONTAL, showvalue=0,
            variable=self._poll_pace_var, bg=t["bg"], fg=t["text_main"], highlightthickness=0,
            command=lambda v: self._on_slider_moved()
        )
        s_pace.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        # 2. Slide Rotation Interval
        row2 = tk.Frame(sliders_frame, bg=t["bg"])
        row2.pack(fill=tk.X, pady=2)
        self.lbl_slide = tk.Label(row2, text=f"Slide Rotation: {self._slide_interval_var.get()}s", bg=t["bg"], fg=t["text_main"], font=f_sub, width=28, anchor="w")
        self.lbl_slide.pack(side=tk.LEFT)
        s_slide = tk.Scale(
            row2, from_=2, to=15, resolution=1, orient=tk.HORIZONTAL, showvalue=0,
            variable=self._slide_interval_var, bg=t["bg"], fg=t["text_main"], highlightthickness=0,
            command=lambda v: self._on_slider_moved()
        )
        s_slide.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        # 3. Git & Disk Scans
        row3 = tk.Frame(sliders_frame, bg=t["bg"])
        row3.pack(fill=tk.X, pady=2)
        self.lbl_scan = tk.Label(row3, text=f"Git & Disk Scans: {self._scan_interval_var.get()}s", bg=t["bg"], fg=t["text_main"], font=f_sub, width=28, anchor="w")
        self.lbl_scan.pack(side=tk.LEFT)
        s_scan = tk.Scale(
            row3, from_=15, to=300, resolution=15, orient=tk.HORIZONTAL, showvalue=0,
            variable=self._scan_interval_var, bg=t["bg"], fg=t["text_main"], highlightthickness=0,
            command=lambda v: self._on_slider_moved()
        )
        s_scan.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        # Section 2: Behavior & Overhead Monitor
        sec2 = tk.LabelFrame(
            body, text=" Behavior & Self-Overhead ",
            bg=t["bg"], fg=t["accent_primary"], font=f_bold, padx=10, pady=8
        )
        sec2.pack(fill=tk.X, pady=(0, 10))

        chk_self = tk.Checkbutton(
            sec2, text="Monitor Ticker HUD process CPU & RAM overhead",
            variable=self._monitor_self_var, bg=t["bg"], fg=t["text_main"],
            activebackground=t["bg"], activeforeground=t["text_main"],
            selectcolor=t["card"], font=f_sub
        )
        chk_self.pack(anchor="w")

        chk_game = tk.Checkbutton(
            sec2, text="Auto-hide when fullscreen game is running (Game Guard)",
            variable=self._auto_hide_var, bg=t["bg"], fg=t["text_main"],
            activebackground=t["bg"], activeforeground=t["text_main"],
            selectcolor=t["card"], font=f_sub
        )
        chk_game.pack(anchor="w")

        chk_pulse = tk.Checkbutton(
            sec2, text="Visual border glow alerts on events (crashes, chain done, VRAM tight)",
            variable=self._pulse_var, bg=t["bg"], fg=t["text_main"],
            activebackground=t["bg"], activeforeground=t["text_main"],
            selectcolor=t["card"], font=f_sub
        )
        chk_pulse.pack(anchor="w")

        # Section 3: Appearance & Themes
        sec3 = tk.LabelFrame(
            body, text=" Appearance & Themes ",
            bg=t["bg"], fg=t["accent_primary"], font=f_bold, padx=10, pady=8
        )
        sec3.pack(fill=tk.X, pady=(0, 10))

        # Theme Selector Row
        row_theme = tk.Frame(sec3, bg=t["bg"])
        row_theme.pack(fill=tk.X, pady=2)
        tk.Label(row_theme, text="Color Theme:", bg=t["bg"], fg=t["text_main"], font=f_sub, width=16, anchor="w").pack(side=tk.LEFT)

        theme_options = [
            ("cyber-cyan", "Cyber Cyan (Default)"),
            ("high-contrast", "High Contrast OLED"),
            ("amber-terminal", "Amber Terminal (CRT)"),
            ("emerald-matrix", "Emerald Matrix"),
            ("nordic-frost", "Nordic Frost"),
            ("dracula-synth", "Dracula Synthwave"),
        ]
        theme_menu = ttk.Combobox(
            row_theme,
            textvariable=self._theme_var,
            values=[opt[0] for opt in theme_options],
            state="readonly",
            width=22,
        )
        theme_menu.pack(side=tk.LEFT, padx=(0, 10))

        # Opacity Slider Row
        row_op = tk.Frame(sec3, bg=t["bg"])
        row_op.pack(fill=tk.X, pady=2)
        self.lbl_opacity = tk.Label(row_op, text=f"Window Opacity: {int(self._opacity_var.get() * 100)}%", bg=t["bg"], fg=t["text_main"], font=f_sub, width=22, anchor="w")
        self.lbl_opacity.pack(side=tk.LEFT)
        s_op = tk.Scale(
            row_op, from_=0.50, to=1.00, resolution=0.05, orient=tk.HORIZONTAL, showvalue=0,
            variable=self._opacity_var, bg=t["bg"], fg=t["text_main"], highlightthickness=0,
            command=lambda v: self.lbl_opacity.configure(text=f"Window Opacity: {int(self._opacity_var.get() * 100)}%")
        )
        s_op.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        chk_trans = tk.Checkbutton(
            sec3, text="Smooth fluid slide transition animations",
            variable=self._transitions_var, bg=t["bg"], fg=t["text_main"],
            activebackground=t["bg"], activeforeground=t["text_main"],
            selectcolor=t["card"], font=f_sub
        )
        chk_trans.pack(anchor="w")

        chk_sparks = tk.Checkbutton(
            sec3, text="Show live 60s sparkline trendlines (GPU & CPU)",
            variable=self._sparklines_var, bg=t["bg"], fg=t["text_main"],
            activebackground=t["bg"], activeforeground=t["text_main"],
            selectcolor=t["card"], font=f_sub
        )
        chk_sparks.pack(anchor="w")

        # Live Overhead Readout Bar
        self.overhead_frame = tk.Frame(body, bg=t["card"], padx=10, pady=8, highlightbackground=t["border"], highlightthickness=1)
        self.overhead_frame.pack(fill=tk.X, pady=(0, 12))

        self.overhead_lbl = tk.Label(
            self.overhead_frame,
            text="Measuring HUD Overhead...",
            bg=t["card"], fg=t["accent_green"], font=f_bold
        )
        self.overhead_lbl.pack(anchor="w")

        # Action Buttons
        btn_box = tk.Frame(body, bg=t["bg"])
        btn_box.pack(fill=tk.X)

        btn_save = tk.Button(
            btn_box, text="Save & Apply", bg=t["accent_primary"], fg="#000000",
            font=f_bold, relief=tk.FLAT, padx=14, pady=6, cursor="hand2",
            command=self._on_save_clicked
        )
        btn_save.pack(side=tk.LEFT, padx=(0, 8))

        btn_cancel = tk.Button(
            btn_box, text="Cancel", bg=t["card"], fg=t["text_dim"],
            font=f_sub, relief=tk.FLAT, padx=12, pady=6, cursor="hand2",
            command=self.win.destroy
        )
        btn_cancel.pack(side=tk.LEFT)

    def _on_slider_moved(self) -> None:
        self.lbl_pace.configure(text=f"Telemetry Poll Rate: {self._poll_pace_var.get():.1f}s")
        self.lbl_slide.configure(text=f"Slide Rotation: {self._slide_interval_var.get()}s")
        self.lbl_scan.configure(text=f"Git & Disk Scans: {self._scan_interval_var.get()}s")
        self.preset_desc_lbl.configure(text="Custom polling configuration active.")

    def _update_slider_labels(self) -> None:
        self.lbl_pace.configure(text=f"Telemetry Poll Rate: {self._poll_pace_var.get():.1f}s")
        self.lbl_slide.configure(text=f"Slide Rotation: {self._slide_interval_var.get()}s")
        self.lbl_scan.configure(text=f"Git & Disk Scans: {self._scan_interval_var.get()}s")
        p = PRESETS.get(self._active_preset.get())
        if p:
            self.preset_desc_lbl.configure(text=p["desc"])

    def _update_overhead_meter(self) -> None:
        if not self.win.winfo_exists():
            return
        if self.get_live_overhead:
            try:
                stats = self.get_live_overhead()
                cpu = stats.get("cpu", 0.0)
                ram = stats.get("ram_mb", 0.0)
                lat = stats.get("latency_ms", 0.0)
                txt = f"📊 Live HUD Overhead: {cpu:.1f}% CPU  ·  {ram:.1f} MB RAM  ·  Loop Latency: {lat:.1f} ms"
                self.overhead_lbl.configure(text=txt)
            except Exception:
                pass
        self.win.after(2000, self._update_overhead_meter)

    def _position_window(self) -> None:
        self.win.update_idletasks()
        w = 480
        h = self.win.winfo_reqheight()
        px = self.parent.winfo_rootx()
        py = self.parent.winfo_rooty()
        x = max(20, px)
        y = max(40, py - h - 10)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _on_save_clicked(self) -> None:
        new_settings = {
            "poll_pace": self._poll_pace_var.get(),
            "interval_seconds": self._slide_interval_var.get(),
            "scan_interval": self._scan_interval_var.get(),
            "stability_interval": self._stability_interval_var.get(),
            "monitor_self": self._monitor_self_var.get(),
            "auto_hide_fullscreen": self._auto_hide_var.get(),
            "alerts_pulse": self._pulse_var.get(),
            "alerts_sound": self._sound_var.get(),
            "opacity": self._opacity_var.get(),
            "theme": self._theme_var.get(),
            "slide_transitions": self._transitions_var.get(),
            "show_sparklines": self._sparklines_var.get(),
        }
        if self.on_save:
            try:
                self.on_save(new_settings)
            except Exception:
                pass
        self.win.destroy()
