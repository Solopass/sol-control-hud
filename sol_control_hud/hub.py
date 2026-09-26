"""SOL Control HUD: one process, one data collector, two views.

    pythonw -m sol_control_hud                      # start with your saved views (ticker on by default)
    pythonw -m sol_control_hud --show ticker        # or: dashboard | both | tray (nothing but the tray icon)

- One `TickerCollector` (and its one GPU sampler) feeds the ticker window and the web dashboard.
- The dashboard is served by this same process on 127.0.0.1:<port> (7900; SOL_CONTROL_PORT overrides it). The web-only
  VRAM guard thread starts when a browser first asks for it and stops after 5 idle minutes.
- A tray icon keeps it reachable with no view open: show/hide the ticker, open the dashboard, exit.
- Pace: collect every 2 s while a view shows it; every 10 s while nobody looks (ticker hidden or covered by a
  fullscreen game, no dashboard request for 30 s).
- Starting it again while it runs doesn't start a second copy: it tells the running one what to show (POST /api/views).
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import json
import msvcrt
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser

from .data.collectors import engines, vram
from .data.snapshot import TickerCollector, attention, format_multiline_rows, format_slides
from .paths import DATA_DIR

SETTINGS_FILE = DATA_DIR / "hub-settings.json"
LOCK_FILE = DATA_DIR / "hub.lock"
LOG_FILE = DATA_DIR / "hub.log"
DEFAULTS = {"ticker": True, "dashboard_at_start": False, "port": 7900}
ACTIVE_PACE, IDLE_PACE = 2.0, 10.0
WEB_ACTIVE_S = 30.0          # a dashboard request within this long counts as "someone is looking"
GUARD_IDLE_S = 300.0         # stop the web-only VRAM guard thread after this long without a dashboard request


def log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except OSError:
        pass


def port_of(settings: dict) -> int:
    return int(os.environ.get("SOL_CONTROL_PORT") or settings.get("port") or 7900)


def views_for(show: str | None, settings: dict) -> tuple[bool, bool]:
    """(ticker, dashboard) for this start: --show wins, else the saved settings."""
    if show == "ticker":
        return True, False
    if show == "dashboard":
        return False, True
    if show == "both":
        return True, True
    if show == "tray":
        return False, False
    return bool(settings["ticker"]), bool(settings["dashboard_at_start"])


def pace_for(ticker_visible: bool, seconds_since_web: float) -> float:
    return ACTIVE_PACE if ticker_visible or seconds_since_web < WEB_ACTIVE_S else IDLE_PACE


def snapshot_payload(collector, views: dict) -> dict:
    """What the dashboard gets: the same snapshot, alerts, slides and rows the ticker renders (computed once)."""
    s = collector.get_snapshot()
    return {"snapshot": dataclasses.asdict(s), "attention": attention(s), "slides": format_slides(s),
            "rows": format_multiline_rows(s), "views": views}


class LazyGuard:
    """The web dashboard's VRAM guard loop, started on first use and stopped when nobody has looked for a while."""

    def __init__(self, sampler):
        self.sampler, self.loop, self.last_used = sampler, None, 0.0
        self._lock = threading.Lock()

    @property
    def latest(self) -> dict:
        with self._lock:
            self.last_used = time.monotonic()
            if self.loop is None:
                self.loop = vram.GuardLoop(vram.VramGuard(), self.sampler, engines.ollama)
                self.loop.start()
                return {"available": False, "error": "starting"}
            return self.loop.latest

    def stop_if_idle(self, idle_s: float = GUARD_IDLE_S) -> None:
        with self._lock:
            if self.loop is not None and time.monotonic() - self.last_used > idle_s:
                self.loop.stop()
                self.loop = None

    def stop(self) -> None:
        with self._lock:
            if self.loop is not None:
                self.loop.stop()
                self.loop = None


