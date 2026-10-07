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
from pathlib import Path

from fastapi import Request   # module level: annotations are strings here, FastAPI resolves them from globals

from . import heal, models_ctl
from .data.collectors import engines, vram
from .data.snapshot import RED, TickerCollector, attention, format_multiline_rows, format_slides
from .paths import DATA_DIR

SETTINGS_FILE = DATA_DIR / "hub-settings.json"
LOCK_FILE = DATA_DIR / "hub.lock"
LOG_FILE = DATA_DIR / "hub.log"
# "I'm running" marker for tools\sol-llm-watch.ps1: present + its pid gone = the app died -> the watcher restarts it.
# A clean exit (tray / ticker / dashboard Exit) removes it, so what you close stays closed.
RUNNING_FILE = DATA_DIR / "app-running.json"
DEFAULTS = {"ticker": True, "dashboard_at_start": False, "port": 7900, "notify": None}   # notify: see notify.DEFAULTS
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


def write_running_marker(path=None) -> None:
    path = path or RUNNING_FILE
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    except OSError:
        pass


def clear_running_marker(path=None) -> None:
    try:
        (path or RUNNING_FILE).unlink()
    except OSError:
        pass


def start_at_login(on: bool | None = None) -> bool:
    """The Windows Startup entry 'SOL Control HUD' (Task Manager lists it under startup apps). on=None: just read it."""
    from .views.ticker import is_startup_enabled, set_startup
    if on is not None:
        set_startup(on)
    return is_startup_enabled()


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


