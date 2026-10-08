"""TickerApp's drawing: the theme, the single-line, mini and multi-line views, the alert pulse.
Split out of ticker.py on 2026-10-08 (methods moved verbatim; TickerApp inherits them)."""
from __future__ import annotations

import tkinter as tk
try:
    import winsound
except ImportError:
    winsound = None
from ..data.snapshot import attention
from .ticker_base import ACCENT_CYAN, DEFAULT_FONT_SCALE, FONT_SCALES, play_alert_sound
from .ticker_widgets import interpolate_color


class TickerRenderMixin:
    """Methods of TickerApp (moved verbatim; TickerApp inherits them)."""

    def _apply_theme(self) -> None:
        """Updates colors across all existing Tkinter widgets live without reloading."""
        t = self.get_theme()
        self.root.configure(bg=t["border"])
        self.container.configure(bg=t["bg"])
        self.single_frame.configure(bg=t["bg"])
        self.vram_meter.configure(bg=t["bg"])
        self.tag_label.configure(bg=t["badge_bg"])
        self.single_text.configure(bg=t["bg"], fg=t["text_main"])
        self.dots_label.configure(bg=t["bg"], fg=t["text_dim"])
        self.btn_expand.configure(bg=t["bg"], fg=t["text_muted"])
        self.btn_close_single.configure(bg=t["bg"], fg=t["text_dim"])
        self.multi_frame.configure(bg=t["bg"])
        self.header_frame.configure(bg=t["bg"])
        self.title_label.configure(bg=t["bg"], fg=t["text_muted"])
        self.mode_badge.configure(bg=t["bg"])
        self.crash_badge.configure(bg=t["bg"])
        self.btn_close_multi.configure(bg=t["bg"], fg=t["text_dim"])
        self.btn_collapse.configure(bg=t["bg"], fg=t["text_muted"])
        if hasattr(self, "btn_settings"):
            self.btn_settings.configure(bg=t["bg"], fg=t["text_muted"])
        if hasattr(self, "btn_scratch"):
            self.btn_scratch.configure(bg=t["bg"], fg=t["text_muted"])
        if hasattr(self, "btn_mini"):
            self.btn_mini.configure(bg=t["bg"], fg=t["text_muted"])
        if hasattr(self, "mini_frame"):
            self.mini_frame.configure(bg=t["bg"])
            self.mini_lbl.configure(bg=t["bg"])
            self.btn_mini_expand.configure(bg=t["bg"], fg=t["text_muted"])
        if hasattr(self, "spark_gpu"):
            self.spark_gpu.configure_theme(bg=t["bg"], color=t["accent_primary"])
        if hasattr(self, "spark_cpu"):
            self.spark_cpu.configure_theme(bg=t["bg"], color=t["accent_green"])
        if hasattr(self, "vram_segmented"):
            self.vram_segmented.configure(bg=t["border"])
        self.btn_web.configure(bg=t["bg"], fg=t["accent_primary"])
        self.sep.configure(bg=t["border"])
        for lbl_title, lbl_val in self.row_widgets:
            lbl_title.configure(bg=t["bg"], fg=t["text_dim"])
            lbl_val.configure(bg=t["bg"], fg=t["text_main"])
            parent_frame = lbl_title.master
            if parent_frame:
                parent_frame.configure(bg=t["bg"])
        for tt in (self.tag_tooltip, self.single_tooltip, self.dots_tooltip,
                   self.mode_badge_tooltip, self.crash_tooltip, *self.row_tooltips):
            tt.widget._tip_bg = t["card"]
            tt.widget._tip_fg = t["text_main"]
            tt.widget._tip_border = t["border"]
        self._render()

    def trigger_alert(self, color: str | None = None, event_type: str = "info") -> None:
        """Triggers an ambient visual border pulse and optional audio chime."""
        t = self.get_theme()
        chosen_color = t["accent_green"] if color is None else color
        if self.alerts_sound and winsound is not None:
            st = winsound.MB_ICONEXCLAMATION if chosen_color == t["accent_red"] else winsound.MB_ICONASTERISK
            play_alert_sound(st)

        if not self.alerts_pulse:
            return

        if self._pulse_timer is not None:
            try:
                self.root.after_cancel(self._pulse_timer)
            except Exception:
                pass
            self._pulse_timer = None

        self._pulse_color = chosen_color
        self._pulse_step = 0
        self._animate_pulse()

    def _animate_pulse(self) -> None:
        total_steps = 12
        step = self._pulse_step
        base_border = self.get_theme()["border"]
        if step >= total_steps:
            self.root.configure(bg=base_border)
            self._pulse_timer = None
            return

        if step < 2:
            cur_color = self._pulse_color
        else:
            factor = (step - 2) / float(total_steps - 2)
            cur_color = interpolate_color(self._pulse_color, base_border, factor)

        self.root.configure(bg=cur_color)
        self._pulse_step += 1
        self._pulse_timer = self.root.after(50, self._animate_pulse)

    def _tc(self, raw: str | None) -> str | None:
        """A ticker_data color -> this theme's shade of it."""
        t = self.get_theme()
        return {"#38bdf8": t["accent_primary"], "#4ade80": t["accent_green"], "#fbbf24": t["accent_amber"],
                "#f87171": t["accent_red"], "#94a3b8": t["text_dim"], "#e2e8f0": t["text_main"]}.get(raw, raw)

    def _render(self) -> None:
        if self.mode == "mini" and not getattr(self, "_is_peeking", False):
            self._render_mini()
        elif self.mode == "single" or getattr(self, "_is_peeking", False):
            self._render_single_slide()
        else:
            self._render_multiline()

    def _render_mini(self) -> None:
        s = self.latest_snap
        t = self.get_theme()
        router_up = s.services.get("Router", False)
        dot_col = t["accent_green"] if router_up else t["accent_red"]
        self.mini_dot.configure(fg=dot_col, bg=t["bg"])

        crashes = s.unexpected_reboots + s.gpu_resets
        if crashes > 0:
            txt = f"[CRASH x{crashes}]"
            col = t["accent_red"]
        elif s.ai_generating:
            txt = f"{s.ai_model or 'AI'} ⚡"
            col = t["accent_green"]
        elif s.gpu_temp is not None:
            txt = f"{s.ai_model or 'SOL'} · {s.gpu_temp}°C"
            col = t["accent_primary"]
        else:
            txt = f"{s.ai_model or 'SOL AI'}"
            col = t["text_main"]
        self.mini_lbl.configure(text=txt, fg=col, bg=t["bg"])

    def _render_single_slide(self) -> None:
        if not self.slides:
            return
        t = self.get_theme()
        idx = self.current_slide % len(self.slides)
        slide = self.slides[idx]

        raw_col = slide.get("color", ACCENT_CYAN)
        col = t["text_muted"] if raw_col == "#94a3b8" else self._tc(raw_col)

        tag_text = "AI ⚡" if (slide["tag"] == "AI" and self.latest_snap.ai_generating) else slide["tag"]
        self.tag_label.configure(text=tag_text, fg=col, bg=t["badge_bg"])
        full_text = slide["text"]
        scale_info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        self.single_text.configure(bg=t["bg"], fg=t["text_main"])
        segs = slide.get("segments") or [(full_text, None)]
        # the text room scales with the monitor and shrinks when a docked ticker is narrowed to fit the taskbar gap
        full_w = int(int(scale_info["width"]) * self._dpi)
        cur_w = self.root.winfo_width() if self.root.winfo_width() > 1 else full_w
        room = int(int(scale_info["max_single_px"]) * self._dpi) - max(0, full_w - cur_w)
        animate = bool(self.settings.get("slide_transitions", True))
        self.single_text.set_segments([(txt, self._tc(c)) for txt, c in segs], max(80, room), animate=animate)

        detail = slide.get("detail", "")
        tip_content = f"{slide['tag']}: {full_text}\n{detail}" if detail else f"{slide['tag']}: {full_text}"
        self.single_tooltip.set_text(tip_content)
        self.tag_tooltip.set_text(f"Click to open {slide['tag']} target")

        # Render dots (e.g. ● ○ ○ ○)
        dots = ["●" if i == idx else "○" for i in range(len(self.slides))]
        dot_color = t["accent_primary"] if self.paused else t["text_dim"]
        self.dots_label.configure(text=" ".join(dots), fg=dot_color, bg=t["bg"])

        # Update 2px segmented VRAM visual meter at bottom
        s = self.latest_snap
        tot = s.vram_total_gb or 16.0
        if s.ai_mode == "away" and s.away_fraction is not None:
            ratio = min(max(s.away_fraction, 0.0), 1.0)
            self.vram_meter.set_segments([(ratio, t["accent_green"])])
        elif s.vram_spill_impact == "slow":
            ratio = min(max((s.vram_used_gb or 0.0) / tot, 0.0), 1.0)
            self.vram_meter.set_segments([(ratio, t["accent_red"])])
        elif s.vram_evicted or s.vram_tight:
            ratio = min(max((s.vram_used_gb or 0.0) / tot, 0.0), 1.0)
            self.vram_meter.set_segments([(ratio, t["accent_amber"])])
        else:
            m_frac = min(max(s.vram_model_gb / tot, 0.0), 1.0)
            o_frac = min(max(s.vram_other_gb / tot, 0.0), 1.0)
            self.vram_meter.set_segments([
                (m_frac, t["accent_primary"]),
                (o_frac, "#a855f7"),
            ])

    def _render_multiline(self) -> None:
        s = self.latest_snap
        t = self.get_theme()
        scale_info = FONT_SCALES.get(self.font_scale, FONT_SCALES[DEFAULT_FONT_SCALE])
        if s.ai_generating:
            self.mode_badge.configure(text=f"[{s.ai_mode.upper()} ⚡]", fg=t["accent_green"], bg=t["bg"])
        else:
            self.mode_badge.configure(text=f"[{s.ai_mode.upper()}]", fg=t["accent_primary"], bg=t["bg"])
        action = "Click to stop the AI work (the job goes back in the queue)" if s.ai_mode == "away" \
            else "Click to switch to AWAY mode"
        self.mode_badge_tooltip.set_text(f"Current AI mode: {s.ai_mode}\n{action}")
        crashes = s.unexpected_reboots + s.gpu_resets
        alerts = attention(s)
        if crashes:
            self.crash_badge.configure(text=f"CRASH x{crashes} · Away blocked", fg=t["accent_red"], bg=t["bg"], cursor="hand2")
            self.crash_tooltip.set_text(f"{crashes} unexpected reboot(s) / GPU reset(s) since the last review: Away "
                                        "won't start until they're reviewed.\nClick to inspect & clear crash alert (unblocks Away mode) · "
                                        "Right-click → Quick Actions to mark them reviewed.")
            self.crash_badge.bind("<Button-1>", lambda e: self.open_crash_inspector())
        elif alerts:
            first = alerts[0][1] if len(alerts[0][1]) <= 34 else alerts[0][1][:33] + "…"   # the header is narrow
            self.crash_badge.configure(text=f"⚠ {first}" + (f" (+{len(alerts) - 1})" if len(alerts) > 1 else ""),
                                       fg=self._tc(alerts[0][0]), bg=t["bg"], cursor="hand2")
            self.crash_tooltip.set_text("\n".join(f"• {txt}" for _, txt, _ in alerts))
            self.crash_badge.bind("<Button-1>", lambda e, tag=alerts[0][2]: self._open_slide_target(tag))
        else:
            self.crash_badge.configure(text="", bg=t["bg"])
            self.crash_tooltip.set_text("")

        router_up = s.services.get("Router", False)
        self.status_dot.configure(fg=t["accent_green"] if router_up else t["accent_red"], bg=t["bg"])

        # Update 5 rows
        for i, (lbl_t, lbl_v) in enumerate(self.row_widgets):
            if i < len(self.multiline_rows):
                r = self.multiline_rows[i]
                lbl_t.configure(text=r["title"], fg=t["text_dim"], bg=t["bg"])
                # each item: its name dim, its value in its own color (red worse, green better, ...)
                parts, segs = [], []
                for k, v, c in r["items"]:
                    parts.append(f"{k}: {v}")
                    if segs:
                        segs.append(("  ·  ", t["text_dim"]))
                    segs += [(f"{k}: ", t["text_dim"]), (str(v), self._tc(c))]
                full_row = "  ·  ".join(parts)
                lbl_v.configure(bg=t["bg"], fg=t["text_main"])
                lbl_v.set_segments(segs, int(int(scale_info["max_row_px"]) * self._dpi))
                if i < len(self.row_tooltips):
                    self.row_tooltips[i].set_text(f"{r['title']}: {full_row}")

        # Update live sparklines and segmented VRAM
        if self.settings.get("show_sparklines", True):
            if hasattr(self, "spark_gpu"):
                if not self.spark_gpu.winfo_manager():
                    self.spark_gpu.pack(side=tk.RIGHT, padx=4)
                if s.gpu_history:
                    self.spark_gpu.set_data(s.gpu_history, min_val=0.0, max_val=100.0)
            if hasattr(self, "spark_cpu"):
                if not self.spark_cpu.winfo_manager():
                    self.spark_cpu.pack(side=tk.RIGHT, padx=4)
                if s.cpu_history:
                    self.spark_cpu.set_data(s.cpu_history, min_val=0.0, max_val=100.0)
        else:
            if hasattr(self, "spark_gpu") and self.spark_gpu.winfo_manager():
                self.spark_gpu.pack_forget()
            if hasattr(self, "spark_cpu") and self.spark_cpu.winfo_manager():
                self.spark_cpu.pack_forget()

        if hasattr(self, "vram_segmented") and s.vram_total_gb:
            tot = s.vram_total_gb
            m_frac = min(max(s.vram_model_gb / tot, 0.0), 1.0)
            o_frac = min(max(s.vram_other_gb / tot, 0.0), 1.0)
            self.vram_segmented.set_segments([
                (m_frac, t["accent_primary"]),
                (o_frac, "#a855f7"),
            ])
