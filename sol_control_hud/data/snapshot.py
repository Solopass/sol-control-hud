"""Data snapshot and aggregation layer for SOL Ticker HUD.
Gathers hardware, local AI, active chains, and local services for single-line and multi-line views.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time
from typing import Callable

import httpx
import psutil

from ..away import screen as away_screen
from .collectors import engines, gpu, system, vram
from .collectors.adl import get_gpu_sensors
from .collectors.media import get_media_info, start_media_collector, stop_media_collector
from ..chains.gpulock import is_held

from .snapshot_model import (  # noqa: F401 - moved verbatim; old imports keep working
    AMBER, AWAY_LOG_FILE, CARD_FULL_GB, CHAINS_FILE, CHAINS_LOG_FILE, CHECK_PORTS,
    clean_log_line, CYAN, DISK_TREND_GB, DISK_TREND_WINDOW, GIT_OLD_DAYS, GPU_LOCK_FILE,
    GREEN, HOTSPOT_ALERT_C, joined, MUTED, NIGHT_SHOWN_HOURS, NO_SPILL,
    NO_WINDOW, plain, RED, RUN_RESULT, RUN_WORDS, SEP,
    SERVICE_ORDER, Snapshot, SPILL_WORD, STATE_FILE, TEMP_RISE_C, TEXT,
    VRAM_RISE_GB,
)
from .snapshot_text import (  # noqa: F401 - moved verbatim; old imports keep working
    _part_color, attention, chain_result_color, colorize, disk_color, format_multiline_rows,
    format_rate, format_slides, review_slide, rotation, slide_level,
)
from ..swallow import note as _swallowed


def gpu_lock_held(path: Path | None = None) -> bool:
    """A job holds the GPU lock right now (the lock file itself always exists after first use)."""
    return is_held(path or GPU_LOCK_FILE)


def check_port(port: int, host: str = "127.0.0.1", timeout: float = 0.04) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except (OSError, socket.error):
        return False


def chain_runner_alive(path: Path = CHAINS_FILE, max_age: float = 120.0) -> bool:
    """The chain runner rewrites chains.json every 10 s; older than 2 min = not running."""
    try:
        return time.time() - path.stat().st_mtime < max_age
    except OSError:
        return False


def wsl_running() -> bool:
    """WSL's VM process exists (no wsl.exe call, never starts WSL)."""
    return any((p.info.get("name") or "").lower() in ("vmmemwsl", "vmmem") for p in psutil.process_iter(["name"]))


def probe_services(router_up: bool | None = None) -> dict[str, bool]:
    """router_up: already known from the /models call (skip a second connect to :11440)."""
    out = {name: (router_up if name == "Router" and router_up is not None else check_port(port))
           for name, port in CHECK_PORTS}
    out["Chains"] = chain_runner_alive()
    out["WSL"] = wsl_running()
    return out


def read_llm_state(path: Path = STATE_FILE) -> tuple[str, str | None, str | None]:
    """Reads workstation AI mode ('desk', 'away', 'off'), reason, and until from state.json."""
    if not path.exists():
        return "desk", None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("mode", "desk"), data.get("reason"), data.get("until")
    except (OSError, ValueError):
        return "desk", None, None


def read_chains_state(path: Path = CHAINS_FILE) -> tuple[str | None, str | None, int]:
    """Reads currently running chain, step info, and pending count from chains.json."""
    if not path.exists():
        return None, None, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        running = data.get("running")
        chain_name = None
        step_info = None
        if isinstance(running, dict):
            chain_name = running.get("chain") or running.get("name")
            step = running.get("step")
            total = running.get("steps") or running.get("total_steps")
            step_name = running.get("step_name")
            item = running.get("item")
            items = running.get("items")

            parts = []
            if step is not None and total:
                parts.append(f"Step {step}/{total}")
            elif step is not None:
                parts.append(f"Step {step}")
            if step_name:
                parts.append(step_name)
            if item is not None and items:
                parts.append(f"{item}/{items}")
            if parts:
                step_info = " · ".join(parts)
        elif isinstance(running, str):
            chain_name = running
        pending = data.get("pending", [])
        pending_count = len(pending) if isinstance(pending, list) else int(data.get("runnable_now", 0))
        return chain_name, step_info, pending_count
    except (OSError, ValueError):
        return None, None, 0