def stream_message(prev: dict | None, payload: dict) -> tuple[dict, dict]:
    """What to push this tick: everything the first time ({"full": ...}), then only what changed: top-level parts in
    "patch", single snapshot fields in "snap" (the snapshot changes a few fields a tick, not all ~70).
    Returns (message, state to pass back next time)."""
    dump = lambda v: json.dumps(v, default=str, sort_keys=True, separators=(",", ":"))  # noqa: E731
    snap = payload.get("snapshot") or {}
    state = {"parts": {k: dump(v) for k, v in payload.items() if k != "snapshot"}, "snap": {k: dump(v) for k, v in snap.items()}}
    if not prev:
        return {"full": payload}, state
    msg = {}
    patch = {k: payload[k] for k, d in state["parts"].items() if prev["parts"].get(k) != d}
    fields = {k: snap[k] for k, d in state["snap"].items() if prev["snap"].get(k) != d}
    if patch:
        msg["patch"] = patch
    if fields:
        msg["snap"] = fields
    return msg, state


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
                self.loop = vram.GuardLoop(vram.VramGuard(), self.sampler, engines.ollama, interval=4.0)   # a verdict every 4 s is plenty
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
        self._login = False          # the Startup entry exists (read at start, changed through do_action)
        self.notifier = None
        self._prev_snap = None
        self.metrics = None
        from .views.web.feed import History
        self.history = History()

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
                "dashboard_at_start": bool(self.settings["dashboard_at_start"]),
                "theme": getattr(t, "theme_name", None),       # one theme for both views
                "start_at_login": self._login,
                "notify": bool((self.settings.get("notify") or {}).get("enabled", True))}

    OPEN_TARGETS = ("AI", "RUN", "NOTE", "GIT", "SYS", "DISK", "NET", "MEDIA", "NIGHT", "HW")

    def do_action(self, action: str, target: str = "", body: dict | None = None) -> dict:
        """The dashboard's buttons. Machine changes go through the same safe paths as the ticker and the Away panel."""
        import subprocess
        llm = Path(r"D:\AI\Cache\llm")
        if action == "stop_ai":
            state = json.loads((llm / "state.json").read_text(encoding="utf-8")) if (llm / "state.json").exists() else {}
            if state.get("mode") != "away":
                return {"ok": False, "why": "Away isn't running"}
            (llm / "away-stop.flag").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
            return {"ok": True, "why": "stopping: the job goes back in the queue"}
        if action in ("away", "away-sleep"):
            script = Path(r"D:\OBVLT\tools\sol-llm.ps1")
            subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), action],
                             creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "why": f"starting {action}"}
        if action == "open" and target in self.OPEN_TARGETS:
            self.cmds.put(("open", target))       # the ticker's own targets (folders, the daily note, reports)
            return {"ok": True}
        if action == "theme" and target:
            self.cmds.put(("theme", target))
            return {"ok": True}
        if action in ("model", "models_unload_all", "ai_power"):
            from . import models_ctl
            try:
                router = self.collectors["llama_swap"].get() if hasattr(self, "collectors") else {}
                running = (router or {}).get("running") or []
                if action == "model":
                    why = models_ctl.model_op(target, str((body or {}).get("op", "")), [m.get("model") for m in running])
                elif action == "models_unload_all":
                    why = models_ctl.unload_all([m.get("model") for m in running if m.get("state") in ("loaded", "sleeping", "loading")])
                else:
                    why = models_ctl.ai_power(target == "on")
                if hasattr(self, "collectors"):
                    self.collectors["llama_swap"].value = None     # ask the router again on the next update
                log(f"models: {action} {target} {(body or {}).get('op', '')}".strip())
                return {"ok": True, "why": why}
            except models_ctl.ModelError as e:
                return {"ok": False, "why": str(e)}
        if action in ("ask", "ask_remove", "chain"):
            from . import control
            b = body or {}
            try:
                if action == "ask":
                    files = b.get("files") or []
                    if b.get("now"):
                        r = control.ask_now(str(b.get("title", "")), str(b.get("question", "")),
                                            [str(x) for x in files] if isinstance(files, list) else [],
                                            str(b.get("model") or "sol-smart"))
                        log(f"asked now: {r['job']}")
                        return {"ok": True, "why": f"answering now on {b.get('model') or 'sol-smart'} "
                                                   "(it shows in Answers when done)", **r}
                    r = control.queue_ask(str(b.get("title", "")), str(b.get("question", "")),
                                          [str(x) for x in files] if isinstance(files, list) else [], str(b.get("model") or "sol-away"))
                    log(f"asked overnight: {r['job']}")
                    return {"ok": True, "why": f"queued: {r['job'][4:]} (it runs in the next Away session)", **r}
                if action == "ask_remove":
                    control.remove_ask(target)
                    return {"ok": True, "why": "removed from the queue"}
                if str(b.get("op", "")) == "schedule":
                    canon = control.set_schedule(target, str(b.get("schedule") or ""))
                    log(f"chain {target}: schedule {canon or 'removed'} (dashboard)")
                    return {"ok": True, "why": f"{target}: " + (f"runs {canon}" if canon else "no schedule"), "schedule": canon}
                status = control.chain_op(target, str(b.get("op", "")))
                log(f"chain {target}: status {status} (dashboard)")
                why = f"{target}: running now, skipping the wait" if b.get("op") == "now" else f"{target}: {status}"
                return {"ok": True, "why": why, "status": status}
            except control.ControlError as e:
                return {"ok": False, "why": str(e)}
        if action == "notify" and target in ("on", "off", "test"):
            if target == "test":
                return {"ok": self.notify_now("SOL Control HUD", "Notifications work. You'll see Away results, chain failures, "
                                              "crashes, disks filling up and VRAM spills here."), "why": "test notification sent"}
            if not isinstance(self.settings.get("notify"), dict):
                from . import notify
                self.settings["notify"] = json.loads(json.dumps(notify.DEFAULTS))
            self.settings["notify"]["enabled"] = target == "on"
            if self.notifier:
                self.notifier.settings = self.settings["notify"]
            save_settings(self.settings)
            return {"ok": True, "why": f"notifications {target}", "notify": target == "on"}
        if action == "login_start" and target in ("on", "off"):
            self._login = start_at_login(target == "on")
            log(f"start at login: {'on' if self._login else 'off'}")
            return {"ok": True, "why": f"start at login: {'on' if self._login else 'off'}", "start_at_login": self._login}
        if action == "open_note":
            from .data.collectors import notes
            b = body or {}
            vault = str(b.get("vault") or "OBVLT")
            file_path = str(b.get("file") or target)
            r = notes.open_note(vault, file_path)
            log(f"open note: {vault}/{file_path} -> {r.get('ok')}")
            return r
        if action == "scratch":
            from .views.scratch_dialog import append_scratch_note
            b = body or {}
            text = str(b.get("text", "")).strip()
            dest = str(b.get("destination", "daily"))
            r = append_scratch_note(text, destination=dest)
            log(f"scratch note ({dest}): {r.get('success')} -> {r.get('message')}")
            return {"ok": bool(r.get("success")), "why": r.get("message", "")}
        if action == "open_folder":
            from .data.collectors import notes
            b = body or {}
            vault = str(b.get("vault") or "OBVLT")
            file_path = str(b.get("file") or target)
            r = notes.open_folder(vault, file_path)
            log(f"open folder: {vault}/{file_path} -> {r.get('ok')}")
            return r
        if action == "ack_crashes":
            from .views.crash_dialog import ACK_FILE
            from .views.ticker import write_crash_ack
            write_crash_ack(ACK_FILE)
            log("crashes acknowledged via web dashboard")
            return {"ok": True, "why": "Crash events acknowledged and cleared"}
        if action == "setting":                   # the dashboard's Settings panel (settings_api.py checks every value)
            from . import settings_api
            try:
                r = settings_api.change(self, target, (body or {}).get("value"))
            except settings_api.SettingError as e:
                return {"ok": False, "why": str(e)}
            log(f"setting {target} = {(body or {}).get('value')!r}: {r.get('why')}")
            return r
        if action in ("project", "media"):
            return self._launch(action, target, str((body or {}).get("op", "")), body or {})
        if action == "close_port":
            # ports.close_listener looks the port up itself and refuses the AI stack, Windows services and us
            from .data.collectors import ports
            b = body or {}
            r = ports.close_listener(target or b.get("port"), b.get("pid"))
            log(f"close port {target or b.get('port')}: {r.get('why')}")
            if hasattr(self, "collectors") and "ports" in self.collectors:
                self.collectors["ports"].value = None      # the card lists what's left on its next poll
            return r
        return {"ok": False, "why": f"unknown action {action!r}"}

    # ---- the Projects and Media APIs cards
    def _launchpad(self) -> tuple[dict, dict, list[dict]]:
        """(registry, WSL probe, Windows listeners): what both cards are built from."""
        from .data.collectors import projects
        reg = projects.load_registry()
        probe = self.collectors["media_probe"].get() if hasattr(self, "collectors") else {}
        listening = ((self.collectors["ports"].get() or {}).get("listening") or []) if hasattr(self, "collectors") else []
        return reg, probe if isinstance(probe, dict) else {}, listening

    def projects_payload(self) -> dict:
        from .data.collectors import media_apis, projects
        reg, probe, listening = self._launchpad()
        return projects.collect(listening, media_apis.running_ports(reg, probe), registry=reg)

    def media_payload(self) -> dict:
        from .data.collectors import media_apis
        reg, probe, listening = self._launchpad()
        wsl_state = (self.collectors["wsl"].get() or {}).get("state") if hasattr(self, "collectors") else None
        from . import launcher
        return {**media_apis.collect(reg, listening, wsl_state, probe=probe), "notes": launcher.note_runs()}

    def _refresh_launchpad(self) -> None:
        """After a start / stop: look again on the next poll instead of serving the cached state."""
        for key in ("ports", "media_probe", "projects", "media", "wsl"):
            if hasattr(self, "collectors") and key in self.collectors:
                self.collectors[key].value = None

    def _launch(self, kind: str, name: str, op: str, body: dict | None = None) -> dict:
        from . import launcher
        from .data.collectors import projects
        reg = projects.load_registry()
        b = body or {}
        # changes that need no current state: your tags / hides, and the transcript box
        if kind == "project" and op in ("hide", "show", "tag"):
            r = projects.set_user(name, op, str(b.get("tag") or ""), workspace=reg["workspace"])
            if "projects" in getattr(self, "collectors", {}):
                self.collectors["projects"].value = None
            log(f"project {op} {name} {b.get('tag') or ''}: {r.get('why')}".strip())
            return r
        if kind == "media" and op == "note":
            r = launcher.start_transcript_note(str(b.get("source") or ""), str(b.get("title") or ""))
            log(f"transcript note {str(b.get('source') or '')[:120]}: {r.get('why')}")
            if "media" in getattr(self, "collectors", {}):
                self.collectors["media"].value = None
            return r
        if kind == "media" and op == "open_note":
            return launcher.open_note_run(b.get("run"))
        self._refresh_launchpad()                     # act on the current state, not a cached one
        try:
            rows = self.projects_payload()["projects"]
            tiles = self.media_payload()["tiles"]
            if kind == "project":
                if op == "start":
                    r = launcher.start_project(reg, name, rows, tiles)
                elif op == "stop":
                    r = launcher.stop_project(reg, name, rows, tiles)
                elif op in ("folder", "editor", "file"):
                    r = launcher.open_project(reg, name, op)
                else:
                    r = {"ok": False, "why": f"unknown project action {op!r}"}
            elif op == "start":
                r = launcher.start_media(reg, name, tiles, rows)
            elif op == "stop":
                r = launcher.stop_media(reg, name, tiles, rows)
            elif op == "folder":
                r = launcher.open_media_folder(reg, name)
            else:
                r = {"ok": False, "why": f"unknown media action {op!r}"}
        except launcher.LaunchError as e:
            r = {"ok": False, "why": str(e)}
        if op in ("start", "stop"):
            log(f"{kind} {op} {name}: {r.get('why')}")
            projects.forget_git(name)
            self._refresh_launchpad()
        return r

    # ---- setup
    def run(self) -> None:
        import tkinter as tk

        from .views.ticker import TickerApp
        self.collector = TickerCollector()
        self.collector.start()
        self.guard = LazyGuard(self.collector._sampler)
        self._heal_state = heal.HealState()          # auto-heal a model that spilled into system RAM (heal.py)
        self._last_big_vram_change = time.monotonic()
        self._prev_others_gb = None
        self._start_web()
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda t, v, tb: log("error: " + "".join(traceback.format_exception(t, v, tb)))
        self.ticker = TickerApp(self.root, collector=self.collector, hub=self)
        if not self.start_ticker:
            self.ticker.set_user_hidden(True)
        self._start_tray()
        if self.start_dashboard:
            self.open_dashboard()
        try:
            self._login = start_at_login()
        except Exception:  # noqa: BLE001 - reading a shortcut must never stop the app
            self._login = False
        write_running_marker()
        if self.settings.get("gpu_choices"):       # Discord moves to a new folder on update: put the choice on it
            try:
                from . import gpu_prefs
                gpu_prefs.reapply(self.settings["gpu_choices"])
            except Exception as e:  # noqa: BLE001 - a registry hiccup must never stop the app
                log(f"gpu choices not re-applied: {type(e).__name__}: {e}")
        self._start_notifier()
        try:
            from .metrics import Metrics
            llm = Path(r"D:\AI\Cache\llm")
            self.metrics = Metrics(DATA_DIR / "metrics.sqlite", llm / "away.jsonl", llm / "chains.log")
        except Exception as e:  # noqa: BLE001 - history is a nicety; the app runs without it
            log(f"metrics off: {type(e).__name__}: {e}")
        log(f"start (pid {os.getpid()}, dashboard {self.url}, ticker {'on' if self.start_ticker else 'hidden'}, "
            f"start at login {'on' if self._login else 'off'})")
        self.root.after(250, self._pump)
        self.root.after(2000, self._tick)
        try:
            self.root.mainloop()
        finally:
            log("mainloop ended")

    def _start_notifier(self) -> None:
        from . import notify
        from .data.snapshot import night_summary
        if not isinstance(self.settings.get("notify"), dict):
            self.settings["notify"] = json.loads(json.dumps(notify.DEFAULTS))
        llm = Path(r"D:\AI\Cache\llm")
        self.notifier = notify.Notifier(self.settings["notify"], llm / "away.jsonl", llm / "chains.log",
                                        session_fn=night_summary)

    def notify_now(self, title: str, text: str, level: str = "info") -> bool:
        ok = bool(self.tray and self.tray.notify(title, text, level))
        log(f"notice ({level}): {title}: {text[:120]}{'' if ok else ' [not shown: no tray]'}")
        return ok

    def _start_web(self) -> None:
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(self.build_app(), host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._serve, name="web", daemon=True).start()

    def dashboard_payload(self) -> dict:
        """Everything the dashboard shows, in one message (pushed every 2 s over /api/stream)."""
        c = self.collectors
        p = snapshot_payload(self.collector, self.views())
        p.pop("rows", None)                           # the ticker's multi-line rows: the page doesn't use them
        return {**p, "cpu": c["cpu"].get(), "gpu": c["gpu"].get(), "vram_guard": c["vram"].get(),
                "router": c["llama_swap"].get(), "wsl": c["wsl"].get(), "stability": c["stability"].get(),
                "away": c["away"].get(), "chains": c["chains"].get(), "activity": c["activity"].get(),
                "point": self.history.latest(), "models": self._model_rows(),
                "speed": c["speed"].get(), "heal": c["heal"].get()}

    def _model_rows(self) -> list[dict]:
        from .models_ctl import rows
        c = self.collectors
        try:
            return rows(c["llama_swap"].get(), c["gpu"].get(), c["model_procs"].get(), c["model_desc"].get())
        except Exception:  # noqa: BLE001 - a display nicety
            return []

    def build_app(self):
        """The dashboard's web app on this hub's data: the page + /static, /api/stream (pushed), /api/snapshot,
        /api/history, /api/meta, /api/views and /api/action (POST: our page only), /api/status (the old cards)."""
        import asyncio

        from fastapi import Body
        from fastapi.responses import JSONResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles

        from .views.web import feed
        from .views.web.app import STATIC, Cached, build_collectors, create_app
        self.collectors = build_collectors(self.collector._sampler, self.guard)
        from . import models_ctl
        from .data.collectors import cpu, ports
        self.collectors["ports"] = Cached(ports.listeners, 3)      # also keeps the open/closed history up to date
        self.collectors.update({"cpu": Cached(cpu.sample, 1.5),    # per-core load + clock for the CPU · GPU card
                                "model_procs": Cached(models_ctl.model_processes, 5),
                                "model_desc": Cached(models_ctl.descriptions, 60)})
        self.collectors.update({"away": Cached(feed.away_info, 1.5), "chains": Cached(lambda: feed._read_json(feed.LLM_DIR / "chains.json"), 1.5),
                                "activity": Cached(feed.activity, 5)})
        from .data.collectors import media_apis, projects

        def probe() -> dict:          # one wsl.exe for every media service, never while WSL is stopped
            reg = projects.load_registry()
            return media_apis.wsl_probe(reg["media"], media_apis.wsl_port_list(reg), reg.get("wsl_distro", "Ubuntu-24.04"),
                                        (self.collectors["wsl"].get() or {}).get("state"))
        self.collectors.update({"media_probe": Cached(probe, 10), "projects": Cached(self.projects_payload, 4),
                                "media": Cached(self.media_payload, 4)})
        from .data.collectors import speed           # real answer speed (router.log) and the watcher's spill auto-heal
        self.collectors.update({"speed": Cached(lambda: speed.answer_speed(self.collectors["model_procs"].get()), 3),
                                "heal": Cached(speed.heal_status, 10)})
        app = create_app(collectors=self.collectors)
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.middleware("http")
        async def guard_and_seen(request: Request, call_next):
            if request.method == "POST":
                # only this page may change things: a custom header forces a CORS preflight (which nothing here
                # answers), and a browser's Origin must be this server. Other web pages can't press these buttons.
                origin = request.headers.get("origin")
                if request.headers.get("x-sol-control") != "1" or (origin and origin not in (
                        self.url, f"http://localhost:{self.port}")):
                    return JSONResponse({"error": "not allowed"}, status_code=403)
            if request.url.path.startswith("/api/"):
                self.last_web = time.monotonic()      # someone has the dashboard open: keep the 2 s pace
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                # revalidate every time (cheap: 304 if unchanged): after an update the browser must not keep an old
                # app.js (09-25 lesson with the old HUD's page)
                response.headers["Cache-Control"] = "no-cache"
            return response

        @app.get("/api/stream")
        async def stream(request: Request):
            async def events():
                state = None
                while not await request.is_disconnected():
                    self.last_web = time.monotonic()  # an open stream = a dashboard on screen (hidden tabs close it)
                    payload = await asyncio.to_thread(self.dashboard_payload)
                    msg, state = stream_message(state, payload)   # after the first message: only what changed
                    yield f"data: {json.dumps(msg, default=str, separators=(',', ':'))}\n\n"
                    await asyncio.sleep(ACTIVE_PACE)
            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        @app.get("/api/history")
        def history(range: str = "1h") -> dict:   # noqa: A002 - the query parameter's name
            if range in ("24h", "7d"):
                from .metrics import series
                return series(DATA_DIR / "metrics.sqlite", 24 if range == "24h" else 168)
            return self.history.export()

        @app.get("/api/week")
        def week_summary() -> dict:
            from .metrics import week
            return week(DATA_DIR / "metrics.sqlite")

        @app.get("/api/meta")
        def meta() -> dict:
            from .views.ticker import THEMES
            theme = getattr(self.ticker, "theme_name", "cyber-cyan")
            return {"themes": THEMES, "theme": theme, "port": self.port, "pid": os.getpid()}

        @app.post("/api/action")
        def action(body: dict = Body(default={})) -> dict:
            return self.do_action(str(body.get("action", "")), str(body.get("target", "")), body)

        @app.get("/api/control")
        def control_state() -> dict:
            from . import control
            return {"models": control.away_models(), "desk_models": control.desk_models(), "asks": control.list_asks(), "answers": control.list_answers(),
                    "chains": control.list_chains()}

        @app.get("/api/answer")
        def answer(file: str = "") -> dict:
            from . import control
            try:
                return {"ok": True, "text": control.read_answer(file)}
            except control.ControlError as e:
                return {"ok": False, "why": str(e)}

        @app.get("/api/chain-result")
        def chain_result(name: str = "") -> dict:
            from . import control
            try:
                return {"ok": True, "text": control.chain_result(name)}
            except control.ControlError as e:
                return {"ok": False, "why": str(e)}

        @app.get("/api/vaults")
        def vaults_list() -> dict:
            from .data.collectors import notes
            discovered = notes.discover_vaults()
            names = list(discovered.keys())
            if "OBVLT" in names:
                names.remove("OBVLT")
                names.insert(0, "OBVLT")
            return {"ok": True, "vaults": names, "default": "OBVLT" if "OBVLT" in names else (names[0] if names else "")}

        @app.get("/api/notes")
        def notes_list(vault: str = "OBVLT", limit: int = 30, q: str = "") -> dict:
            from .data.collectors import notes
            items = notes.list_notes(vault_name=vault, limit=min(max(1, limit), 100), query=q)
            return {"ok": True, "vault": vault, "count": len(items), "notes": items}

        @app.get("/api/note-content")
        def note_content(vault: str = "OBVLT", file: str = "") -> dict:
            from .data.collectors import notes
            return notes.read_note_content(vault_name=vault, rel_path=file)

        @app.post("/api/note-save")
        def note_save(body: dict = Body(default={})) -> dict:
            from .data.collectors import notes
            return notes.save_note_content(
                vault_name=str(body.get("vault") or "OBVLT"),
                rel_path=str(body.get("file") or ""),
                text=str(body.get("text", "")),
            )

        @app.post("/api/note-create")
        def note_create(body: dict = Body(default={})) -> dict:
            from .data.collectors import notes
            return notes.create_note(
                vault_name=str(body.get("vault") or "OBVLT"),
                rel_path=str(body.get("file") or ""),
                initial_text=str(body.get("text", "")),
            )

        @app.get("/api/git-status")
        def git_repo_status(repo: str = "") -> dict:
            import subprocess
            ws = Path(r"D:\Workspace")
            target_repo = (ws / repo).resolve()
            try:
                target_repo.relative_to(ws.resolve())
            except ValueError:
                return {"ok": False, "why": "Invalid repo path"}
            if not target_repo.exists() or not (target_repo / ".git").exists():
                return {"ok": False, "why": f"Repo '{repo}' not found"}
            try:
                proc = subprocess.run(
                    ["git", "status", "--short", "--branch"],
                    cwd=str(target_repo),
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                return {"ok": True, "repo": repo, "status": proc.stdout.strip()}
            except Exception as e:
                return {"ok": False, "why": str(e)}

        @app.get("/api/crashes")
        def list_crashes() -> dict:
            from .views.crash_dialog import parse_unacknowledged_crashes
            events = parse_unacknowledged_crashes()
            return {"ok": True, "count": len(events), "events": events}

        @app.get("/api/ports")
        def localhost_ports() -> dict:
            """What listens on this PC and what closed recently (the Localhost card polls this), with the graphics chip
            each one draws on when it uses one (the GPU sampler's pid_chips)."""
            data = dict(self.collectors["ports"].get() or {})
            chips = (self.collectors["gpu"].get() or {}).get("pid_chips") or {}
            data["listening"] = [{**r, "chip": chips.get(r.get("pid"))} for r in data.get("listening") or []]
            return {"ok": True, **data}

        @app.get("/api/settings")
        def settings_read() -> dict:
            from . import settings_api
            return {"ok": True, **settings_api.read(self)}

        @app.get("/api/projects")
        def projects_list() -> dict:
            """The Projects card: every folder in D:\\Workspace, its state and whether it runs."""
            return {"ok": True, **(self.collectors["projects"].get() or {})}

        @app.get("/api/media")
        def media_list() -> dict:
            """The Media APIs card. Built without sending a single request to a sleeping service."""
            return {"ok": True, **(self.collectors["media"].get() or {})}

        @app.get("/api/media-live")
        def media_live(name: str = "") -> dict:
            """Live data from a media service that's already running (the tile's "Live" button)."""
            from . import launcher
            from .data.collectors import projects
            self.collectors["media_probe"].value = self.collectors["media"].value = None   # its state right now
            tiles = (self.collectors["media"].get() or {}).get("tiles") or []
            try:
                return launcher.live_media(projects.load_registry(), name, tiles)
            except launcher.LaunchError as e:
                return {"ok": False, "why": str(e)}

        @app.get("/api/top-processes")
        def top_processes() -> dict:
            import psutil
            procs = []
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                try:
                    mem_info = p.info.get("memory_info")
                    mem_mb = (mem_info.rss if mem_info else 0) / (1024 * 1024)
                    procs.append({
                        "pid": p.info["pid"],
                        "name": p.info.get("name") or "",
                        "cpu": p.info.get("cpu_percent") or 0.0,
                        "mem_mb": round(mem_mb, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:5]
            top_mem = sorted(procs, key=lambda x: x["mem_mb"], reverse=True)[:5]
            return {"ok": True, "top_cpu": top_cpu, "top_mem": top_mem}

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
                    ("Unload AI models", lambda: self.do_action("models_unload_all", ""), False),
                    None,
                    ("Open the dashboard when SOL starts",
                     lambda: self.cmds.put(("dashboard_at_start", not self.settings["dashboard_at_start"])),
                     bool(self.settings["dashboard_at_start"])),
                    ("Start at login", lambda: self.do_action("login_start", "off" if self._login else "on"), self._login),
                    ("Notifications", lambda: self.do_action("notify", "off" if self.views()["notify"] else "on"),
                     self.views()["notify"]),
                    ("Send a test notification", lambda: self.do_action("notify", "test"), False),
                    None,
                    ("Exit SOL Control HUD", lambda: self.exit("tray Exit"), False)]
        # click: show the ticker if it's hidden, else open the dashboard
        self.tray = Tray("SOL Control HUD", menu,
                         on_click=lambda: (self.open_dashboard() if self.ticker and not self.ticker.user_hidden
                                           else self.show_ticker()),
                         on_notice_click=self.open_dashboard)
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
                elif name == "open":
                    if arg == "NIGHT":
                        self.ticker.open_night_summary()
                    else:
                        self.ticker._open_slide_target(arg)
                elif name == "theme":
                    self.ticker.set_theme(arg)         # one theme for both views
                elif name == "ticker_settings":        # the dashboard's Settings (checked in settings_api.py)
                    self.ticker.apply_new_settings(arg)
                elif name == "font_scale":
                    self.ticker.set_font_scale(arg)
                elif name == "slide":
                    tag, on = arg
                    if self.ticker.slides_enabled.get(tag, True) != on:
                        self.ticker.toggle_slide_enabled(tag)
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
            snap = self.collector.get_snapshot()
            self.history.add(snap)
            if self.metrics:
                self.metrics.add(snap)
                self.metrics.read_logs()
            if self.notifier and snap.sampled_at:
                busy = bool(t and t._is_hidden_for_fullscreen) or (snap.ai_mode == "off" and str(snap.ai_reason or "").startswith("game"))
                for n in self.notifier.check(self._prev_snap, snap, busy):
                    self.notify_now(n.title, n.text, n.level)
                self._prev_snap = snap
            self._maybe_heal(snap)
            if self.tray:
                alerts = attention(snap)
                if alerts:
                    top_color, top_text, _ = alerts[0]
                    if top_color == RED:
                        prefix = "🔴 CRASH ALERT: " if "crash" in top_text.lower() else "🔴 ALERT: "
                    else:
                        prefix = "🟡 "
                    self.tray.set_tooltip(f"{prefix}{top_text}")
                else:
                    self.tray.set_tooltip(f"🟢 SOL: {snap.ai_model or 'No model'} · {snap.ai_mode} · GPU {snap.gpu_temp or '--'}°C")
        except Exception as e:  # noqa: BLE001
            log(f"tick error: {type(e).__name__}: {e}")
        self.root.after(2000, self._tick)

    def _maybe_heal(self, snap) -> None:
        """A model that spilled into system RAM never gets back on its own: reload it (heal.py, VRAM plan step 4).
        Desk only - in Away a job owns the GPU, and `off` means nothing should load.

        Off by default since 2026-10-07: OBVLT's tools/sol-llm-watch.ps1 now heals spills itself (and also runs while
        the HUD doesn't), waiting while a chain, a queue job or a chat uses the model. Two healers reacting to one spill
        could reload twice. The HUD still shows the spill (VRAM card, alert); `"hud_heal": true` in hub-settings.json
        turns this one back on."""
        if not self.settings.get("hud_heal", False):
            return
        g = getattr(snap, "vram_guard", None) or {}
        if not g.get("available") or snap.ai_mode != "desk":
            return
        others = g.get("others_gb")
        if others is not None:
            if self._prev_others_gb is not None and abs(others - self._prev_others_gb) > 1.0:
                self._last_big_vram_change = time.monotonic()
            self._prev_others_gb = others
        ai = g.get("ai") or {}
        models = [m for m in (ai.get("models") or []) if m]
        model = models[0] if len(models) == 1 else None      # two loaded: leave it alone, we'd guess wrong
        size_gb = (ai.get("dedicated_gb") or 0) + (ai.get("shared_gb") or 0)
        gpu = getattr(snap, "gpu", None) or {}
        decision = heal.decide(
            self._heal_state, now=time.monotonic(), evicted=bool(g.get("evicted")), model=model,
            busy=(gpu.get("load_percent") or 0) > 25,        # generating: a reload would kill the answer
            fits=(g.get("room_gb") or 0) >= size_gb + 0.5,   # room for the whole model, else it spills again
            settled_s=time.monotonic() - self._last_big_vram_change)
        if decision.heal:
            log(f"heal: {decision.why}")
            log("heal: " + heal.heal(self._heal_state, model, self._reload_model,
                                     now=time.monotonic(), notify=lambda t, m: self.notify_now(t, m, "info")))

    def _reload_model(self, name: str) -> None:
        """Unload, wait for the card to give the memory back, load again. Both halves are models_ctl's own path."""
        served = [m.get("model") or m.get("id") for m in (engines.llama_swap().get("running") or [])] or [name]
        models_ctl.model_op(name, "unload", served)

        def _load_again() -> None:
            time.sleep(6)
            try:
                models_ctl.model_op(name, "load", served)
            except Exception as e:  # noqa: BLE001 - reported by the guard on the next tick
                log(f"heal: loading {name} again failed: {type(e).__name__}: {e}")
        threading.Thread(target=_load_again, name="heal-load", daemon=True).start()

    def _shutdown(self, why: str) -> None:
        clear_running_marker()                 # a clean exit: the watcher must not bring it back
        log(f"exit: {why}")
        for step in (lambda: self.tray and self.tray.stop(), lambda: setattr(self.server, "should_exit", True),
                     lambda: self.metrics and self.metrics.close(),
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
    if show in ("dashboard", "both", "open-dashboard"):
        body["dashboard"] = True
    try:
        return httpx.post(f"http://127.0.0.1:{port}/api/views", json=body, timeout=3).status_code == 200
    except Exception:
        return False


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m sol_control_hud")
    p.add_argument("--show", choices=["ticker", "dashboard", "both", "tray"], help="which views to show this time")
    p.add_argument("--open-dashboard", action="store_true",
                   help="open the dashboard on top of your saved views (the Desktop shortcut 'SOL Dashboard')")
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
        ok = tell_running_hub(port_of(settings), "open-dashboard" if args.open_dashboard else args.show)
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
    if args.open_dashboard:
        show_dashboard = True          # your saved ticker choice stays as it was
    Hub(settings, show_ticker, show_dashboard).run()
