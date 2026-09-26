"""Away screen: both monitors black while Away mode works; the main monitor shows what the AI is doing, a progress bar
and when it'll be done. Coming back takes a deliberate key: Enter, or Space tapped 5 times within 3 s.
OBVLT plans/AWAY_SCREEN_PLAN.md. Started by D:\\OBVLT\\tools\\sol-llm.ps1 when Away starts:

    pythonw -m sol_control_hud.away.screen

Talks to the watcher (tools\\sol-llm-watch.ps1) through files in D:\\AI\\Cache\\llm:
- away-screen.json  heartbeat every 2 s ({pid, updated, phase, guarding}). While it's fresh and `guarding`, the watcher
                    ignores ordinary input; otherwise it falls back to "any input = you're back".
- away-back.flag    written when you press Enter / Space x5 during Away. Since 2026-09-26 (your choice) that only means
                    "I'm at the PC": the AI keeps working and the screen shrinks to a small panel in the corner
                    (--panel) with Stop AI work / Black screen buttons. During the sleep countdown it cancels the sleep.
- away-stop.flag    written by the panel's Stop AI work button: the watcher ends Away (job back in the queue).
- sleep-countdown.json  written by the watcher before an away-sleep: the screen shows "sleeping in N s".
Reads: state.json (mode), progress.json (queue job), chains.json (running chain), away.jsonl (what finished).
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LLM_DIR = Path(os.environ.get("SOL_LLM_DIR", r"D:\AI\Cache\llm"))
STATE_F = LLM_DIR / "state.json"
PROGRESS_F = LLM_DIR / "progress.json"
CHAINS_F = LLM_DIR / "chains.json"
AWAYLOG_F = LLM_DIR / "away.jsonl"
HEARTBEAT_F = LLM_DIR / "away-screen.json"
BACK_FLAG = LLM_DIR / "away-back.flag"
STOP_FLAG = LLM_DIR / "away-stop.flag"
COUNTDOWN_F = LLM_DIR / "sleep-countdown.json"
LOCK_F = LLM_DIR / "away-screen.lock"
QUEUE_DIR = Path(os.environ.get("SOL_QUEUE_DIR", r"D:\AI\Queue"))

SPACE_TAPS, SPACE_WINDOW = 5, 3.0          # your choice 2026-09-25
ETA_AFTER = 120.0                           # no time estimate before 2 min of data (it'd be a wild guess)
KEEP_SCREENS_ON_AFTER_DONE = 30 * 60        # then Windows may turn the monitors off as usual
LOST_FOCUS_GIVE_UP = 5.0                    # someone is using another window: stop guarding (watcher: old rule)

GREY, DIM, GREEN, AMBER, RED, BAR_BG = "#9aa4b2", "#4b5563", "#4ade80", "#fbbf24", "#f87171", "#1f2937"


# ---------------------------------------------------------------- pure logic (tested)
class ExitKeys:
    """Enter comes back at once; Space must be tapped SPACE_TAPS times within SPACE_WINDOW seconds."""

    def __init__(self, taps: int = SPACE_TAPS, window: float = SPACE_WINDOW):
        self.taps, self.window, self.spaces = taps, window, []

    def press(self, key: str, t: float) -> bool:
        if key in ("Return", "KP_Enter"):
            return True
        if key == "space":
            self.spaces = [s for s in self.spaces if t - s <= self.window] + [t]
            return len(self.spaces) >= self.taps
        return False


class EtaTracker:
    """Time left from the rate the screen itself has seen for one task; restarts when the task changes."""

    def __init__(self):
        self.key, self.t0, self.done0 = None, 0.0, 0

    def update(self, key, done: int | None, total: int | None, now: float) -> float | None:
        if done is None or not total:
            self.key = None
            return None
        if key != self.key or done < self.done0:
            self.key, self.t0, self.done0 = key, now, done
            return None
        elapsed, gained = now - self.t0, done - self.done0
        if elapsed < ETA_AFTER or gained <= 0:
            return None
        return (total - done) * elapsed / gained


def minutes_text(seconds: float | None, now: datetime) -> str:
    if seconds is None:
        return ""
    m = max(1, round(seconds / 60))
    left = f"about {m} min left" if m < 90 else f"about {m / 60:.1f} h left"
    end = datetime.fromtimestamp(now.timestamp() + seconds)
    return f"{left} · done around {end:%H:%M}"


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def session_events(since: float, path: Path = AWAYLOG_F) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if datetime.fromisoformat(e["time"]).timestamp() >= since - 5:
                    out.append(e)
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    return out


STOP_REASONS = {"RAM guard": "Stopped early: the PC ran low on memory (RAM guard).",
                "away max time": "Stopped: Away reached its time limit.",
                "game": "Stopped: a game started, so the AI stepped aside.",
                "stopped by you": "Stopped. The unfinished work stays queued for next time.",
                "you came back": "Stopped. The unfinished work stays queued for next time."}


@dataclass
class View:
    phase: str                       # working | done | stopped | countdown | returning | idle
    title: str = "SOL · AWAY MODE"
    line: str = ""                   # what it's doing
    done: int | None = None
    total: int | None = None
    eta: str = ""
    jobs: str = ""
    footer: str = "Press Enter (or tap Space 5×) to come back"
    color: str = GREY
    summary: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float | None:
        return (min(max(self.done / self.total, 0.0), 1.0)) if self.done is not None and self.total else None


def build_view(state: dict | None, progress: dict | None, chains: dict | None, events: list[dict],
               countdown: dict | None, returning: bool, eta: EtaTracker, now: float) -> View:
    state = state or {}
    mode = state.get("mode")
    done_jobs = [e for e in events if e.get("event") == "job-done"]
    failed_jobs = [e for e in events if e.get("event") == "job-failed"]
    summary = [f"✓ {e.get('job')} ({e.get('minutes')} min)" for e in done_jobs] + \
              [f"✗ {e.get('job')}: {e.get('why', '')[:80]}" for e in failed_jobs]
    if returning:
        return View("returning", line="Coming back… stopping the AI safely (the unfinished work stays queued).",
                    footer="", color=GREY)
    if countdown and countdown.get("at"):
        left = max(0, int(datetime.fromisoformat(countdown["at"]).timestamp() - now))
        return View("countdown", line="All done.", done=1, total=1, color=GREEN, summary=summary,
                    footer=f"Sleeping in {left} s — press Enter to stay awake")
    if mode == "away":
        waiting = len(list(QUEUE_DIR.glob("*.json"))) if QUEUE_DIR.is_dir() else 0
        running_chain = (chains or {}).get("running") if isinstance(chains, dict) else None
        p = progress or {}
        fresh = p.get("state") == "running" and p.get("updated") and \
            now - datetime.fromisoformat(p["updated"]).timestamp() < 90
        total_jobs = len(done_jobs) + len(failed_jobs) + (1 if fresh else 0) + waiting
        jobs = f"job {len(done_jobs) + len(failed_jobs) + 1} of {total_jobs}" if fresh and total_jobs > 1 else ""
        if done_jobs:
            jobs += ("  ·  " if jobs else "") + "  ".join(f"{e.get('job')} ✓" for e in done_jobs[-3:])
        if fresh:
            line = f"{p.get('job')}" + (f" — {p['detail']}" if p.get("detail") else "")
            if p.get("done") is None:
                started = p.get("started")
                mins = int((now - datetime.fromisoformat(started).timestamp()) / 60) if started else 0
                line += f" (running {mins} min)"
            secs = eta.update(("job", p.get("job")), p.get("done"), p.get("total"), now)
            return View("working", line=line, done=p.get("done"), total=p.get("total"),
                        eta=minutes_text(secs, datetime.fromtimestamp(now)), jobs=jobs)
        if isinstance(running_chain, dict):
            c = running_chain
            line = f"Chain “{c.get('chain')}”"
            if c.get("step_name"):
                line += f" — step {c.get('step')} of {c.get('steps')}: {c['step_name']}"
            if c.get("items"):
                line += f", item {c.get('item')} of {c['items']}"
            secs = eta.update(("chain", c.get("run"), c.get("step")), c.get("done"), c.get("total"), now)
            return View("working", line=line, done=c.get("done"), total=c.get("total"),
                        eta=minutes_text(secs, datetime.fromtimestamp(now)), jobs=jobs)
        eta.update(None, None, None, now)
        return View("working", line="Getting ready…" if not events else "Finishing up…", jobs=jobs)
    if mode in ("desk", "off"):
        reason = str(state.get("reason") or "")
        if reason == "queue done" or (not reason and done_jobs):
            return View("done", line="All done — you can come back.", done=1, total=1, color=GREEN,
                        summary=summary, footer="Press Enter to go back to your desktop")
        key = "game" if reason.startswith("game") else reason
        text = STOP_REASONS.get(key, f"Away ended ({reason or 'unknown reason'}).")
        return View("stopped", line=text, color=AMBER if key != "RAM guard" else RED, summary=summary,
                    footer="Press Enter to go back to your desktop")
    return View("idle", line="Waiting for Away mode…")


def panel_text(v: View) -> tuple[str, str]:
    """The corner panel's two lines: what's happening, and progress / time left."""
    if v.phase == "working":
        pct = f"{int(v.fraction * 100)} %" if v.fraction is not None and v.total else ""
        return f"AI still working — {v.line}", "  ·  ".join(x for x in (pct, v.eta, v.jobs) if x)
    if v.phase == "done":
        return "AI work all done ✓", "  ·  ".join(v.summary[-3:])
    if v.phase == "stopped":
        return v.line, ""
    return v.line or "Waiting for Away mode…", ""


def closes_by_itself(state: dict | None, returning: bool) -> bool:
    """The game guard turned the AI off: you're at the PC playing, so the screen goes away at once."""
    s = state or {}
    return not returning and s.get("mode") == "off" and str(s.get("reason") or "").startswith("game")


