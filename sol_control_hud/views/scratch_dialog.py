"""Quick Scratch / Note Capture Dialog for SOL Control HUD.
Allows capturing quick thoughts, ideas, or links directly into Polymatica Vault
(today's daily note) or OBVLT 1Notebook (private scratch), without opening Obsidian.
"""
from __future__ import annotations

import os
from pathlib import Path
import time
import tkinter as tk
from tkinter import font as tkfont
from ..swallow import note as _swallowed

POLYMATICA_VAULT = Path(r"D:\Polymatica Vault")
OBVLT_VAULT = Path(r"D:\OBVLT")


def append_scratch_note(
    text: str,
    target: str = "daily",
    include_timestamp: bool = True,
    vault_dir: Path | None = None,
    obvlt_dir: Path | None = None,
    timestamp_override: str | None = None,
) -> tuple[bool, Path, str]:
    """Appends a quick scratch note to today's daily note or private notebook.

    Returns (success, path, error_or_summary).
    """
    clean_text = text.strip()
    if not clean_text:
        return False, Path(), "Empty note text"

    now = time.localtime()
    year_str = time.strftime("%Y", now)
    date_str = time.strftime("%Y-%m-%d", now)
    time_str = timestamp_override or time.strftime("%H:%M", now)

    v_dir = vault_dir or POLYMATICA_VAULT
    o_dir = obvlt_dir or OBVLT_VAULT

    try:
        if target == "private":
            target_path = o_dir / "1Notebook" / "Scratch.md"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            header = ""
            if not target_path.exists() or target_path.stat().st_size == 0:
                header = "# 📓 Private Quick Scratch\n\n"
            
            ts_prefix = f"- {date_str} {time_str}: " if include_timestamp else "- "
            entry = f"{ts_prefix}{clean_text}\n"

            content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            if content and not content.endswith("\n"):
                content += "\n"
            if header and not content:
                content = header
            content += entry
            target_path.write_text(content, encoding="utf-8")
            return True, target_path, "Appended to 1Notebook/Scratch.md"

        else:
            # Default: Polymatica Daily Note (D:\Polymatica Vault\YYYY\YYYY-MM-DD.md)
            target_path = v_dir / year_str / f"{date_str}.md"
            target_path.parent.mkdir(parents=True, exist_ok=True)

            ts_prefix = f"- {time_str} " if include_timestamp else ""
            entry = f"{ts_prefix}{clean_text}\n"

            content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            if content:
                if not content.endswith("\n\n"):
                    content = content.rstrip("\r\n") + "\n\n"
            content += entry
            target_path.write_text(content, encoding="utf-8")
            return True, target_path, f"Appended to {date_str}.md"

    except Exception as exc:
        return False, Path(), str(exc)


