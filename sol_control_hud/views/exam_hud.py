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
        on_pick_snip: Callable[[], None] | None = None,
    ):
        self.parent = parent
        self.on_retry_gemini = on_retry_gemini
        self.on_add_snip = on_add_snip
        self.on_pick_snip = on_pick_snip
        self.win: tk.Toplevel | None = None
        self.title_lbl: tk.Label | None = None
        self.badge_lbl: tk.Label | None = None
        self.topic_lbl: tk.Label | None = None
        self.ans_lbl: tk.Label | None = None
        self.bad_btn: tk.Button | None = None
        self.snip_btn: tk.Button | None = None
        self.copy_btn: tk.Button | None = None
        self.txt: tk.Text | None = None
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
        self.ans_frame.bind(
            "<Configure>",
            lambda e: self.ans_lbl.config(wraplength=max(200, e.width - 20)) if self.ans_lbl else None,
        )

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
        self.txt.tag_configure("question", font=("Segoe UI", 10, "bold"), foreground="#f8fafc")
        self.txt.tag_configure("bold", font=("Segoe UI", 10, "bold"), foreground="#e2e8f0")
        self.txt.tag_configure("normal", foreground="#e2e8f0")
        self.txt.tag_configure("muted", foreground="#64748b")
        self.txt.tag_configure("cyan", foreground="#38bdf8")
        self.txt.tag_configure("green", foreground="#4ade80")
        self.txt.tag_configure("selected", font=("Segoe UI", 10, "bold"), foreground="#4ade80")
        self.txt.tag_configure("selected_gemini", font=("Segoe UI", 10, "bold"), foreground="#38bdf8")
        self.txt.tag_configure("code", font=("Consolas", 10), foreground="#38bdf8")

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
            text="📜 Snip Scroll",
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
        self.snip_btn.bind("<Button-3>", lambda e: self._on_right_click_snip())

        self.copy_btn = tk.Button(
            btn_frame,
            text="📋 Copy Command",
            command=self._on_click_copy,
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            activeforeground="#ffffff",
            bd=0,
            padx=8,
            pady=4,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )

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
        import re
        self._last_item = item
        w = self._ensure_window()

        q_type = item.get("question_type", "multiple_choice")
        topic = item.get("topic") or "Exam Question"
        is_gemini = bool(item.get("gemini_retried") or item.get("model") == "gemini-3.8-flash")

        if q_type == "matching":
            pairs = item.get("matching_pairs") or []
            ans_display = f"{len(pairs)} Pairs Matched" if pairs else (item.get("answer") or "–")
            type_label = "Matching"
        elif q_type == "fill_in_the_blank":
            blanks = item.get("blank_answers") or []
            ans_display = blanks[0] if blanks else (item.get("answer") or "–")
            type_label = "Fill-in-Blank"
        elif q_type == "ordering":
            steps = item.get("ordered_sequence") or []
            ans_display = f"{len(steps)} Steps Sequenced" if steps else (item.get("answer") or "–")
            type_label = "Ordering"
        else:
            ans_display = item.get("answer") or item.get("correct") or "–"
            type_label = "Multiple Choice"

        self.topic_lbl.config(text=f"Topic: {topic} · {type_label}")
        self.ans_lbl.config(
            text=f"Ans: {ans_display}",
            fg="#38bdf8" if is_gemini else "#4ade80",
        )

        if is_gemini:
            self.badge_lbl.config(text=f"✨ Gemini 3.8 Flash ({type_label})", fg="#38bdf8")
            self.bad_btn.config(state=tk.DISABLED, text="✓ Gemini Checked")
        else:
            self.badge_lbl.config(text=f"⚡ sol-vision ({type_label})", fg="#94a3b8")
            self.bad_btn.config(state=tk.NORMAL, text="🔍 Verify / Retry (Gemini)")

        if self.snip_btn:
            self.snip_btn.config(text="📜 Snip Scroll")

        # Show Copy Command button for fill_in_the_blank, otherwise hide it
        if self.copy_btn:
            if q_type == "fill_in_the_blank":
                if self.snip_btn:
                    self.copy_btn.pack(side=tk.LEFT, padx=8, before=self.snip_btn)
                else:
                    self.copy_btn.pack(side=tk.LEFT, padx=8)
            else:
                self.copy_btn.pack_forget()

        self.txt.config(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)

        q = item.get("question") or ""
        if q:
            self.txt.insert(tk.END, f"{q}\n\n", "question")

        exhibit = item.get("exhibit_text") or ""
        if exhibit:
            self.txt.insert(tk.END, "--- EXHIBIT / DIAGRAM ---\n", "cyan")
            self.txt.insert(tk.END, f"{exhibit}\n")
            self.txt.insert(tk.END, "-------------------------\n\n", "cyan")

        if q_type == "matching":
            pairs = item.get("matching_pairs") or []
            if pairs:
                self.txt.insert(tk.END, "MATCHING PAIRS:\n", "bold")
                for p in pairs:
                    src = p.get("source", "")
                    tgt = p.get("target", "")
                    self.txt.insert(tk.END, f"  • {src} ")
                    self.txt.insert(tk.END, "➔", "cyan")
                    self.txt.insert(tk.END, f" {tgt}\n", "green")
                self.txt.insert(tk.END, "\n")
        elif q_type == "fill_in_the_blank":
            blanks = item.get("blank_answers") or []
            if blanks:
                self.txt.insert(tk.END, "COMMAND / INPUT:\n", "bold")
                for b in blanks:
                    self.txt.insert(tk.END, "  >>> ", "cyan")
                    self.txt.insert(tk.END, f"{b}\n", "code")
                self.txt.insert(tk.END, "\n")
        elif q_type == "ordering":
            steps = item.get("ordered_sequence") or []
            if steps:
                self.txt.insert(tk.END, "ORDERED SEQUENCE:\n", "bold")
                for idx, s in enumerate(steps, 1):
                    line = s if re.match(r"^\d+\.", s) else f"{idx}. {s}"
                    self.txt.insert(tk.END, f"  {line}\n")
                self.txt.insert(tk.END, "\n")
        else:
            choices = item.get("choices") or []
            sel_labels = set(item.get("answer_labels") or item.get("correct_labels") or [])
            if not sel_labels and item.get("answer"):
                sel_labels = set(re.findall(r"\b([A-Z])\b", str(item.get("answer"))))
            for c in choices:
                lb, txt = c.get("label", "").strip(), c.get("text", "")
                is_sel = lb in sel_labels
                box_icon = "☑ " if is_sel else "☐ "
                tag_icon = "selected_gemini" if (is_sel and is_gemini) else ("selected" if is_sel else "muted")
                self.txt.insert(tk.END, f"  {box_icon}{lb}. ", tag_icon)
                self.txt.insert(tk.END, f"{txt}\n", "bold" if is_sel else "normal")
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

    def _on_click_copy(self) -> None:
        if not self._last_item or not self.win:
            return
        blanks = self._last_item.get("blank_answers") or []
        cmd = blanks[0] if blanks else (self._last_item.get("answer") or "")
        if cmd:
            try:
                self.win.clipboard_clear()
                self.win.clipboard_append(cmd)
                if self.copy_btn:
                    self.copy_btn.config(text="✓ Copied!")
                    self.win.after(1500, lambda: self.copy_btn.config(text="📋 Copy Command") if self.copy_btn else None)
            except Exception:
                pass

    def _on_click_retry(self) -> None:
        if self.on_retry_gemini:
            self.bad_btn.config(state=tk.DISABLED, text="Consulting Gemini…")
            self.on_retry_gemini()

    def _on_click_snip(self) -> None:
        if self.on_add_snip:
            self.on_add_snip()

    def _on_right_click_snip(self) -> None:
        if self.on_pick_snip:
            self.on_pick_snip()
        elif self.on_add_snip:
            self.on_add_snip()