# ---------------------------------------------------------------- Windows helpers
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def monitors() -> list[tuple[int, int, int, int, bool]]:
    """(left, top, width, height, primary) for every monitor, in physical pixels (call after DPI awareness)."""
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, hdc, rect, data):
        info = _MONITORINFO(); info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        m = info.rcMonitor
        found.append((m.left, m.top, m.right - m.left, m.bottom - m.top, bool(info.dwFlags & 1)))
        return 1
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return found or [(0, 0, 1920, 1080, True)]


def take_focus(hwnd: int) -> None:
    """Bring our window to the foreground. Windows refuses SetForegroundWindow to a background program, so attach to the
    current foreground window's input queue for the call (a standard technique; it doesn't fake any key presses)."""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    fg = u32.GetForegroundWindow()
    if not hwnd or fg == hwnd:
        return
    fg_tid, me = u32.GetWindowThreadProcessId(fg, None), k32.GetCurrentThreadId()
    attached = bool(fg_tid and fg_tid != me and u32.AttachThreadInput(me, fg_tid, True))
    try:
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.SetActiveWindow(hwnd)
        u32.SetFocus(hwnd)
    finally:
        if attached:
            u32.AttachThreadInput(me, fg_tid, False)


def keep_screens_on(on: bool) -> None:
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except (AttributeError, OSError):
        pass