class Hub:
    def __init__(self, settings: dict, show_ticker: bool, open_dashboard: bool):
        self.settings = settings
        self.port = port_of(settings)
        self.url = f"http://127.0.0.1:{self.port}"
        self.start_ticker, self.start_dashboard = show_ticker, open_dashboard
        self.cmds: queue.Queue = queue.Queue()
        self.last_web = -1e9
        self.pace = ACTIVE_PACE
        self.ticker = self.server = self.tray = self.root = None

    # ---- callable from any thread (the tray thread, the web server, the ticker's Tk callbacks)
    def show_ticker(self) -> None:
        self.cmds.put("show_ticker")

    def hide_ticker(self) -> None:
        self.cmds.put("hide_ticker")

    def open_dashboard(self) -> None:
        webbrowser.open(f"{self.url}/?t={int(time.time())}")   # a new address skips any cached page

    def exit(self, why: str = "exit") -> None:
        self.cmds.put(("exit", why))

    def views(self) -> dict:
        t = self.ticker
        return {"hub": True, "ticker": bool(t and not t.user_hidden), "pace_s": self.pace,
                "dashboard_at_start": bool(self.settings["dashboard_at_start"])}

    # ---- setup
    def run(self) -> None:
        import tkinter as tk

        from .views.ticker import TickerApp
        self.collector = TickerCollector()
        self.collector.start()
        self.guard = LazyGuard(self.collector._sampler)
        self._start_web()
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda t, v, tb: log("error: " + "".join(traceback.format_exception(t, v, tb)))
        self.ticker = TickerApp(self.root, collector=self.collector, hub=self)
        if not self.start_ticker:
            self.ticker.set_user_hidden(True)
        self._start_tray()
        if self.start_dashboard:
            self.open_dashboard()
        log(f"start (pid {os.getpid()}, dashboard {self.url}, ticker {'on' if self.start_ticker else 'hidden'})")
        self.root.after(250, self._pump)
        self.root.after(2000, self._tick)
        try:
            self.root.mainloop()
        finally:
            log("mainloop ended")

    def _start_web(self) -> None:
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(self.build_app(), host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._serve, name="web", daemon=True).start()

    def build_app(self):
        """The dashboard's web app: the old /api/status (cards) plus /api/snapshot and /api/views, on this hub's data."""
        from fastapi import Body, Request

        from .views.web.app import build_collectors, create_app
        app = create_app(collectors=build_collectors(self.collector._sampler, self.guard))

        @app.middleware("http")
        async def seen(request: Request, call_next):
            if request.url.path.startswith("/api/"):
                self.last_web = time.monotonic()      # someone has the dashboard open: keep the 2 s pace
            return await call_next(request)

        @app.get("/api/snapshot")
        def snapshot() -> dict:
            return snapshot_payload(self.collector, self.views())

        @app.get("/api/views")
        def get_views() -> dict:
            return self.views()

        @app.post("/api/views")
        def set_views(body: dict = Body(default={})) -> dict:
            if "ticker" in body:
                (self.show_ticker if body["ticker"] else self.hide_ticker)()
            if body.get("dashboard"):
                self.open_dashboard()
            if "dashboard_at_start" in body:
                self.cmds.put(("dashboard_at_start", bool(body["dashboard_at_start"])))
            if body.get("exit"):
                self.exit("dashboard")
            return {**self.views(), "queued": True}

        return app

    def _serve(self) -> None:
        try:
            self.server.run()
        except BaseException as e:  # noqa: BLE001 - e.g. the port is taken: the ticker and tray keep working
            log(f"dashboard server stopped: {type(e).__name__}: {e}")

    def _start_tray(self) -> None:
        from .tray import Tray

        def menu():
            ticker_on = bool(self.ticker and not self.ticker.user_hidden)
            return [("Show ticker", self.hide_ticker if ticker_on else self.show_ticker, ticker_on),
                    ("Open dashboard", self.open_dashboard, False),
                    None,
                    ("Open the dashboard when SOL starts",
                     lambda: self.cmds.put(("dashboard_at_start", not self.settings["dashboard_at_start"])),
                     bool(self.settings["dashboard_at_start"])),
                    None,
                    ("Exit SOL Control HUD", lambda: self.exit("tray Exit"), False)]
        # click: show the ticker if it's hidden, else open the dashboard
        self.tray = Tray("SOL Control HUD", menu,
                         on_click=lambda: (self.open_dashboard() if self.ticker and not self.ticker.user_hidden
                                           else self.show_ticker()))
        if not self.tray.start():
            log("tray icon could not be created")

    # ---- the Tk thread
    def _pump(self) -> None:
        try:
            while True:
                cmd = self.cmds.get_nowait()
                name, arg = (cmd, None) if isinstance(cmd, str) else cmd
                if name in ("show_ticker", "hide_ticker"):
                    self.ticker.set_user_hidden(name == "hide_ticker")
                    self.settings["ticker"] = name == "show_ticker"      # the next start shows what you left
                    save_settings(self.settings)
                elif name == "dashboard_at_start":
                    self.settings["dashboard_at_start"] = arg
                    save_settings(self.settings)
                elif name == "exit":
                    self._shutdown(arg)
                    return
        except queue.Empty:
            pass
        self.root.after(250, self._pump)

    def _tick(self) -> None:
        """Every 2 s: set the pace, stop the idle web guard, keep the tray tooltip current."""
        try:
            t = self.ticker
            visible = bool(t and not t.user_hidden and not t._is_hidden_for_fullscreen)
            pace = pace_for(visible, time.monotonic() - self.last_web)
            if pace != self.pace:
                self.pace = pace
                self.collector.set_pace(pace)
                log(f"pace {pace:.0f} s ({'someone is looking' if pace == ACTIVE_PACE else 'nobody is looking'})")
            self.guard.stop_if_idle()
            if self.tray:
                alerts = attention(self.collector.get_snapshot())
                self.tray.set_tooltip("SOL Control HUD: " + (alerts[0][1] if alerts else "all fine"))
        except Exception as e:  # noqa: BLE001
            log(f"tick error: {type(e).__name__}: {e}")
        self.root.after(2000, self._tick)

    def _shutdown(self, why: str) -> None:
        log(f"exit: {why}")
        for step in (lambda: self.tray and self.tray.stop(), lambda: setattr(self.server, "should_exit", True),
                     self.collector.stop, self.guard.stop, self.ticker._save_settings, self.root.destroy):
            try:
                step()
            except Exception:  # noqa: BLE001 - leave no matter what
                pass
        os._exit(0)