class QuickScratchDialog:
    """Dark-mode quick capture popup for thoughts, ideas, and notes."""

    def __init__(
        self,
        parent: tk.Tk | tk.Toplevel,
        theme: dict[str, str],
        on_saved: callable | None = None,
        vault_dir: Path | None = None,
        obvlt_dir: Path | None = None,
    ):
        self.parent = parent
        self.theme = theme
        self.on_saved = on_saved
        self.vault_dir = vault_dir
        self.obvlt_dir = obvlt_dir

        self.win = tk.Toplevel(parent)
        self.win.title("Quick Scratch Capture")
        self.win.configure(bg=theme["bg"])
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.transient(parent)

        self._target_var = tk.StringVar(value="daily")
        self._timestamp_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._position_window()

    def _build_ui(self) -> None:
        t = self.theme
        f_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_sub = tkfont.Font(family="Segoe UI", size=8)
        f_bold = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        f_body = tkfont.Font(family="Segoe UI", size=9)

        # Header Frame
        header = tk.Frame(self.win, bg=t["card"], padx=12, pady=8)
        header.pack(fill=tk.X)

        title_lbl = tk.Label(
            header,
            text="📝 QUICK SCRATCH / NOTE CAPTURE",
            bg=t["card"],
            fg=t["accent_primary"],
            font=f_title,
        )
        title_lbl.pack(side=tk.LEFT)

        btn_x = tk.Label(header, text="✕", bg=t["card"], fg=t["text_dim"], font=f_bold, cursor="hand2")
        btn_x.pack(side=tk.RIGHT)
        btn_x.bind("<Button-1>", lambda e: self.win.destroy())

        # Main Body
        body = tk.Frame(self.win, bg=t["bg"], padx=14, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        # Target Selector Frame
        target_frame = tk.Frame(body, bg=t["bg"])
        target_frame.pack(fill=tk.X, pady=(0, 8))

        tk.Label(target_frame, text="Destination:", bg=t["bg"], fg=t["text_dim"], font=f_sub).pack(side=tk.LEFT, padx=(0, 8))

        rb_daily = tk.Radiobutton(
            target_frame,
            text="Daily Note (Polymatica)",
            variable=self._target_var,
            value="daily",
            bg=t["bg"],
            fg=t["text_main"],
            activebackground=t["bg"],
            activeforeground=t["text_main"],
            selectcolor=t["card"],
            font=f_sub,
        )
        rb_daily.pack(side=tk.LEFT, padx=(0, 8))

        rb_priv = tk.Radiobutton(
            target_frame,
            text="Private Scratch (OBVLT)",
            variable=self._target_var,
            value="private",
            bg=t["bg"],
            fg=t["text_main"],
            activebackground=t["bg"],
            activeforeground=t["text_main"],
            selectcolor=t["card"],
            font=f_sub,
        )
        rb_priv.pack(side=tk.LEFT)

        chk_ts = tk.Checkbutton(
            target_frame,
            text="Timestamp",
            variable=self._timestamp_var,
            bg=t["bg"],
            fg=t["text_dim"],
            activebackground=t["bg"],
            activeforeground=t["text_dim"],
            selectcolor=t["card"],
            font=f_sub,
        )
        chk_ts.pack(side=tk.RIGHT)

        # Text Input Box
        self.text_input = tk.Text(
            body,
            height=4,
            width=48,
            bg=t["card"],
            fg=t["text_main"],
            insertbackground=t["accent_primary"],
            relief=tk.FLAT,
            padx=8,
            pady=8,
            font=f_body,
            wrap=tk.WORD,
            highlightbackground=t["border"],
            highlightthickness=1,
        )
        self.text_input.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self.text_input.focus_set()

        # Keyboard bindings
        self.text_input.bind("<Control-Return>", lambda e: self._on_save())
        self.win.bind("<Escape>", lambda e: self.win.destroy())

        # Status / Feedback label
        self.status_lbl = tk.Label(body, text="Tip: Press Ctrl+Enter to save, Esc to cancel", bg=t["bg"], fg=t["text_dim"], font=f_sub)
        self.status_lbl.pack(side=tk.LEFT)

        # Buttons Frame
        btn_frame = tk.Frame(body, bg=t["bg"])
        btn_frame.pack(side=tk.RIGHT)

        btn_save = tk.Button(
            btn_frame,
            text="✓ Save Note",
            bg=t["accent_green"],
            fg="#000000",
            font=f_bold,
            relief=tk.FLAT,
            padx=12,
            pady=4,
            cursor="hand2",
            command=self._on_save,
        )
        btn_save.pack(side=tk.LEFT, padx=(0, 6))

        btn_cancel = tk.Button(
            btn_frame,
            text="Cancel",
            bg=t["card"],
            fg=t["text_dim"],
            font=f_sub,
            relief=tk.FLAT,
            padx=10,
            pady=4,
            cursor="hand2",
            command=self.win.destroy,
        )
        btn_cancel.pack(side=tk.LEFT)

    def _position_window(self) -> None:
        self.win.update_idletasks()
        w = 460
        h = self.win.winfo_reqheight()
        px = self.parent.winfo_rootx()
        py = self.parent.winfo_rooty()
        x = max(20, px)
        y = max(40, py - h - 10)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _on_save(self) -> None:
        content = self.text_input.get("1.0", tk.END).strip()
        if not content:
            self.status_lbl.configure(text="Please enter some text first", fg=self.theme["accent_amber"])
            return

        ok, target_path, msg = append_scratch_note(
            text=content,
            target=self._target_var.get(),
            include_timestamp=self._timestamp_var.get(),
            vault_dir=self.vault_dir,
            obvlt_dir=self.obvlt_dir,
        )

        if ok:
            if self.on_saved:
                try:
                    self.on_saved(target_path, content)
                except Exception:
                    _swallowed("scratch_dialog.QuickScratchDialog._on_save")
            self.win.destroy()
        else:
            self.status_lbl.configure(text=f"Failed to save: {msg}", fg=self.theme["accent_red"])
