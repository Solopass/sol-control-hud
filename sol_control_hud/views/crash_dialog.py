"""Crash Inspector Dialog for SOL Control HUD.
Displays structured details for recent unacknowledged crash events (BSOD, GPU TDR, Event 41),
and provides a 1-click Acknowledge & Clear action to unblock Away mode.
"""
from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont
import webbrowser

CRASH_WATCH_PATH = Path(r"D:\OBVLT\reports\crash-watch.md")
ACK_FILE = Path(r"D:\OBVLT\reports\stability-ack.json")


def parse_unacknowledged_crashes(report_path: Path = CRASH_WATCH_PATH) -> list[dict[str, str]]:
    """Parses crash-watch.md to extract rows marked as **NEW**."""
    if not report_path.exists():
        return []
    new_events = []
    try:
        text = report_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "| **NEW** |" in line or "| NEW |" in line:
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 4:
                    new_events.append({
                        "time": parts[0].replace("**", "").strip(),
                        "event": parts[2].replace("**", "").strip(),
                        "detail": parts[3].replace("**", "").strip(),
                        "resume": parts[4].replace("**", "").strip() if len(parts) > 4 else "-",
                    })
    except Exception:
        pass
    return new_events


class CrashInspectorDialog:
    """Dark-mode popup dialog for inspecting and clearing system crash events."""

    def __init__(self, parent: tk.Tk | tk.Toplevel, theme: dict[str, str],
                 crash_count: int = 1, on_cleared: callable | None = None):
        self.parent = parent
        self.theme = theme
        self.crash_count = crash_count
        self.on_cleared = on_cleared

        self.win = tk.Toplevel(parent)
        self.win.title("SOL Stability & Crash Inspector")
        self.win.configure(bg=theme["bg"])
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)

        # Remove system window styling / use clean appearance
        self.win.transient(parent)

        self._build_ui()
        self._position_window()

    def _build_ui(self) -> None:
        t = self.theme
        f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_sub = tkfont.Font(family="Segoe UI", size=8)
        f_bold = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        f_mono = tkfont.Font(family="Consolas", size=8)

        # Header Frame
        header = tk.Frame(self.win, bg=t["card"], padx=14, pady=10)
        header.pack(fill=tk.X)

        title_lbl = tk.Label(
            header,
            text=f"⚠️ STABILITY & CRASH ALERT ({self.crash_count} EVENT{'S' if self.crash_count != 1 else ''})",
            bg=t["card"], fg=t["accent_red"], font=f_title
        )
        title_lbl.pack(side=tk.LEFT)

        btn_x = tk.Label(header, text="✕", bg=t["card"], fg=t["text_dim"], font=f_bold, cursor="hand2")
        btn_x.pack(side=tk.RIGHT)
        btn_x.bind("<Button-1>", lambda e: self.win.destroy())

        # Body Frame
        body = tk.Frame(self.win, bg=t["bg"], padx=14, pady=12)
        body.pack(fill=tk.BOTH, expand=True)

        status_box = tk.Frame(body, bg=t["card"], padx=10, pady=8, highlightbackground=t["accent_red"], highlightthickness=1)
        status_box.pack(fill=tk.X, pady=(0, 10))

        status_lbl = tk.Label(
            status_box,
            text="Away Mode Suspended: The AI will not run unattended batch jobs until crash events are acknowledged.",
            bg=t["card"], fg=t["accent_amber"], font=f_bold, wraplength=410, justify=tk.LEFT
        )
        status_lbl.pack(anchor="w")

        # Events List
        events = parse_unacknowledged_crashes()
        events_frame = tk.Frame(body, bg=t["bg"])
        events_frame.pack(fill=tk.BOTH, expand=True)

        if events:
            tk.Label(events_frame, text="Recent Unacknowledged Events:", bg=t["bg"], fg=t["text_main"], font=f_bold).pack(anchor="w", pady=(0, 4))
            for ev in events[-4:]:  # Show up to 4 most recent
                row = tk.Frame(events_frame, bg=t["card"], padx=8, pady=5)
                row.pack(fill=tk.X, pady=2)
                t_lbl = tk.Label(row, text=ev["time"], bg=t["card"], fg=t["text_dim"], font=f_mono)
                t_lbl.pack(side=tk.LEFT, padx=(0, 8))
                e_lbl = tk.Label(row, text=ev["event"], bg=t["card"], fg=t["accent_primary"], font=f_bold)
                e_lbl.pack(side=tk.LEFT)
                if ev.get("detail"):
                    d_lbl = tk.Label(row, text=f"({ev['detail']})", bg=t["card"], fg=t["text_muted"], font=f_sub)
                    d_lbl.pack(side=tk.LEFT, padx=(6, 0))
        else:
            fallback_text = f"{self.crash_count} unexpected reboot or GPU reset event detected in Windows Event Log."
            tk.Label(events_frame, text=fallback_text, bg=t["bg"], fg=t["text_main"], font=f_sub).pack(anchor="w")

        # Context note
        note_lbl = tk.Label(
            body,
            text="Known issue: AMD 26.8.1 driver can hang and BSOD (0x116 amdkmdag) after display wake.",
            bg=t["bg"], fg=t["text_dim"], font=f_sub, justify=tk.LEFT, wraplength=410
        )
        note_lbl.pack(anchor="w", pady=(8, 12))

        # Action Buttons Frame
        btn_frame = tk.Frame(body, bg=t["bg"])
        btn_frame.pack(fill=tk.X)

        btn_ack = tk.Button(
            btn_frame,
            text="✓ Acknowledge & Unblock Away",
            bg=t["accent_green"],
            fg="#000000",
            font=f_bold,
            relief=tk.FLAT,
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._on_ack_clicked,
        )
        btn_ack.pack(side=tk.LEFT, padx=(0, 8))

        btn_report = tk.Button(
            btn_frame,
            text="📄 View Report",
            bg=t["card"],
            fg=t["text_main"],
            font=f_sub,
            relief=tk.FLAT,
            padx=10,
            pady=6,
            cursor="hand2",
            command=self._open_report,
        )
        btn_report.pack(side=tk.LEFT)

        btn_close = tk.Button(
            btn_frame,
            text="Close",
            bg=t["card"],
            fg=t["text_dim"],
            font=f_sub,
            relief=tk.FLAT,
            padx=10,
            pady=6,
            cursor="hand2",
            command=self.win.destroy,
        )
        btn_close.pack(side=tk.RIGHT)

    def _position_window(self) -> None:
        self.win.update_idletasks()
        w = 460
        h = self.win.winfo_reqheight()
        # Position centered near parent
        px = self.parent.winfo_rootx()
        py = self.parent.winfo_rooty()
        x = max(20, px)
        y = max(40, py - h - 10)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _on_ack_clicked(self) -> None:
        try:
            from .ticker import write_crash_ack
            write_crash_ack(ACK_FILE)
        except Exception:
            pass

        if self.on_cleared:
            try:
                self.on_cleared()
            except Exception:
                pass

        self.win.destroy()

    def _open_report(self) -> None:
        if CRASH_WATCH_PATH.exists():
            try:
                os.startfile(str(CRASH_WATCH_PATH))
            except Exception:
                webbrowser.open(str(CRASH_WATCH_PATH))