def read_last_finished_chain(path: Path = CHAINS_LOG_FILE) -> str | None:
    """Reads the last completed, drafted, or errored chain from chains.log tail."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for raw_line in reversed(lines):
            line = clean_log_line(raw_line)
            if not line:
                continue
            # the chain runner's own result line: "Code review.md: run 15 cancelled: cancelled by user"
            m = RUN_RESULT.match(line)
            if m:
                return f"{m['name'].removesuffix(' request')} ({RUN_WORDS[m['status']]})"
            if any(k in line for k in (": drafted ", ": finished", ": done", "finished in ", " completed", ": error", ": can't run:", ": couldn't draft:", " failed")):
                if ": drafted " in line:
                    name = line.split(": drafted ")[0].removesuffix(".md").removesuffix(" request")
                    return f"{name} (drafted)"
                elif ": finished" in line:
                    name = line.split(": finished")[0].removesuffix(".md").removesuffix(" request")
                    return f"{name} (finished)"
                elif " completed" in line:
                    name = line.split(" completed")[0].removesuffix(".md").removesuffix(" request")
                    if ": run " in name:
                        name = name.split(": run ")[0]
                    return f"{name} (completed)"
                elif any(err in line for err in (": error", ": can't run:", ": couldn't draft:", " failed")):
                    for err_key in (": error", ": can't run:", ": couldn't draft:", " failed"):
                        if err_key in line:
                            name = line.split(err_key)[0].removesuffix(".md").removesuffix(" request")
                            return f"{name} (failed)"
                return line[:40]
    except Exception:
        _swallowed("snapshot.read_last_finished_chain")
    return None


_ROUTER: httpx.Client | None = None


def read_ai_models(timeout: float = 0.5) -> tuple[str | None, str, bool, int | None]:
    """Asks the router (:11440/models, answered by the router itself) which model is loaded or sleeping.
    Never asks a model (/slots?model=...): that counts as using it and stops its idle unload, so polling it every 2 s
    kept models loaded for good (seen 09-25 with a chain; the ticker did the same). `generating` comes from the GPU
    instead (ai_busy); the third value stays for callers and is always False here."""
    global _ROUTER
    try:
        # one client for the ticker's life: building a new one (SSL context and all) cost ~5 ms CPU every 2 s
        if _ROUTER is None:
            _ROUTER = httpx.Client(timeout=timeout)
        resp = _ROUTER.get("http://127.0.0.1:11440/models")
        if resp.status_code != 200:
            return None, "offline", False, None
        for m in resp.json().get("data", []):
            status_dict = m.get("status") if isinstance(m, dict) else None
            if isinstance(status_dict, dict) and status_dict.get("value") in ("loaded", "sleeping", "loading"):
                return m.get("id"), status_dict["value"], False, (m.get("meta") or {}).get("n_ctx")
        return None, "unloaded", False, None   # the router answers, nothing is loaded
    except Exception:
        return None, "offline", False, None


def ai_busy(ai_state: str, gpu_load: float | None, processes: list[dict]) -> bool:
    """A loaded model is generating: llama-server holds VRAM and the GPU is busy. (Games turn the AI off first.)"""
    if ai_state != "loaded" or gpu_load is None or gpu_load < 50:
        return False
    return any(p.get("name", "").lower() == "llama-server" and p.get("dedicated_gb", 0) >= 1.0 for p in processes)


def read_state(path: Path = STATE_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def away_progress(state: dict, eta: "away_screen.EtaTracker", now: float) -> dict:
    """What Away is doing, in the Away screen's own words (progress.json, chains.json, the queue)."""
    if state.get("mode") != "away":
        eta.update(None, None, None, now)
        return {}
    v = away_screen.build_view(state, away_screen.read_json(away_screen.PROGRESS_F), away_screen.read_json(away_screen.CHAINS_F),
                               [], None, False, eta, now)
    line = v.line
    if v.jobs.startswith("job "):
        line += f"  ({v.jobs.split('  ·  ')[0]})"
    return {"away_line": line, "away_fraction": v.fraction, "away_eta": v.eta, "away_phase": v.phase,
            "away_present": bool(state.get("present"))}


