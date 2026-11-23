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
import logging
import msvcrt
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from fastapi import Request  # noqa: F401 - used by hub_web.py as _hub.Request (route annotations)

from . import deck_services, health, heal, models_ctl
from .data.collectors import engines, vram
from .data.snapshot import RED, TickerCollector, attention, format_multiline_rows, format_slides
from .paths import DATA_DIR
from .swallow import note as _swallowed

HUD_TARGET_CPU = 1.5          # % of one core the HUD should stay under (health card, no dashboard open; the CPU notice fires at 10x for 10 min)

SETTINGS_FILE = DATA_DIR / "hub-settings.json"
LOCK_FILE = DATA_DIR / "hub.lock"
LOG_FILE = DATA_DIR / "hub.log"
# "I'm running" marker for the watcher (sol-watch.exe; formerly tools\sol-llm-watch.ps1): present + its pid gone = the app died -> the watcher restarts it.
# A clean exit (tray / ticker / dashboard Exit) removes it, so what you close stays closed.
RUNNING_FILE = DATA_DIR / "app-running.json"
MEDIA_CHECK_S = 300.0          # stuck media jobs (E3): the card's own file read, looked at by the hub every 5 min
DEFAULTS = {"ticker": True, "dashboard_at_start": False, "port": 7900, "notify": None}   # notify: see notify.DEFAULTS
ACTIVE_PACE, IDLE_PACE = 2.0, 10.0
WEB_ACTIVE_S = 30.0          # a dashboard request within this long counts as "someone is looking"
GUARD_IDLE_S = 300.0         # stop the web-only VRAM guard thread after this long without a dashboard request


def _is_cloud_model(model: str) -> bool:
    """Cloud models (Gemini) run on Google's servers; nothing private may be added to their prompts."""
    return str(model).strip().lower().startswith("gemini")


def log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


class ClosedConnectionFilter(logging.Filter):
    """Drop asyncio's 'Exception in callback _ProactorBasePipeTransport._call_connection_lost' records: on Windows a
    browser tab closing the dashboard stream makes socket.shutdown() raise WinError 10054 after the connection is gone
    (a CPython proactor quirk, harmless). It was most of hub.log's tracebacks (15 by 10-08), burying real errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, ConnectionResetError) and "_call_connection_lost" in record.getMessage())


def write_running_marker(path=None) -> None:
    path = path or RUNNING_FILE
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    except OSError:
        pass


def note_previous_end(path=None, boot_time: float | None = None) -> str | None:
    """Before this run's marker is written: did the last run reach Exit? One hub.log line if not (health.py counts them)."""
    path = path or RUNNING_FILE
    try:
        marker = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        marker = {}                       # there but unreadable: it still didn't end cleanly
    if boot_time is None:
        import psutil
        boot_time = psutil.boot_time()
    end = health.previous_end(marker if marker is None else (marker or {"pid": None}), boot_time)
    if end:
        log(f"{end} (pid {(marker or {}).get('pid')}, started {(marker or {}).get('started')})")
    return end


def read_health_logs(now: float | None = None) -> dict:
    """The log half of the HUD health card: unexpected ends and the newest real errors (last 64 KB of each log)."""
    from .views.ticker import LOG_FILE as TICKER_LOG
    hub_text = health.tail(LOG_FILE)
    from .swallow import LOG_FILE as SWALLOWED_LOG
    return {"restarts": health.restarts(hub_text, now),
            "errors": health.errors([("hub", hub_text), ("ticker", health.tail(TICKER_LOG))]),
            "handled": health.handled(health.tail(SWALLOWED_LOG), now)}


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


def snap_game_on(snap) -> bool:
    """The watcher's game guard turned the AI off for a game (state.json reason "game: ...")."""
    return bool(snap and snap.ai_mode == "off" and str(snap.ai_reason or "").startswith("game"))


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