def single_instance():
    import msvcrt
    LOCK_F.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_F, "a+b")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        fh.close()
        return None


# ---------------------------------------------------------------- the screen
class AwayScreen:
    def __init__(self, root, panel: bool = False):
        import tkinter as tk
        self.tk, self.root = tk, root
        self.keys, self.eta = ExitKeys(), EtaTracker()
        self.started = time.time()
        self.returning = False
        self.done_since: float | None = None
        self.unfocused_since: float | None = None
        self.guarding = True
        self.panel = False
        self.windows = []
        self.canvas = None
        root.bind_all("<KeyPress>", self.on_key)
        self.view = View("idle")
        if panel:
            self.show_panel()
        else:
            self.show_full()
        self.tick()

    def _clear(self) -> None:
        for win in self.windows[1:]:
            win.destroy()
        for child in self.root.winfo_children():
            child.destroy()
        self.windows, self.canvas = [], None

    def show_full(self) -> None:
        """Both monitors black, the progress in the middle of the main one."""
        tk, root = self.tk, self.root
        self._clear()
        self.panel, self.guarding, self.unfocused_since = False, True, None
        self.keys = ExitKeys()
        mons = sorted(monitors(), key=lambda m: not m[4])      # primary first
        for i, (x, y, w, h, primary) in enumerate(mons):
            win = root if i == 0 else tk.Toplevel(root)
            win.overrideredirect(True)
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.configure(bg="black", cursor="none")
            win.attributes("-topmost", True)
            self.windows.append(win)
        self.w, self.h = mons[0][2], mons[0][3]
        self.canvas = tk.Canvas(root, bg="black", highlightthickness=0, cursor="none")
        self.canvas.pack(fill="both", expand=True)
        keep_screens_on(True)

    def show_panel(self) -> None:
        """You're at the PC and the AI keeps working: a small panel in the corner of the main monitor."""
        tk, root = self.tk, self.root
        self._clear()
        self.panel, self.guarding = True, False
        keep_screens_on(False)
        x, y, w, h, _ = sorted(monitors(), key=lambda m: not m[4])[0]
        scale = max(1.0, h / 1080)
        pw, ph = int(470 * scale), int(118 * scale)
        root.overrideredirect(True)
        root.configure(bg="#0b0f14", cursor="arrow")
        root.geometry(f"{pw}x{ph}+{x + w - pw - int(24 * scale)}+{y + h - ph - int(72 * scale)}")
        root.attributes("-topmost", True)
        self.windows = [root]
        f1, f2 = ("Segoe UI", 10, "bold"), ("Segoe UI", 9)
        self.p_line = tk.Label(root, text="", fg="#e5e7eb", bg="#0b0f14", font=f1, anchor="w", justify="left",
                               wraplength=pw - 20)
        self.p_line.pack(fill="x", padx=10, pady=(8, 0))
        self.p_sub = tk.Label(root, text="", fg=GREY, bg="#0b0f14", font=f2, anchor="w")
        self.p_sub.pack(fill="x", padx=10)
        self.p_bar = tk.Canvas(root, height=6, bg=BAR_BG, highlightthickness=0)
        self.p_bar.pack(fill="x", padx=10, pady=(4, 6))
        row = tk.Frame(root, bg="#0b0f14")
        row.pack(fill="x", padx=10, pady=(0, 8))
        style = dict(font=f2, relief="flat", bd=0, padx=10, pady=3, cursor="hand2")
        self.p_stop = tk.Button(row, text="Stop AI work", command=self.stop_ai, bg="#7f1d1d", fg="white",
                                activebackground="#991b1b", activeforeground="white", **style)
        self.p_stop.pack(side="right")
        self.p_black = tk.Button(row, text="Black screen", command=self.back_to_full, bg="#1f2937", fg="#e5e7eb",
                                 activebackground="#374151", activeforeground="white", **style)
        self.p_black.pack(side="right", padx=(0, 8))
        self.p_close = tk.Button(row, text="Close", command=self.close, bg="#1f2937", fg="#e5e7eb",
                                 activebackground="#374151", activeforeground="white", **style)

    def stop_ai(self) -> None:
        try:
            STOP_FLAG.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        except OSError:
            pass
        self.p_stop.configure(text="Stopping…", state="disabled")

    def back_to_full(self) -> None:
        self.show_full()
        self.render()

    # -- keys
    def on_key(self, event) -> None:
        if self.panel or not self.keys.press(event.keysym, time.time()):
            return
        view = self.view
        if view.phase in ("working", "countdown"):
            try:
                BACK_FLAG.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            except OSError:
                pass
            if view.phase == "working":        # you're here; the AI keeps going (Stop is a button on the panel)
                self.show_panel()
                self.render()
                return
            self.returning = True              # countdown: the flag cancels the sleep
            self.render()
        elif view.phase in ("done", "stopped", "idle"):
            self.close()

    # -- loop
    def tick(self) -> None:
        now = time.time()
        state = read_json(STATE_F)
        self.view = build_view(state, read_json(PROGRESS_F), read_json(CHAINS_F), session_events(self.started),
                               read_json(COUNTDOWN_F), self.returning, self.eta, now)
        if self.returning and (state or {}).get("mode") != "away" and not COUNTDOWN_F.exists():
            self.close(); return
        if self.returning and now - self._returning_since(now) > 60:
            self.close(); return                                           # never hang on "coming back"
        if closes_by_itself(state, self.returning):
            self.close(); return                                           # a game started: you're back, never cover it
        if self.view.phase in ("done", "stopped"):
            self.done_since = self.done_since or now
            if now - self.done_since > KEEP_SCREENS_ON_AFTER_DONE:
                keep_screens_on(False)
            if self.panel and self.view.phase == "stopped" and now - self.done_since > 8:
                self.close(); return                                           # you pressed Stop: it did, bye
        self.keep_on_top(now)
        self.heartbeat(now)
        self.render()
        self.root.after(1000, self.tick)

    def _returning_since(self, now: float) -> float:
        if not hasattr(self, "_ret0"):
            self._ret0 = now
        return self._ret0

    def keep_on_top(self, now: float) -> None:
        for win in self.windows:
            win.lift()
            win.attributes("-topmost", True)
        if self.panel or self.view.phase not in ("working", "countdown", "returning"):
            return                     # Away is over: stay on top until Enter, but don't fight for focus
        take_focus(ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2))   # GA_ROOT
        pid = wintypes.DWORD()   # focused = the foreground window belongs to this process (any of our monitors)
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(pid))
        focused = pid.value == os.getpid()
        if focused:
            self.unfocused_since, self.guarding = None, True
            return
        self.root.focus_force()
        self.unfocused_since = self.unfocused_since or now
        if now - self.unfocused_since > LOST_FOCUS_GIVE_UP:
            self.guarding = False              # someone is using another window: the watcher's old rule takes over

    def heartbeat(self, now: float) -> None:
        data = {"pid": os.getpid(), "updated": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                "phase": self.view.phase, "guarding": self.guarding and not self.panel, "panel": self.panel}
        try:
            tmp = HEARTBEAT_F.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, HEARTBEAT_F)
        except OSError:
            pass

    def render(self) -> None:
        if self.panel:
            return self.render_panel()
        c, v, w, h = self.canvas, self.view, self.w, self.h
        c.delete("all")
        cy = h // 2
        big, mid, small = max(16, h // 40), max(12, h // 60), max(10, h // 80)
        c.create_text(w // 2, cy - 5 * mid, text=v.title, fill=DIM, font=("Segoe UI", small, "bold"))
        c.create_text(w // 2, cy - 2 * mid, text=v.line, fill=v.color if v.phase != "working" else "#cbd5e1",
                      font=("Segoe UI", big), width=int(w * 0.7))
        bar_w, bar_h = int(w * 0.42), max(8, h // 120)
        x0, y0 = (w - bar_w) // 2, cy + mid
        if v.phase in ("working", "done", "countdown", "stopped"):
            c.create_rectangle(x0, y0, x0 + bar_w, y0 + bar_h, fill=BAR_BG, width=0)
            frac = v.fraction
            if frac is not None:
                c.create_rectangle(x0, y0, x0 + int(bar_w * frac), y0 + bar_h,
                                   fill=GREEN if v.phase in ("done", "countdown") else "#38bdf8", width=0)
                if v.total and v.phase == "working":
                    c.create_text(x0 + bar_w + mid, y0 + bar_h // 2, anchor="w", fill=GREY, font=("Segoe UI", small),
                                  text=f"{int(frac * 100)} %")
        line_y = y0 + bar_h + 2 * mid
        for text, color in ((v.eta, GREY), (v.jobs, DIM)):
            if text:
                c.create_text(w // 2, line_y, text=text, fill=color, font=("Segoe UI", small)); line_y += int(1.8 * small)
        for text in v.summary[-6:]:
            c.create_text(w // 2, line_y, text=text, fill=GREY, font=("Segoe UI", small)); line_y += int(1.6 * small)
        if v.footer:
            c.create_text(w // 2, h - 4 * mid, text=v.footer, fill=DIM if v.phase == "working" else GREY,
                          font=("Segoe UI", small))

    def render_panel(self) -> None:
        v = self.view
        line, sub = panel_text(v)
        self.p_line.configure(text=line, fg=v.color if v.phase in ("done", "stopped") else "#e5e7eb")
        self.p_sub.configure(text=sub)
        self.p_bar.delete("all")
        frac = v.fraction
        if frac is not None:
            bw = self.p_bar.winfo_width()
            self.p_bar.create_rectangle(0, 0, int(bw * frac), 6, width=0,
                                        fill=GREEN if v.phase == "done" else "#38bdf8")
        if v.phase in ("done", "stopped") and not self.p_close.winfo_ismapped():
            self.p_stop.pack_forget()
            self.p_black.pack_forget()
            self.p_close.pack(side="right")

    def close(self) -> None:
        keep_screens_on(False)
        for path in (HEARTBEAT_F,):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self.root.destroy()
        finally:
            os._exit(0)


def main() -> None:
    # DPI awareness before the first window (Windows fixes it then; the ticker learned this the hard way)
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
    except (AttributeError, OSError):
        pass
    if single_instance() is None:
        sys.exit(0)                                   # already showing
    import tkinter as tk
    root = tk.Tk()
    AwayScreen(root, panel="--panel" in sys.argv[1:])
    root.mainloop()


if __name__ == "__main__":
    main()