def read_chains_extra(path: Path = CHAINS_FILE, now: datetime | None = None) -> tuple[list[dict], str | None]:
    """Why queued chains wait (chains.json pending[].blocked) and the next scheduled run ("Weekly digest Sun 20:00")."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    pending = [{"chain": p.get("chain"), "blocked": p.get("blocked")} for p in data.get("pending") or [] if isinstance(p, dict)]
    nxt = None
    for s in data.get("scheduled") or []:
        if isinstance(s, dict) and s.get("next") and s["next"] != "on away":
            try:
                at = datetime.strptime(s["next"], "%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            today = (now or datetime.now()).date()
            day = "today" if at.date() == today else "tomorrow" if at.date() == today + timedelta(days=1) else at.strftime("%a")
            nxt = f"{s.get('chain')} {day} {at:%H:%M}"
            break
    return pending, nxt


def disk_trends_from(history: list[tuple[float, dict[str, float]]], now: float,
                     window: float = DISK_TREND_WINDOW, min_age: float = 600.0) -> dict[str, float]:
    """Free space now minus free space at the oldest sample in the window (only when that sample is >= 10 min old)."""
    if not history:
        return {}
    t_now, free_now = history[-1]
    old = next(((t, f) for t, f in history if now - t <= window), None)
    if not old or t_now - old[0] < min_age:
        return {}
    return {d: round(free_now[d] - old[1][d], 1) for d in free_now if d in old[1]}


def rise(samples: list[tuple[float, float | None]], now: float, window: float) -> float | None:
    """The latest value minus the lowest one inside the window (None until there's more than one sample)."""
    vals = [v for t, v in samples if now - t <= window and v is not None]
    return round(vals[-1] - min(vals), 1) if len(vals) >= 2 else None


def dirty_age_days(repo: Path, porcelain: str, now: float | None = None) -> float | None:
    """How old the oldest uncommitted change is (the modified time of the oldest changed file that still exists)."""
    now = now or time.time()
    oldest = None
    for line in porcelain.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        try:
            m = (repo / path).stat().st_mtime
        except OSError:
            continue
        oldest = m if oldest is None else min(oldest, m)
    return round((now - oldest) / 86400, 1) if oldest else None


def night_summary(away_log: Path = AWAY_LOG_FILE, chains_log: Path = CHAINS_LOG_FILE, now: float | None = None) -> dict | None:
    """The last finished Away session (>= 30 min, ended < 18 h ago): jobs done / failed, chain results, how it ended."""
    now = now or time.time()
    try:
        events = [json.loads(line) for line in away_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None
    ends = [i for i, e in enumerate(events) if e.get("event") == "away-end"]
    if not ends:
        return None
    end_i = ends[-1]
    starts = [i for i in range(end_i) if events[i].get("event") == "away-start"]
    if not starts:
        return None
    start, end = events[starts[-1]], events[end_i]
    try:
        t0, t1 = datetime.fromisoformat(start["time"]), datetime.fromisoformat(end["time"])
    except (KeyError, ValueError):
        return None
    if now - t1.timestamp() > NIGHT_SHOWN_HOURS * 3600 or (t1 - t0).total_seconds() < 1800:
        return None
    status: dict[str, str] = {}
    for e in events[starts[-1]:end_i]:
        if e.get("event") in ("job-done", "job-failed") and e.get("job"):
            status[e["job"]] = e["event"]              # the last word per job counts (a requeued job may finish later)
    chains = []
    try:
        for line in chains_log.read_text(encoding="utf-8", errors="replace").splitlines():
            stamp = line[:19]
            if len(stamp) == 19 and t0.isoformat() <= stamp <= t1.isoformat():
                m = RUN_RESULT.match(clean_log_line(line))
                if m:
                    chains.append(f"{m['name'].removesuffix(' request')} ({RUN_WORDS[m['status']]})")
    except OSError:
        pass
    return {"start": f"{t0:%H:%M}", "end": f"{t1:%H:%M}", "end_iso": end["time"],
            "done": sum(v == "job-done" for v in status.values()),
            "failed": [j for j, v in status.items() if v == "job-failed"],
            "chains": chains[-3:], "reason": end.get("reason") or "?"}


def vram_holders(guard_res: dict) -> list[dict]:
    """Who holds the card, biggest first, from the VRAM guard's per-app numbers (they add up to the adapter total;
    the raw per-process counters re-count shared surfaces) plus the AI runner."""
    ai = guard_res.get("ai") or {}
    parts = [{"name": t["label"], "dedicated_gb": t["gb"], "shared_gb": 0.0} for t in guard_res.get("top_consumers") or []]
    if (ai.get("dedicated_gb") or 0) >= 0.05:
        parts.append({"name": "llama-server", "dedicated_gb": ai["dedicated_gb"], "shared_gb": ai.get("shared_gb", 0.0)})
    return sorted(parts, key=lambda p: p["dedicated_gb"], reverse=True)


def spill_impact(evicted: bool, mark, speed: dict) -> tuple[str, object]:
    """Does the spill actually slow answers? The shared-memory counter only says some of the model sits in system RAM;
    10-06 it said so every ~35 min all day with nothing answering, and 10-08 sol-fast held 0.62 GB there at full speed
    (68-82 tok/s). So judge by the first real answer after the spill began. `mark` is the newest answer's id when it
    began (NO_SPILL while not evicted); returns (impact, new mark)."""
    if not evicted:
        return "", NO_SPILL
    current = speed.get("answer_id") if speed.get("current_process") else None
    if mark is NO_SPILL:
        mark = current                  # answers from before the spill don't count
    if current is None or current == mark:
        return "unknown", mark
    return ("slow" if speed.get("slow") else "fine"), mark


def count_note_words(text: str) -> int:
    """Counts words in a markdown note, stripping YAML frontmatter and header metadata."""
    lines = text.splitlines()
    body_lines = []
    in_yaml = False
    started_body = False
    if lines and lines[0].strip() == "---":
        in_yaml = True
        lines = lines[1:]
    for line in lines:
        if in_yaml:
            if line.strip() == "---":
                in_yaml = False
            continue
        if not started_body:
            stripped = line.strip()
            if not stripped:
                started_body = True
                continue
            if ":" in stripped and not stripped.startswith("#"):
                parts = stripped.split(":", 1)
                if len(parts[0].split()) == 1:
                    continue
            started_body = True
        body_lines.append(line)
    return len(" ".join(body_lines).split())


def read_daily_note_status(vault_dir: Path | None = None) -> tuple[bool, int, str | None, Path]:
    """Checks today's daily note in Polymatica Vault (D:\\Polymatica Vault\\YYYY\\YYYY-MM-DD.md).
    If today's note does not exist yet, checks yesterday's note to provide context.
    Returns (exists, word_count, last_modified_time_str, path).
    """
    vault_dir = vault_dir or Path(r"D:\Polymatica Vault")
    now = time.localtime()
    year_str = time.strftime("%Y", now)
    date_str = time.strftime("%Y-%m-%d", now)
    note_path = vault_dir / year_str / f"{date_str}.md"
    if not note_path.exists():
        # Check yesterday's note for fallback context
        y_now = time.localtime(time.time() - 86400)
        y_year = time.strftime("%Y", y_now)
        y_date = time.strftime("%Y-%m-%d", y_now)
        y_path = vault_dir / y_year / f"{y_date}.md"
        y_words = 0
        if y_path.exists():
            try:
                y_words = count_note_words(y_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                _swallowed("snapshot.read_daily_note_status")
        return False, y_words, None, note_path
    try:
        st = note_path.stat()
        mod_time_str = time.strftime("%H:%M", time.localtime(st.st_mtime))
        text = note_path.read_text(encoding="utf-8", errors="replace")
        words = count_note_words(text)
        return True, words, mod_time_str, note_path
    except Exception:
        return True, 0, None, note_path


def check_workspace_git(workspace_dir: Path | None = None) -> tuple[int, list[str], int]:
    """Scans repositories in D:\\Workspace for uncommitted / dirty files.
    Returns (dirty_count, dirty_repos, total_repos).
    """
    workspace_dir = workspace_dir or Path(r"D:\Workspace")
    if not workspace_dir.exists():
        return 0, [], 0
    dirty_repos: list[str] = []
    total = 0
    try:
        entries = sorted(
            [d for d in workspace_dir.iterdir() if d.is_dir() and not d.name.startswith(".") and (d / ".git").exists()],
            key=lambda p: p.name.lower()
        )
        total = len(entries)
        for d in entries:
            try:
                # --no-optional-locks: a plain `git status` may take .git/index.lock to refresh the index, and then a
                # commit you (or Gemini) make at that moment fails with "index.lock exists"
                res = subprocess.run(
                    ["git", "--no-optional-locks", "-C", str(d), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=NO_WINDOW,
                )
                if res.returncode == 0 and res.stdout.strip():
                    dirty_repos.append(d.name)
            except Exception:
                _swallowed("snapshot.check_workspace_git")
    except Exception:
        _swallowed("snapshot.check_workspace_git")
    return len(dirty_repos), dirty_repos, total


def _review_state() -> dict | None:
    try:
        from ..exam_review import ticker_state
        return ticker_state()
    except Exception:  # noqa: BLE001 - a display nicety
        return None


class TickerCollector:
    """Gathers data asynchronously and provides the latest Snapshot without blocking UI."""

    def __init__(self, sampler: gpu.GpuSampler | None = None, guard: vram.VramGuard | None = None):
        self._sampler = sampler or gpu.GpuSampler(interval=2.0)
        self.interval = 2.0
        self._guard = guard or vram.VramGuard()
        self._latest = Snapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_stability = 0.0
        self._stability_data = {}
        self._last_system = 0.0
        self._disks_data: list[dict] = []
        self._backups_data: dict = {}
        self._last_git = 0.0
        self._git_data: tuple[int, list[str], int] = (0, [], 0)
        self._git_thread: threading.Thread | None = None
        self._last_apps = 0.0
        self._apps: dict = {"apps": [], "media": []}
        self._last_speed = 0.0
        self._spill_mark = NO_SPILL
        self.health_alert: str | None = None
        from .collectors.cpu import Utility
        self._cpu_utility = Utility()   # set by the hub from health.py, copied into each snapshot
        self._speed: dict = {}
        self._apps_thread: threading.Thread | None = None
        self._eta = away_screen.EtaTracker()
        self._disk_hist: list[tuple[float, dict[str, float]]] = []
        self._recent: list[tuple[float, float | None, float | None]] = []   # (t, vram GB, edge temp) for 10 min
        self._git_ages: dict[str, float] = {}
        self._night: dict | None = None
        self._last_net_bytes: tuple[int, int] | None = None
        self._last_net_time: float = 0.0
        self._net_down_kb: float = 0.0
        self._net_up_kb: float = 0.0
        self.stability_interval: float = 300.0
        self.system_interval: float = 60.0
        self.git_interval: float = 60.0
        self.monitor_self: bool = True
        self._last_self_check: float = 0.0
        self._self_cpu: float = 0.0
        self._self_ram_mb: float = 0.0
        self._proc: psutil.Process | None = None
        self._gpu_history: list[float] = []
        self._vram_history: list[float] = []
        self._cpu_history: list[float] = []

    def set_intervals(self, stability: float | None = None, system: float | None = None,
                      git: float | None = None, monitor_self: bool | None = None) -> None:
        """Sets throttled data intervals dynamically without stopping the collector."""
        if stability is not None:
            self.stability_interval = max(10.0, float(stability))
        if system is not None:
            self.system_interval = max(5.0, float(system))
        if git is not None:
            self.git_interval = max(5.0, float(git))
        if monitor_self is not None:
            self.monitor_self = bool(monitor_self)

    def start(self) -> None:
        if hasattr(self._sampler, "start"):
            self._sampler.start()
        start_media_collector()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ticker-collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if hasattr(self._sampler, "stop"):
            self._sampler.stop()
        stop_media_collector()

    def set_pace(self, seconds: float) -> None:
        """How often to collect (and sample the GPU): 2 s while a view shows it, slower while nobody looks."""
        self.interval = seconds
        if hasattr(self._sampler, "interval"):
            self._sampler.interval = seconds

    def get_snapshot(self) -> Snapshot:
        with self._lock:
            return self._latest

    def invalidate_cache(self) -> None:
        """Forces throttled data (stability, disks, git) to refresh immediately on next poll."""
        self._last_stability = 0.0
        self._last_system = 0.0
        self._last_git = 0.0
        self._last_apps = 0.0

    def collect_once(self) -> Snapshot:
        t_gather_start = time.perf_counter()
        # 1. System Memory & CPU
        mem = system.memory()
        cpu = psutil.cpu_percent(interval=None)
        u = self._cpu_utility.read()                     # Task Manager's number when Windows has it (cpu.Utility)
        if u:
            cpu = u[0]

        # 2. GPU & VRAM
        gpu_latest = getattr(self._sampler, "latest", {})
        gpu_avail = gpu_latest.get("available", False)
        gpu_load = gpu_latest.get("load_percent") if gpu_avail else None
        vram_used = gpu_latest.get("vram_used_gb") if gpu_avail else None
        vram_total = gpu_latest.get("vram_total_gb") if gpu_avail else 16.0
        gpu_name = gpu_latest.get("name") if gpu_avail else None

        # VRAM eviction check & top process extraction
        guard_res = self._guard.update(gpu_latest, {}) if gpu_avail else {}
        vram_evicted = guard_res.get("verdict") == "EVICTED"
        vram_tight = guard_res.get("verdict") == "TIGHT"
        vram_top_process = None
        vram_processes = []
        if gpu_avail and guard_res.get("available"):
            # Who holds the card, from the guard's per-app numbers. The raw per-process counters re-count shared
            # surfaces: on 10-08 the ticker said "VRAM 4.3/16G (dwm 10.5G)". The guard keeps each app's own number and
            # gives the desktop (dwm) only what's left of the adapter total, so these add up to what's really used.
            procs = vram_holders(guard_res)
            if procs:
                vram_processes = procs[:3]
                top_p = procs[0]
                if top_p.get("dedicated_gb", 0.0) >= 0.1:
                    vram_top_process = f"{top_p['name']} {top_p['dedicated_gb']:.1f}G"

        # 2a. Is the card nearly full, and who holds most of it (the guard's per-app numbers: dwm gets the remainder)
        vram_card_full = bool(vram_used is not None and vram_used >= CARD_FULL_GB)
        hogs = [t for t in (guard_res.get("top_consumers") or [])
                if t.get("movable") and not str(t.get("name", "")).lower().startswith(("llama-server", "ollama"))]
        vram_hog = f"{hogs[0]['label']} {hogs[0]['gb']:.1f} GB" if hogs and hogs[0].get("gb", 0) >= 0.2 else None

        # 2a'. The last answer's real speed (every 10 s: one log tail + a llama-server process scan)
        if time.monotonic() - self._last_speed >= 10:
            self._last_speed = time.monotonic()
            try:
                from .collectors import speed as speed_mod
                from .. import models_ctl
                self._speed = speed_mod.answer_speed(models_ctl.model_processes())
            except Exception:
                self._speed = {}
        vram_spill_impact, self._spill_mark = spill_impact(vram_evicted, self._spill_mark, self._speed)
        sp = self._speed if self._speed.get("current_process") and time.time() - (self._speed.get("at") or 0) <= 1800 else {}

        # 2b. GPU Sensors (ADL: Edge/Hotspot/Mem Temp, Fan RPM)
        try:
            sensors = get_gpu_sensors()
            gpu_temp = sensors.temp_edge
            gpu_hotspot = sensors.temp_hotspot
            gpu_mem_temp = sensors.temp_mem
            gpu_fan_rpm = sensors.fan_rpm
        except Exception:
            gpu_temp = gpu_hotspot = gpu_mem_temp = gpu_fan_rpm = None

        # 2c. Short trends (VRAM over 10 min, temperature over 5 min)
        t_now = time.time()
        self._recent = [r for r in self._recent if t_now - r[0] <= 600] + [(t_now, vram_used, gpu_temp)]
        vram_rise = rise([(r[0], r[1]) for r in self._recent], t_now, 600)
        temp_rise = rise([(r[0], r[2]) for r in self._recent], t_now, 300)

        # 3. Local AI
        ai_model, ai_state, _, ai_ctx_size = read_ai_models(timeout=0.4)
        ai_generating = ai_busy(ai_state, gpu_load, gpu_latest.get("processes", []) if gpu_avail else [])
        ai_mode, ai_reason, ai_until = read_llm_state()
        gpu_locked = gpu_lock_held()
        try:
            away = away_progress(read_state(), self._eta, time.time())
        except Exception:
            away = {}

        # 4. Chains
        chain_name, chain_step, pending_count = read_chains_state()
        chain_last_finished = None
        if not chain_name:
            chain_last_finished = read_last_finished_chain()

        # 5. Stability (cached every stability_interval: Get-WinEvent is expensive)
        now = time.time()
        if now - self._last_stability >= self.stability_interval:
            try:
                self._stability_data = system.stability()
            except Exception:
                _swallowed("snapshot.TickerCollector.collect_once")
            self._last_stability = now
        gpu_resets = self._stability_data.get("gpu_driver_resets", 0)
        whea_errors = self._stability_data.get("whea", 0)
        unexpected_reboots = self._stability_data.get("unexpected_reboots", 0)

        # 6. Disks & Backups (cached every system_interval), a free-space history for the red/green trend, the last Away session
        if now - self._last_system >= self.system_interval or not self._disks_data:
            try:
                self._disks_data = system.disks()
                self._backups_data = system.backups()
                self._disk_hist.append((now, {d["drive"]: float(d["free_gb"]) for d in self._disks_data
                                              if "drive" in d and "free_gb" in d}))
                self._disk_hist = [h for h in self._disk_hist if now - h[0] <= DISK_TREND_WINDOW + 600]
            except Exception:
                _swallowed("snapshot.TickerCollector.collect_once")
            try:
                self._night = night_summary(now=now)
            except Exception:
                self._night = None
            self._last_system = now
        disk_trends = disk_trends_from(self._disk_hist, now)
        chain_pending, chain_next = read_chains_extra()

        # 7. Workspace Git (every git_interval, in its own thread: 13 repos can take seconds and must not freeze the other stats)
        if now - self._last_git >= self.git_interval and not (self._git_thread and self._git_thread.is_alive()):
            self._last_git = now

            def scan():
                try:
                    self._git_data = check_workspace_git()
                    ws, ages = Path(r"D:\Workspace"), {}
                    for name in self._git_data[1]:
                        res = subprocess.run(["git", "--no-optional-locks", "-C", str(ws / name), "status", "--porcelain"],
                                             capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
                        age = dirty_age_days(ws / name, res.stdout)
                        if age is not None:
                            ages[name] = age
                    self._git_ages = ages
                except Exception:
                    _swallowed("snapshot.TickerCollector.collect_once.scan")
            self._git_thread = threading.Thread(target=scan, name="ticker-git", daemon=True)
            self._git_thread.start()
        git_dirty_count, git_dirty_repos, git_total_repos = self._git_data

        # 7b. What runs from the Projects / Media cards (every system_interval, in its own thread: one listener scan and,
        # while WSL already runs, one systemd probe; never a request to a service, so it can't wake one)
        if now - self._last_apps >= self.system_interval and not (self._apps_thread and self._apps_thread.is_alive()):
            self._last_apps = now

            def scan_apps():
                try:
                    from .collectors.media_apis import launchpad_summary
                    self._apps = launchpad_summary(wsl_running())
                except Exception:
                    _swallowed("snapshot.TickerCollector.collect_once.scan_apps")
            self._apps_thread = threading.Thread(target=scan_apps, name="ticker-apps", daemon=True)
            self._apps_thread.start()

        # 8. Daily Note
        note_exists, note_words, note_time, note_path = read_daily_note_status()

        # 9. Network Throughput
        try:
            counters = psutil.net_io_counters()
            if self._last_net_bytes is not None:
                dt = now - self._last_net_time
                if 0.5 <= dt <= 15.0:   # 15: the slow pace (10 s) must still give a rate; longer = asleep
                    self._net_down_kb = max(0.0, (counters.bytes_recv - self._last_net_bytes[1]) / 1024.0 / dt)
                    self._net_up_kb = max(0.0, (counters.bytes_sent - self._last_net_bytes[0]) / 1024.0 / dt)
            self._last_net_bytes = (counters.bytes_sent, counters.bytes_recv)
            self._last_net_time = now
        except Exception:
            _swallowed("snapshot.TickerCollector.collect_once")

        # 10. Services
        services = probe_services(router_up=ai_state != "offline")

        # 11. Media Playback (Windows GSMTC)
        try:
            m = get_media_info()
            media_status = m.status
            media_title = m.title
            media_artist = m.artist
            media_app = m.clean_app
            media_playing = m.playing
        except Exception:
            media_status = "none"
            media_title = media_artist = media_app = None
            media_playing = False

        # 12. Self-Resource Overhead
        if self.monitor_self:
            if now - self._last_self_check >= 4.0 or self._last_self_check == 0.0:
                try:
                    if self._proc is None:
                        self._proc = psutil.Process()
                    self._self_cpu = self._proc.cpu_percent(interval=None)
                    self._self_ram_mb = self._proc.memory_info().rss / (1024.0 * 1024.0)
                except Exception:
                    _swallowed("snapshot.TickerCollector.collect_once")
                self._last_self_check = now
        else:
            self._self_cpu = 0.0
            self._self_ram_mb = 0.0
        t_latency_ms = (time.perf_counter() - t_gather_start) * 1000.0

        # Rolling metric history (up to 40 samples, ~60-80s on 2s pace)
        if gpu_load is not None:
            self._gpu_history = (self._gpu_history + [float(gpu_load)])[-40:]
        if vram_used is not None:
            self._vram_history = (self._vram_history + [float(vram_used)])[-40:]
        self._cpu_history = (self._cpu_history + [float(cpu)])[-40:]

        # Segmented VRAM calculation
        v_tot = float(vram_total or 16.0)
        v_used = float(vram_used or 0.0)
        v_model = 0.0
        if gpu_avail:
            for p in gpu_latest.get("processes", []):
                pname = (p.get("name") or "").lower()
                if any(k in pname for k in ("llama", "ollama", "sol-", "python")):
                    v_model += float(p.get("dedicated_gb", 0.0))
        v_model = min(v_model, v_used)
        v_other = max(0.0, v_used - v_model)
        v_free = max(0.0, v_tot - v_used)

        snap = Snapshot(
            gpu_name=gpu_name,
            gpu_load=gpu_load,
            gpu_temp=gpu_temp,
            gpu_hotspot=gpu_hotspot,
            gpu_mem_temp=gpu_mem_temp,
            gpu_fan_rpm=gpu_fan_rpm,
            vram_used_gb=vram_used,
            vram_total_gb=vram_total,
            vram_evicted=vram_evicted,
            vram_spill_impact=vram_spill_impact,
            hud_alert=self.health_alert,
            vram_spilled_gb=round((guard_res.get("ai") or {}).get("shared_gb", 0.0), 2),
            vram_tight=vram_tight,
            vram_top_process=vram_top_process,
            vram_card_full=vram_card_full,
            vram_hog=vram_hog,
            ai_tps=sp.get("tps"),
            ai_tps_slow=bool(sp.get("slow")),
            vram_processes=vram_processes,
            ram_used_gb=mem.get("used_gb", 0.0),
            ram_total_gb=mem.get("total_gb", 0.0),
            ram_percent=mem.get("percent", 0.0),
            cpu_percent=cpu,
            ai_model=ai_model,
            ai_state=ai_state,
            ai_mode=ai_mode,
            ai_reason=ai_reason,
            ai_until=ai_until,
            gpu_locked=gpu_locked,
            ai_generating=ai_generating,
            ai_ctx_size=ai_ctx_size,
            **away,
            chain_running=chain_name,
            chain_step=chain_step,
            chain_pending_count=pending_count,
            chain_last_finished=chain_last_finished,
            chain_pending=chain_pending,
            chain_next=chain_next,
            night=self._night,
            review=_review_state(),
            disk_trends=disk_trends,
            vram_rise_gb=vram_rise,
            temp_rise_c=temp_rise,
            git_dirty_days={k: v for k, v in self._git_ages.items() if k in git_dirty_repos},
            gpu_resets=gpu_resets,
            whea_errors=whea_errors,
            unexpected_reboots=unexpected_reboots,
            disks=self._disks_data,
            backup_latest=self._backups_data.get("latest"),
            backup_age_h=self._backups_data.get("age_hours"),
            backup_stale=self._backups_data.get("stale", False),
            note_exists=note_exists,
            note_words=note_words,
            note_time=note_time,
            note_path=str(note_path) if note_path else None,
            git_dirty_count=git_dirty_count,
            git_dirty_repos=git_dirty_repos,
            apps_running=list(self._apps.get("apps") or []),
            media_running=list(self._apps.get("media") or []),
            git_total_repos=git_total_repos,
            net_down_kb=self._net_down_kb,
            net_up_kb=self._net_up_kb,
            media_status=media_status,
            media_title=media_title,
            media_artist=media_artist,
            media_app=media_app,
            media_playing=media_playing,
            services=services,
            sampled_at=time.time(),
            self_cpu=self._self_cpu,
            self_ram_mb=self._self_ram_mb,
            self_latency_ms=round(t_latency_ms, 1),
            gpu_history=list(self._gpu_history),
            vram_history=list(self._vram_history),
            cpu_history=list(self._cpu_history),
            vram_model_gb=round(v_model, 2),
            vram_other_gb=round(v_other, 2),
            vram_free_gb=round(v_free, 2),
        )
        with self._lock:
            self._latest = snap
        return snap

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect_once()
            except Exception:
                _swallowed("snapshot.TickerCollector._run")
            if self._stop.wait(self.interval):   # the hub slows this down while nobody is looking
                break