def tell_running_hub(port: int, show: str | None) -> bool:
    """Starting it again: ask the running copy to show what was asked for (default: the ticker)."""
    import httpx
    body = {"ticker": True} if show in (None, "ticker", "both") else {}
    if show in ("dashboard", "both"):
        body["dashboard"] = True
    try:
        return httpx.post(f"http://127.0.0.1:{port}/api/views", json=body, timeout=3).status_code == 200
    except Exception:
        return False


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m sol_control_hud")
    p.add_argument("--show", choices=["ticker", "dashboard", "both", "tray"], help="which views to show this time")
    args = p.parse_args(argv)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:   # pythonw: uvicorn/print would crash on a None stream
        stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout, sys.stderr = sys.stdout or stream, sys.stderr or stream
    sys.excepthook = lambda t, v, tb: log("crash: " + "".join(traceback.format_exception(t, v, tb)))
    settings = load_settings()
    lock = open(LOCK_FILE, "a+b")
    try:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        ok = tell_running_hub(port_of(settings), args.show)
        log(f"already running: asked it to show {args.show or 'ticker'} ({'ok' if ok else 'no answer'})")
        return
    try:   # the attached-desktop and DPI setup the ticker needs, before the first Tk window
        hdesk = ctypes.windll.user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            ctypes.windll.user32.SetThreadDesktop(hdesk)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass
    show_ticker, show_dashboard = views_for(args.show, settings)
    Hub(settings, show_ticker, show_dashboard).run()