class SharedPayload:
    """One dashboard payload build per tick for every /api/stream connection. A caller within `ttl` seconds of the last
    build reuses it; callers arriving while a build runs wait for that build. There is no background task: with no
    stream connected nothing builds. (Results are shared read-only: stream_message only reads them.)"""

    def __init__(self, build, ttl: float, clock=time.monotonic):
        self.build, self.ttl, self.clock = build, ttl, clock
        self.value, self.at, self.builds = None, -1e9, 0
        self._lock = None

    async def get(self) -> dict:
        import asyncio
        if self.value is not None and self.clock() - self.at < self.ttl:
            return self.value
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.value is not None and self.clock() - self.at < self.ttl:   # built while we waited
                return self.value
            self.value = await asyncio.to_thread(self.build)
            self.at = self.clock()
            self.builds += 1
            return self.value


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
        self.shared_payload = SharedPayload(lambda: self.dashboard_payload(), ACTIVE_PACE * 0.9)   # looked up late: tests patch dashboard_payload
        self.ticker = self.server = self.tray = self.root = self.exam_hud = None
        self._login = False          # the Startup entry exists (read at start, changed through do_action)
        self.notifier = None
        self._prev_snap = None
        self.metrics = None
        from .views.web.app import Cached
        self._health_logs = Cached(lambda: read_health_logs(), 60)     # the logs: once a minute is plenty
        self._last_health = 0.0
        self._last_media_check = 0.0
        self._media_stuck_seen: set[str] = set()
        self.autostart_results: dict[str, dict] = {}      # name -> {ok, why, at}: what the HUD's start-up autostart did
        self._hud_started = time.monotonic()
        self._deck_watch = deck_services.DownWatch()      # warns once per down period of an autostart project
        self._last_deck_check = 0.0
        self._cpu_watch = health.CpuWatch()               # warns once per episode of the HUD itself burning CPU
        self._last_cpu_check = 0.0
        self._last_digest_check = 0.0
        from . import layouts                      # the dashboard's layout presets (data/layouts.json)
        self._layouts = layouts.load()
        self._layout_auto = layouts.Auto()
        self._layouts_lock = threading.Lock()
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

    def do_action(self, action: str, target: str='', body: dict | None=None) -> dict:
        from . import hub_actions     # moved out of hub.py (2026-11-22)
        return hub_actions.do_action(self, action, target, body)

    def review_action(self, target: str, body: dict | None=None) -> dict:
        from . import hub_actions     # moved out of hub.py (2026-11-22)
        return hub_actions.review_action(self, target, body)

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

    def deck_services_payload(self) -> dict:
        """The Deck services card: reads the ports / security collectors only (never the projects collector, whose
        process and git scans would then run all the time, card shown or not)."""
        c = self.collectors
        from .data.collectors import projects
        reg = projects.load_registry()      # not self._launchpad(): that also reads the WSL probe collector
        listening = (c["ports"].get() or {}).get("listening") or []
        return deck_services.build_payload(reg, listening, self.autostart_results, c["security"].get() if "security" in c else {},
                                           time.time())

    def _deck_down_notices(self) -> list:
        from .notify import Notice
        payload = self.collectors["deck_services"].get() or {}
        names = {s["name"]: s for s in payload.get("services") or []}
        out = []
        for name, key in self._deck_watch.step(payload.get("down") or [], time.monotonic(), time.monotonic() - self._hud_started):
            res = (names.get(name) or {}).get("autostart")
            tail = f" The autostart failed: {res['why']}." if res and not res["ok"] else ""
            out.append(Notice("deck_down", f"{(names.get(name) or {}).get('title') or name} is down",
                              f"It has been down for over 2 minutes.{tail} Start it from the Deck services card.", "warning", key))
        return out

    def _cpu_notices(self, snap) -> list:
        from .notify import Notice
        cpu, target = getattr(snap, "self_cpu", None), HUD_TARGET_CPU
        if not self._cpu_watch.step(cpu, target, time.monotonic()):
            return []
        return [Notice("hud_cpu", "SOL HUD is using too much CPU",
                       f"SOL HUD is using more CPU than it should ({cpu:.0f}% of one core for 10 min). "
                       "Close dashboard tabs you aren't looking at, or restart the HUD.", "warning",
                       f"hud_cpu:{int(time.time())}")]

    def _start_autostart(self, delay_s: float = 15.0) -> None:
        """Once, after the network is up: start the projects marked `autostart: true` in launchpad.yaml.
        Only called from run(), so tests (which build the Hub without running it) never start anything."""
        threading.Thread(target=self._autostart, args=(delay_s,), name="autostart", daemon=True).start()

    def _autostart(self, delay_s: float) -> None:
        from . import launcher
        from .data.collectors import projects
        time.sleep(delay_s)
        try:
            reg = projects.load_registry()
            self._refresh_launchpad()
            rows = self.projects_payload()["projects"]
            tiles = self.media_payload()["tiles"]
            for r in launcher.autostart_projects(reg, rows, tiles):
                log(f"autostart {r['name']}: {'ok' if r['ok'] else 'skipped'}: {r['why']}")
                self.autostart_results[r["name"]] = {"ok": bool(r["ok"]), "why": str(r.get("why") or ""), "at": time.time()}
        except (launcher.LaunchError, OSError, KeyError, ValueError, TypeError):
            _swallowed("hub.autostart")

    def _refresh_launchpad(self, wsl: bool = True) -> None:
        """After a start / stop: look again on the next poll instead of serving the cached state. `wsl=False` (a
        Windows project) keeps the WSL probes: re-running wsl.exe cost every click ~0.3 s (2026-11-22)."""
        for key in ("ports", "projects", "media", *(("media_probe", "wsl") if wsl else ())):
            if hasattr(self, "collectors") and key in self.collectors:
                self.collectors[key].value = None

    def _launch(self, kind: str, name: str, op: str, body: dict | None=None) -> dict:
        from . import hub_launch     # moved out of hub.py (2026-11-22)
        return hub_launch._launch(self, kind, name, op, body)

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
        self._start_autostart()
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda t, v, tb: log("error: " + "".join(traceback.format_exception(t, v, tb)))
        self.ticker = TickerApp(self.root, collector=self.collector, hub=self)
        if not self.start_ticker:
            self.ticker.set_user_hidden(True)
        try:                                       # started mid-game: off from the first moment, not after the first tick
            from .data.snapshot import read_llm_state
            mode, reason, _ = read_llm_state()
            if mode == "off" and str(reason or "").startswith("game"):
                self.ticker.shut_down_for_game()
        except Exception as e:  # noqa: BLE001 - the first tick does the same 2 s later
            log(f"game check at start failed: {type(e).__name__}: {e}")
        self._start_tray()
        if self.start_dashboard:
            self.open_dashboard()
        try:
            self._login = start_at_login()
        except Exception:  # noqa: BLE001 - reading a shortcut must never stop the app
            self._login = False
        try:
            note_previous_end()
        except Exception as e:  # noqa: BLE001 - bookkeeping must never stop the app
            log(f"previous run check failed: {type(e).__name__}: {e}")
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
        try:
            from . import exam_review
            exam_review.watcher().set_on_item(lambda item: self.cmds.put(("review_update_hud", item)))
        except Exception as e:
            log(f"exam review wire failed: {e}")
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
                "speed": c["speed"].get(), "heal": c["heal"].get(), "review": self._review_state(),
                "ai_ops": c["ai_ops"].get() if "ai_ops" in c else {},
                "security": c["security"].get() if "security" in c else {},
                "deck_services": c["deck_services"].get() if "deck_services" in c else {},
                "ai_inbox": c["ai_inbox"].get() if "ai_inbox" in c else {},
                "network": c["network"].get() if "network" in c else {},
                "sol_pad": c["sol_pad"].get() if "sol_pad" in c else {},
                "layout": {"active": self._layouts["active"], "rev": self._layouts["rev"]}}

    @staticmethod
    def _review_state() -> dict:
        from . import exam_review
        return exam_review.watcher().state()

    def _model_rows(self) -> list[dict]:
        from .models_ctl import rows
        c = self.collectors
        try:
            return rows(c["llama_swap"].get(), c["gpu"].get(), c["model_procs"].get(), c["model_desc"].get())
        except Exception:  # noqa: BLE001 - a display nicety
            return []

    @staticmethod
    def _process_rows() -> list:
        """[(pid, name, cpu_seconds, rss_bytes)] for every process. One NtQuerySystemInformation call (~10 ms) where
        possible; psutil's per-process queries cost ~1 s per scan of ~400 processes on Windows, so that is the fallback."""
        from .data.collectors import procsnap
        try:
            return procsnap.snapshot()
        except (OSError, NotImplementedError, ValueError, AttributeError):
            _swallowed("hub._process_rows")
        import psutil
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_times"]):
            try:
                mem, ct = p.info.get("memory_info"), p.info.get("cpu_times")
                rows.append((p.info.get("pid"), p.info.get("name") or "", (ct.user + ct.system) if ct else 0.0,
                             mem.rss if mem else 0))
            except (psutil.Error, KeyError, AttributeError):
                continue
        return rows

    def _top_processes_payload(self) -> dict:
        """Top processes by CPU / memory / GPU. Runs as the `top_procs` collector (once per TTL, shared by every tab), so
        the CPU% baseline in `_proc_cpu_cache` always spans one whole TTL."""
        now = time.monotonic()
        prev_cache = getattr(self, "_proc_cpu_cache", {})   # pid -> (cpu_time, sampled_at)
        procs = []
        new_cache = {}
        gpu_col = (getattr(self, "collectors", None) or {}).get("gpu")
        chips = ((gpu_col.get() if gpu_col else None) or {}).get("pid_chips") or {}

        for pid, name, total_time, rss in self._process_rows():
            if not pid:
                continue
            new_cache[pid] = (total_time, now)
            cpu_pct = 0.0
            if pid in prev_cache:
                prev_time, prev_at = prev_cache[pid]
                dt = now - prev_at
                if dt > 0.1:
                    cpu_pct = max(0.0, ((total_time - prev_time) / dt) * 100.0)
            procs.append({"pid": pid, "name": name or "", "cpu": round(cpu_pct, 1),
                          "mem_mb": round(rss / (1024 * 1024), 1), "chip": chips.get(pid)})

        self._proc_cpu_cache = new_cache
        top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:6]
        top_mem = sorted(procs, key=lambda x: x["mem_mb"], reverse=True)[:6]
        gpu_procs = [p for p in procs if p.get("chip")]
        top_gpu = sorted(gpu_procs, key=lambda x: (x["mem_mb"], x["cpu"]), reverse=True)[:6]
        return {"ok": True, "top_cpu": top_cpu, "top_mem": top_mem, "top_gpu": top_gpu}

    def build_app(self):
        from . import hub_web     # moved out of hub.py (2026-11-22)
        return hub_web.build_app(self)

    def _serve(self) -> None:
        try:
            self.server.run()
        except BaseException as e:  # noqa: BLE001 - e.g. the port is taken: the ticker and tray keep working
            log(f"dashboard server stopped: {type(e).__name__}: {e}")

    def _start_tray(self) -> None:
        from .tray import Tray

        def review_item():
            from . import exam_review
            w = exam_review._watcher
            if w is not None and w.state()["running"]:
                return ("Stop exam review", lambda: self.review_action("stop"), False)
            return ("Exam review: set box & start", lambda: self.review_action("pick"), False)

        def menu():
            ticker_on = bool(self.ticker and not self.ticker.user_hidden)
            return [("Show ticker", self.hide_ticker if ticker_on else self.show_ticker, ticker_on),
                    ("Open dashboard", self.open_dashboard, False),
                    ("Unload AI models", lambda: self.do_action("models_unload_all", ""), False),
                    review_item(),
                    ("Exam HUD (floating)", lambda: self.review_action("hud"), False),
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
                elif name == "review_pick":
                    from . import exam_review
                    from .views.box_picker import pick_box
                    pick_box(self.root, lambda rect: (exam_review.watcher().set_rect(rect, start=True),
                                                      log(f"exam review: box {rect}, watching")))
                elif name == "review_pick_snip":
                    from . import exam_review
                    from .views.box_picker import pick_box
                    def _on_snip(rect):
                        r = exam_review.watcher().add_snip(rect)
                        log(f"exam review: added scroll snip {rect} -> {r.get('why')}")
                        if r.get("ok"):
                            self.cmds.put(("review_snip_added", r.get("count", 2)))
                    pick_box(self.root, _on_snip)
                elif name == "review_snip_added":
                    if self.exam_hud and getattr(self.exam_hud, "snip_btn", None):
                        count = arg or 2
                        self.exam_hud.snip_btn.config(text=f"✓ Stitched ({count} parts)")
                elif name == "review_set_hud":
                    if arg:
                        self._ensure_exam_hud()
                        if self.exam_hud:
                            self.exam_hud.show()
                    else:
                        if self.exam_hud:
                            self.exam_hud.hide()
                elif name == "review_toggle_hud":
                    from . import exam_review
                    w = exam_review.watcher()
                    new_val = not w.state().get("hud_enabled", False)
                    w.set_hud_enabled(new_val)
                    if new_val:
                        self._ensure_exam_hud()
                        if self.exam_hud:
                            self.exam_hud.show()
                    else:
                        if self.exam_hud:
                            self.exam_hud.hide()
                elif name == "review_update_hud":
                    from . import exam_review
                    if exam_review.watcher().state().get("hud_enabled", False):
                        self._ensure_exam_hud()
                        if self.exam_hud:
                            self.exam_hud.update_item(arg, show=True)
                elif name == "exit":
                    self._shutdown(arg)
                    return
        except queue.Empty:
            pass

        now = time.time()
        if now - getattr(self, "_last_overnight_check", 0) >= 2.5:
            self._last_overnight_check = now
            try:
                from .chains import overnight
                overnight.check_and_advance()
                overnight.check_monitor_off_trigger()
            except Exception:
                pass

        self.root.after(250, self._pump)

    def health_report(self) -> dict:
        """The HUD health card (health.py): how runs ended, real errors, samplers that went quiet, its own cost."""
        logs = self._health_logs.get()
        snap = self.collector.get_snapshot()
        sampler = self.collector._sampler
        alive = lambda th: bool(th and th.is_alive())   # noqa: E731
        checks = [("GPU counters", (getattr(sampler, "latest", None) or {}).get("sampled_at"), getattr(sampler, "interval", 2.0),
                   alive(getattr(sampler, "_thread", None))),
                  ("Snapshot", snap.sampled_at or None, self.collector.interval, alive(self.collector._thread))]
        loop = self.guard.loop
        if loop is not None:                              # started only while the dashboard looks at VRAM
            checks.append(("VRAM guard", loop.at, loop.interval, alive(loop._thread)))
        started = None
        try:
            started = json.loads(RUNNING_FILE.read_text(encoding="utf-8")).get("started")
        except (OSError, ValueError, AttributeError):
            pass
        report = {**logs, "stale": health.stale(checks),
                  "broken": sorted(k for k, c in (getattr(self, "collectors", None) or {}).items() if getattr(c, "failed", False)),
                  "up_since": started, "pid": os.getpid(),
                  "cost": {"cpu": snap.self_cpu, "ram_mb": snap.self_ram_mb, "loop_ms": snap.self_latency_ms,
                           "target_cpu": HUD_TARGET_CPU}}
        report["level"] = health.level(report)
        return report

    def _health_alert(self) -> str | None:
        """The ticker's one line when the HUD itself is in trouble (only "bad": a sampler stopped, 3+ crashes today)."""
        r = self.health_report()
        if r["level"] != "bad":
            return None
        stopped = [s["name"] for s in r["stale"] if s["why"] == "stopped"]
        if stopped:
            return f"HUD: {', '.join(stopped)} stopped updating"
        return f"HUD ended without Exit {r['restarts']['day']}x today"

    # ---- dashboard layout presets (layouts.py)
    def layout_state(self) -> dict:
        from . import layouts
        from .views.ticker_base import SLIDE_LABELS
        return {"ok": True, **self._layouts, "widgets": layouts.registry(),
                "slides": [{"tag": tag, "label": label} for tag, label in SLIDE_LABELS]}

    def _ticker_for(self, data: dict, name: str) -> None:
        """Give the ticker the slides preset `name` asks for (sets data["ticker_base"]; the caller saves)."""
        from . import layouts
        current = dict(getattr(self.ticker, "slides_enabled", None) or {})
        slides, data["ticker_base"] = layouts.ticker_changes(data, name, current)
        if slides and self.ticker is not None:
            turn_on = [k for k, v in slides.items() if v and not current.get(k, True)]
            turn_off = [k for k, v in slides.items() if not v and current.get(k, True)]
            for tag in turn_on + turn_off:            # on first: the ticker refuses to switch off its last slide
                self.cmds.put(("slide", (tag, slides[tag])))

    def layout_action(self, body: dict) -> dict:
        import copy
        from . import layouts
        with self._layouts_lock:
            try:
                new, why = layouts.apply_action(self._layouts, body or {})
            except layouts.Bad as e:
                return {"ok": False, "why": str(e)}
            active_changed = new["active"] != self._layouts["active"]
            touched_active = body.get("action") in ("save", "reset") and new["active"] == (body.get("preset") or {}).get("name", body.get("name"))
            if active_changed or touched_active:
                self._ticker_for(new, new["active"])
            self._layouts = layouts.save(copy.deepcopy(new))
        log(why)
        return {**self.layout_state(), "why": why}

    def _auto_layout(self, snap) -> None:
        """A game / Away started or ended: switch the layout (and the ticker's slides) if a preset asks for it."""
        from . import layouts
        cond = layouts.condition(snap)
        if cond == "game" and not self.settings.get("game_auto_layout", False):
            cond = None
        target = self._layout_auto.step(self._layouts, cond)
        if target:
            r = self.layout_action({"action": "activate", "name": target})
            log(f"layout auto-switch -> {target} ({'ok' if r.get('ok') else r.get('why')})")

    def _ai_inbox_notices(self) -> list:
        """One ticker line when a new weekly digest (the AI's, not the machine's) lands; once per digest file.
        Held back during quiet hours (the notifier would drop it), and the first look only records what exists."""
        from datetime import datetime
        from . import digest, notify
        try:
            cols = getattr(self, "collectors", None) or {}
            payload = cols["ai_inbox"].get() if "ai_inbox" in cols else {}
            name = ((payload or {}).get("digest") or {}).get("name")
            if not name:
                return []
            state_file = DATA_DIR / "ai-inbox-state.json"
            state = digest.load_state(state_file)
            if state.get("digest") == name:
                return []
            first = "digest" not in state
            if not first and notify.in_quiet_hours({**notify.DEFAULTS, **(self.settings.get("notify") or {})}.get("quiet", ""), datetime.now()):
                return []
            state["digest"] = name
            digest.save_state(state, state_file)
            if first:
                return []
            d = payload["digest"]
            nxt = len(d.get("next") or [])
            return [notify.Notice("ai_digest", "Your AI wrote your weekly digest",
                                  f"{name}: {nxt} thing{'s' if nxt != 1 else ''} for next week. Open it from the From your AI card.",
                                  "info", f"ai_inbox:{name}")]
        except Exception:                                   # noqa: BLE001 - a ticker nicety never breaks the loop
            _swallowed("hub.ai_inbox_notices")
            return []

    def _weekly_digest(self, snap) -> list:
        """Sunday 20:00 (or the first minute after it, outside quiet hours): write the machine's week (digest.py) and
        return its notice for the notifier, which holds it during a game like any other."""
        from datetime import datetime
        from . import digest, notify
        state = digest.load_state()
        had = state.get("last")
        now = datetime.now()
        end = digest.due(now, state)
        if not had and state.get("last"):
            digest.save_state(state)                      # first start: this week counts as done
        if end is None or notify.in_quiet_hours({**notify.DEFAULTS, **(self.settings.get("notify") or {})}.get("quiet", ""), now):
            return []
        try:
            path = digest.write(digest.build(DATA_DIR / "metrics.sqlite", end, snap.disks), end)
        except OSError as e:
            log(f"digest not written: {type(e).__name__}: {e}")
            return []
        state["last"] = digest.week_name(end)
        digest.save_state(state)
        log(f"digest written: {path}")
        return [notify.Notice("digest", "Your machine's week is ready", f"Digests/{path.name}: "
                              + digest.summary_line(DATA_DIR / "metrics.sqlite", end), "info", f"digest:{state['last']}")]

    def _tick(self) -> None:
        """Every 2 s: set the pace, stop the idle web guard, keep the tray tooltip current."""
        try:
            t = self.ticker
            # the ticker is fully off while a game runs (the game guard's reason), back when it ends
            game_on = snap_game_on(self.collector.get_snapshot())
            game_off = getattr(t, "_game_off", None)       # None: a ticker without the game shutdown (tests' stand-ins)
            if game_off is not None and game_on != game_off:
                t.shut_down_for_game() if game_on else t.come_back_after_game()
            visible = bool(t and not t.user_hidden and not t._is_hidden_for_fullscreen and not getattr(t, "_game_off", False))
            pace = pace_for(visible, time.monotonic() - self.last_web)
            if pace != self.pace:
                self.pace = pace
                self.collector.set_pace(pace)
                log(f"pace {pace:.0f} s ({'someone is looking' if pace == ACTIVE_PACE else 'nobody is looking'})")
            self.guard.stop_if_idle()
            if time.monotonic() - self._last_health >= 30 and getattr(self, "collectors", None):
                self._last_health = time.monotonic()
                self.collector.health_alert = self._health_alert()
            snap = self.collector.get_snapshot()
            self._auto_layout(snap)
            self.history.add(snap)
            if self.metrics:
                self.metrics.add(snap)
                self.metrics.read_logs()
            if self.notifier and snap.sampled_at:
                busy = bool(t and t._is_hidden_for_fullscreen) or (snap.ai_mode == "off" and str(snap.ai_reason or "").startswith("game"))
                extra = []
                if time.monotonic() - self._last_media_check >= MEDIA_CHECK_S and getattr(self, "collectors", None):
                    self._last_media_check = time.monotonic()
                    from .notify import from_media
                    tiles = (self.collectors["media"].get() or {}).get("tiles") or []
                    extra = from_media(tiles, self._media_stuck_seen)
                if time.monotonic() - self._last_deck_check >= 10 and "deck_services" in (getattr(self, "collectors", None) or {}):
                    self._last_deck_check = time.monotonic()
                    extra += self._deck_down_notices()
                if time.monotonic() - self._last_cpu_check >= 10:
                    self._last_cpu_check = time.monotonic()
                    extra += self._cpu_notices(snap)
                if time.monotonic() - self._last_digest_check >= 60:
                    self._last_digest_check = time.monotonic()
                    extra += self._ai_inbox_notices()
                    extra += self._weekly_digest(snap)
                for n in self.notifier.check(self._prev_snap, snap, busy, extra):
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

        Off by default since 2026-10-07: the local-AI watcher (sol-watch.exe, formerly OBVLT's tools/sol-llm-watch.ps1) now heals spills itself (and also runs while
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

    def _ensure_exam_hud(self):
        if getattr(self, "exam_hud", None) is None and getattr(self, "root", None):
            try:
                from .views.exam_hud import ExamHudWindow
                self.exam_hud = ExamHudWindow(
                    self.root,
                    on_retry_gemini=lambda: self.review_action("retry_gemini"),
                    on_add_snip=lambda: self.review_action("snip"),
                    on_pick_snip=lambda: self.review_action("pick_snip"),
                    on_auto_scroll=lambda: self.review_action("auto_scroll"),
                    on_close=lambda: self.review_action("toggle_hud", {"enabled": False}),
                )
            except Exception as e:
                log(f"exam hud init failed: {e}")
                self.exam_hud = None
        return self.exam_hud

    @staticmethod
    def _stop_review() -> None:
        from . import exam_review
        if exam_review._watcher is not None:
            exam_review._watcher.stop()

    def _shutdown(self, why: str) -> None:
        clear_running_marker()                 # a clean exit: the watcher must not bring it back
        log(f"exit: {why}")
        for step in (lambda: self.tray and self.tray.stop(), lambda: setattr(self.server, "should_exit", True),
                     lambda: self.metrics and self.metrics.close(),
                     self._stop_review, self.collector.stop, self.guard.stop,
                     lambda: self.ticker and self.ticker._save_settings(), _save_git_cache, self.root.destroy):
            try:
                step()
            except Exception:  # noqa: BLE001 - leave no matter what
                _swallowed("hub.Hub._shutdown")
        os._exit(0)


def _save_git_cache() -> None:
    from .data.collectors import projects
    projects.save_git_cache()


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


LOG_MAX_BYTES = 1_000_000


def rotate_log(path: Path = None, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """At start, before the log is opened (Windows can't rename a file the running HUD holds open): a hub.log over
    max_bytes becomes hub.log.old (replacing the previous one). True when it rotated. ticker.log does the same."""
    path = path or LOG_FILE
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            path.replace(path.with_suffix(".log.old"))
            return True
    except OSError:
        pass
    return False


def hub_answers(port: int, timeout: float = 1.5) -> bool:
    import httpx
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/snapshot", timeout=timeout).status_code == 200
    except Exception:
        return False


def lock_free(path: Path = None) -> bool:
    """No HUD holds the single-instance lock."""
    try:
        with open(path or LOCK_FILE, "a+b") as f:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return True
    except OSError:
        return False


def ask_exit(port: int) -> bool:
    import httpx
    try:
        return httpx.post(f"http://127.0.0.1:{port}/api/views", json={"exit": True}, headers={"X-SOL-Control": "1"},
                          timeout=5).status_code == 200
    except Exception:
        return False


def start_detached() -> bool:
    """Start the HUD fully detached (no console, not a child of whoever asked), like sol-control.ps1 does."""
    import subprocess
    from .paths import ROOT
    pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    cmd = f'"{pyw}" -m sol_control_hud'
    ps = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{cmd}';"
          f" CurrentDirectory='{ROOT}'}}; exit $r.ReturnValue")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, creationflags=0x08000000)
    return r.returncode == 0


def restart_hub(port: int, wait_s: float = 25.0, answers=None, free=None, exit_=None, start=None,
                sleep=time.sleep, clock=time.monotonic, say=print) -> int:
    """`python -m sol_control_hud --restart`: ask the running HUD to exit cleanly (so it logs "exit" and the health
    card doesn't count a crash), wait until it's gone, start a fresh detached copy, wait until it answers.
    Never kills: a HUD that doesn't answer the exit request is a problem worth seeing, so this stops and says so."""
    answers = answers or (lambda: hub_answers(port))
    free = free or lock_free
    exit_ = exit_ or (lambda: ask_exit(port))
    start = start or start_detached
    if not free() or answers():
        log("restart requested (--restart): asking the running HUD to exit")
        if not exit_():
            say("The running HUD didn't accept the exit request (is it hung?). Nothing was killed: check data/hub.log, "
                "then close it from the tray or Task Manager.")
            return 1
        deadline = clock() + wait_s
        while clock() < deadline and (not free() or answers()):
            sleep(0.5)
        if not free():
            say(f"The HUD was asked to exit but is still running after {wait_s:.0f} s. Nothing was killed.")
            return 1
    if not start():
        say("Could not start the HUD (see data/hub.log).")
        return 1
    deadline = clock() + wait_s
    while clock() < deadline:
        if answers():
            say(f"HUD restarted: http://127.0.0.1:{port}")
            return 0
        sleep(0.5)
    say(f"Started, but it isn't answering yet after {wait_s:.0f} s: check data/hub.log.")
    return 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m sol_control_hud")
    p.add_argument("--show", choices=["ticker", "dashboard", "both", "tray"], help="which views to show this time")
    p.add_argument("--open-dashboard", action="store_true",
                   help="open the dashboard on top of your saved views (the Desktop shortcut 'SOL Dashboard')")
    p.add_argument("--restart", action="store_true",
                   help="ask the running HUD to exit cleanly, then start a fresh one (use this, never kill it)")
    args = p.parse_args(argv)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.restart:
        sys.exit(restart_hub(port_of(load_settings())))
    rotate_log()
    if sys.stdout is None or sys.stderr is None:   # pythonw: uvicorn/print would crash on a None stream
        stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout, sys.stderr = sys.stdout or stream, sys.stderr or stream
    logging.getLogger("asyncio").addFilter(ClosedConnectionFilter())
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
        _swallowed("hub.main")
    show_ticker, show_dashboard = views_for(args.show, settings)
    if args.open_dashboard:
        show_dashboard = True          # your saved ticker choice stays as it was
    Hub(settings, show_ticker, show_dashboard).run()
