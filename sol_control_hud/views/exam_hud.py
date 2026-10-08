"""Floating topmost Exam HUD window for instant answer display during tests.

Keeps answers visible on top of full-screen test browsers and exam software.
Features:
- Always on top (-topmost True).
- Direct "Mark Bad -> Gemini" and "Add Scroll Snip" buttons.
- Styled with SOL HUD dark theme.
- Auto-dismissible with Escape key or close button.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable


class ExamHudWindow:
    """Floating topmost HUD window displaying the live exam solution in the screen corner."""

    def __init__(
        self,
        parent: tk.Misc,
        on_retry_gemini: Callable[[], None] | None = None,
        on_add_snip: Callable[[], None] | None = None,
    ):
        self.parent = parent
        self.on_retry_gemini = on_retry_gemini
        self.on_add_snip = on_add_snip
        self.win: tk.Toplevel | None = None
        self._visible = False
        self._last_item: dict | None = None

    def _ensure_window(self) -> tk.Toplevel:
        if self.win and self.win.winfo_exists():
            return self.win

        self.win = tk.Toplevel(self.parent)
        self.win.title("SOL Exam Assist")
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#0f172a")

        # Position in top-right of main screen
        sw = self.parent.winfo_screenwidth()
        w, h = 480, 340
        x = max(10, sw - w - 40)
        y = 60
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.minsize(360, 240)

        # Top bar: Title & Badge
        top_frame = tk.Frame(self.win, bg="#1e293b", padx=10, pady=6)
        top_frame.pack(fill=tk.X, side=tk.TOP)

        self.title_lbl = tk.Label(
            top_frame,
            text="⚡ SOL Exam Assist",
            fg="#38bdf8",
            bg="#1e293b",
            font=("Segoe UI", 11, "bold"),
        )
        self.title_lbl.pack(side=tk.LEFT)

        self.badge_lbl = tk.Label(
            top_frame,
            text="local sol-vision",
            fg="#94a3b8",
            bg="#1e293b",
            font=("Segoe UI", 9),
        )
        self.badge_lbl.pack(side=tk.LEFT, padx=10)

        close_btn = tk.Button(
            top_frame,
            text="✕",
            command=self.hide,
            fg="#94a3b8",
            bg="#1e293b",
            bd=0,
            activeforeground="#f87171",
            activebackground="#1e293b",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        close_btn.pack(side=tk.RIGHT)

        # Answer Header
        self.ans_frame = tk.Frame(self.win, bg="#0f172a", padx=12, pady=8)
        self.ans_frame.pack(fill=tk.X, side=tk.TOP)

        self.topic_lbl = tk.Label(
            self.ans_frame,
            text="Topic: waiting for question…",
            fg="#94a3b8",
            bg="#0f172a",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.topic_lbl.pack(fill=tk.X)

        self.ans_lbl = tk.Label(
            self.ans_frame,
            text="Answer: –",
            fg="#4ade80",
            bg="#0f172a",
            font=("Segoe UI", 13, "bold"),
            anchor="w",
            wraplength=450,
            justify=tk.LEFT,
        )
        self.ans_lbl.pack(fill=tk.X, pady=(4, 0))

        # Explanation Text Area
        txt_frame = tk.Frame(self.win, bg="#0f172a", padx=12, pady=4)
        txt_frame.pack(fill=tk.BOTH, expand=True)

        self.txt = tk.Text(
            txt_frame,
            wrap="word",
            bg="#1e293b",
            fg="#e2e8f0",
            insertbackground="#ffffff",
            bd=0,
            padx=8,
            pady=8,
            font=("Segoe UI", 10),
        )
        self.txt.pack(fill=tk.BOTH, expand=True)

        # Bottom Action Bar
        btn_frame = tk.Frame(self.win, bg="#0f172a", padx=12, pady=8)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.bad_btn = tk.Button(
            btn_frame,
            text="🔍 Verify / Retry (Gemini)",
            command=self._on_click_retry,
            bg="#ef4444",
            fg="#ffffff",
            activebackground="#dc2626",
            activeforeground="#ffffff",
            bd=0,
            padx=10,
            pady=4,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.bad_btn.pack(side=tk.LEFT)

        self.snip_btn = tk.Button(
            btn_frame,
            text="➕ Add Snip (Scroll)",
            command=self._on_click_snip,
            bg="#334155",
            fg="#cbd5e1",
            activebackground="#475569",
            activeforeground="#ffffff",
            bd=0,
            padx=8,
            pady=4,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        self.snip_btn.pack(side=tk.LEFT, padx=8)

        hint_lbl = tk.Label(
            btn_frame,
            text="Esc: hide",
            fg="#64748b",
            bg="#0f172a",
            font=("Segoe UI", 8),
        )
        hint_lbl.pack(side=tk.RIGHT)

        # Bindings
        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<Control-r>", lambda e: self._on_click_retry())
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        return self.win

    def show(self) -> None:
        w = self._ensure_window()
        w.deiconify()
        w.lift()
        w.attributes("-topmost", True)
        self._visible = True

    def hide(self) -> None:
        if self.win and self.win.winfo_exists():
            self.win.withdraw()
        self._visible = False

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def update_item(self, item: dict) -> None:
        self._last_item = item
        w = self._ensure_window()

        topic = item.get("topic") or "Exam Question"
        ans = item.get("answer") or item.get("correct") or "–"
        is_gemini = bool(item.get("gemini_retried") or item.get("model") == "gemini-3.8-flash")

        self.topic_lbl.config(text=f"Topic: {topic}")
        self.ans_lbl.config(
            text=f"Ans: {ans}",
            fg="#38bdf8" if is_gemini else "#4ade80",
        )

        if is_gemini:
            self.badge_lbl.config(text="✨ Gemini 3.8 Flash", fg="#38bdf8")
            self.bad_btn.config(state=tk.DISABLED, text="✓ Gemini Checked")
        else:
            self.badge_lbl.config(text="⚡ sol-vision (local)", fg="#94a3b8")
            self.bad_btn.config(state=tk.NORMAL, text="🔍 Verify / Retry (Gemini)")

        self.txt.config(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)

        q = item.get("question") or ""
        if q:
            self.txt.insert(tk.END, f"{q}\n\n", "question")

        choices = item.get("choices") or []
        for c in choices:
            lb, txt = c.get("label", ""), c.get("text", "")
            self.txt.insert(tk.END, f"  {lb}. {txt}\n")
        if choices:
            self.txt.insert(tk.END, "\n")

        exp = item.get("explanation") or ""
        if exp:
            self.txt.insert(tk.END, f"Explanation: {exp}\n\n")

        why_wrong = item.get("why_previous_wrong") or ""
        if why_wrong:
            self.txt.insert(tk.END, f"Why previous answer was wrong:\n{why_wrong}\n\n")

        self.txt.config(state=tk.DISABLED)

        # Show if not already visible
        self.show()

    def _on_click_retry(self) -> None:
        if self.on_retry_gemini:
            self.bad_btn.config(state=tk.DISABLED, text="Consulting Gemini…")
            self.on_retry_gemini()

    def _on_click_snip(self) -> None:
        if self.on_add_snip:
            self.on_add_snip()
