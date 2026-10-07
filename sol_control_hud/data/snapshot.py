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

# RDNA4 runs its hotspot in the high 80s/low 90s under a long AI job; 85 alarmed on every run (09-26)
HOTSPOT_ALERT_C = 95
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# What the colors mean, everywhere in the ticker (the renderer maps them to the theme's own shades):
#   RED    a problem, or getting worse fast (a disk filling up, a crash, the model spilling out of VRAM, hot)
#   AMBER  worth a look (VRAM tight, uncommitted work, a backup getting old, something waiting)
#   GREEN  good or getting better (space freed, a job done, the daily note past 250 words)
#   CYAN   working right now (a chain or Away job running, the network busy)
#   MUTED  idle / nothing to see;  TEXT  plain values
CYAN, GREEN, AMBER, RED, MUTED, TEXT = "#38bdf8", "#4ade80", "#fbbf24", "#f87171", "#94a3b8", "#e2e8f0"
SEP = ("  ·  ", MUTED)
DISK_TREND_GB = 1.0          # this much more / less free space within the last hour colors a drive red / green
DISK_TREND_WINDOW = 3600.0
AWAY_LOG_FILE = Path(r"D:\AI\Cache\llm\away.jsonl")
NIGHT_SHOWN_HOURS = 18       # the "last Away session" summary shows this long after it ended (until you click it)


def plain(segments: list[tuple[str, str]]) -> str:
    return "".join(t for t, _ in segments)


def joined(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Segments separated by a muted ' · '."""
    out: list[tuple[str, str]] = []
    for p in parts:
        if out:
            out.append(SEP)
        out.append(p)
    return out


def gpu_lock_held(path: Path | None = None) -> bool:
    """A job holds the GPU lock right now (the lock file itself always exists after first use)."""
    return is_held(path or GPU_LOCK_FILE)

STATE_FILE = Path(r"D:\AI\Cache\llm\state.json")
CHAINS_FILE = Path(r"D:\AI\Cache\llm\chains.json")
CHAINS_LOG_FILE = Path(r"D:\AI\Cache\llm\chains.log")
GPU_LOCK_FILE = Path(r"D:\AI\Cache\llm\gpu.lock")

# Only Windows-side services are probed. media-api / speedman / voice-savior / omni-tools run in WSL behind systemd
# socket activation: a TCP connect *starts* them, so probing every 2 s would keep them loaded (RAM/VRAM) for good.
CHECK_PORTS = [
    ("Router", 11440),
    ("Embed", 11443),
    ("HUD", 7900),
]
SERVICE_ORDER = ["Router", "Embed", "HUD", "Chains", "WSL"]


@dataclass
class Snapshot:
    # Hardware
    gpu_name: str | None = None
    gpu_load: float | None = None
    gpu_temp: int | None = None
    gpu_hotspot: int | None = None
    gpu_mem_temp: int | None = None
    gpu_fan_rpm: int | None = None
    vram_used_gb: float | None = None
    vram_total_gb: float | None = None
    vram_evicted: bool = False
    vram_tight: bool = False
    vram_top_process: str | None = None
    vram_processes: list[dict] = field(default_factory=list)
    vram_card_full: bool = False               # in use >= CARD_FULL_GB: Windows starts squeezing the model past ~12.2
    vram_hog: str | None = None                # the app holding the most of the card, e.g. "brave 1.4 GB"
    ai_tps: float | None = None                # the last answer's generation speed (router.log), if from the loaded model
    ai_tps_slow: bool = False                  # under half its usual speed (speed.USUAL_TPS)
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_percent: float = 0.0
    cpu_percent: float = 0.0

    # Local AI
    ai_model: str | None = None
    ai_state: str = "offline"  # loaded, sleeping, unloaded, offline
    ai_mode: str = "desk"      # desk, away, off, unknown
    ai_reason: str | None = None
    ai_until: str | None = None
    gpu_locked: bool = False
    ai_generating: bool = False
    ai_ctx_size: int | None = None

    # Away mode progress (same wording as the Away screen: sol_control_hud.away.screen.build_view)
    away_line: str | None = None       # "hard-evals — gpt-oss-120b: parser test, run 2 of 3"
    away_fraction: float | None = None
    away_eta: str = ""                 # "about 18 min left · done around 22:10"
    away_present: bool = False         # you came back; the AI keeps working (corner panel)
    away_phase: str | None = None      # working | done | stopped | ...

    # Chains
    chain_running: str | None = None
    chain_step: str | None = None
    chain_pending_count: int = 0
    chain_last_finished: str | None = None
    chain_pending: list[dict] = field(default_factory=list)   # [{"chain", "blocked"}] from chains.json
    chain_next: str | None = None                              # "Weekly digest Sun 20:00"
    night: dict | None = None                                  # the last Away session, see night_summary()
    review: dict | None = None                                 # the exam review helper, see exam_review.ticker_state()
    disk_trends: dict[str, float] = field(default_factory=dict)   # drive -> GB of free space gained (+) / lost (-)
    vram_rise_gb: float | None = None      # VRAM now minus the lowest of the last 10 min
    temp_rise_c: float | None = None       # GPU edge temperature now minus the lowest of the last 5 min
    git_dirty_days: dict[str, float] = field(default_factory=dict)   # repo -> age of its oldest uncommitted change

    # Stability (since the baseline or the last acknowledgement: reports\stability-ack.json)
    gpu_resets: int = 0
    whea_errors: int = 0
    unexpected_reboots: int = 0   # the real signal on this PC: every crash since 09-14 was a reboot (41), not a 4101

    # Storage & Backups
    disks: list[dict] = field(default_factory=list)
    backup_latest: str | None = None
    backup_age_h: float | None = None
    backup_stale: bool = False

    # Workspace & Knowledge Feeds
    note_exists: bool = False
    note_words: int = 0
    note_time: str | None = None
    note_path: str | None = None
    git_dirty_count: int = 0
    git_dirty_repos: list[str] = field(default_factory=list)
    apps_running: list[str] = field(default_factory=list)      # "kiiy2k :5174" (the dashboard's Projects card)
    media_running: list[str] = field(default_factory=list)     # media services that run right now
    git_total_repos: int = 0
    net_down_kb: float = 0.0
    net_up_kb: float = 0.0

    # Media / Playback
    media_status: str = "none"
    media_title: str | None = None
    media_artist: str | None = None
    media_app: str | None = None
    media_playing: bool = False

    # Services
    services: dict[str, bool] = field(default_factory=dict)
    sampled_at: float = 0.0

    # Self-Resource Telemetry (HUD & Ticker overhead)
    self_cpu: float = 0.0          # Process CPU % (e.g. 0.3%)
    self_ram_mb: float = 0.0       # Working Set in MB (e.g. 48.5)
    self_latency_ms: float = 0.0   # Last collection duration in ms

    # Visual Monitoring: Sparklines & Segmented Memory
    gpu_history: list[float] = field(default_factory=list)
    vram_history: list[float] = field(default_factory=list)
    cpu_history: list[float] = field(default_factory=list)
    vram_model_gb: float = 0.0
    vram_other_gb: float = 0.0
    vram_free_gb: float = 0.0


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


def clean_log_line(line: str) -> str:
    """Strips leading ISO or space-separated timestamp and bracketed log level."""
    line = re.sub(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*", "", line.strip())
    line = re.sub(r"^\[\w+\]\s*", "", line)
    return line


RUN_RESULT = re.compile(r"^(?P<name>.+?)(?:\.md)?: run \d+ (?P<status>succeeded|failed|cancelled|waiting)\b")
RUN_WORDS = {"succeeded": "finished", "failed": "failed", "cancelled": "cancelled", "waiting": "waiting"}


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
        pass
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


VRAM_RISE_GB = 1.0          # VRAM grown this much in 10 min (at the desk): amber, something is eating it
TEMP_RISE_C = 10            # GPU edge temperature up this much in 5 min: amber
GIT_OLD_DAYS = 3.0          # a repo with changes older than this: red
CARD_FULL_GB = 11.5         # card in use: Windows lets programs use ~12.2 GB, past it the model is squeezed (10-07: 26 tok/s)


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


def disk_color(d: dict, trend: float | None) -> str:
    pct, free = d.get("percent", 0) or 0, d.get("free_gb", 0) or 0
    if pct >= 92 or free < 20:
        return RED
    if trend is not None and trend <= -DISK_TREND_GB:
        return RED                         # filling up: more space used in the last hour
    if trend is not None and trend >= DISK_TREND_GB:
        return GREEN                       # space freed in the last hour
    return AMBER if pct >= 85 else TEXT


def chain_result_color(text: str | None) -> str:
    t = (text or "").lower()
    if "(failed)" in t:
        return RED
    if "(cancelled)" in t or "(waiting)" in t:
        return AMBER
    return GREEN if any(k in t for k in ("(finished)", "(drafted)", "(completed)")) else MUTED


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


def attention(s: "Snapshot") -> list[tuple[str, str, str]]:
    """What needs you, worst first: (color, text, slide tag to open)."""
    out: list[tuple[str, str, str]] = []
    crashes = s.unexpected_reboots + s.gpu_resets
    if crashes:
        out.append((RED, f"{crashes} new crash event{'s' if crashes != 1 else ''}: Away blocked until reviewed", "SYS"))
    if s.ai_state == "offline" and s.ai_mode != "off":
        out.append((RED, "local AI router is down", "AI"))
    if s.vram_evicted:
        out.append((RED, "model spilled out of VRAM (slow)", "HW"))
    if s.gpu_hotspot is not None and s.gpu_hotspot >= HOTSPOT_ALERT_C:
        out.append((RED, f"GPU hotspot {s.gpu_hotspot}°C", "HW"))
    for d in s.disks:
        drive, free, pct = d.get("drive"), d.get("free_gb", 0) or 0, d.get("percent", 0) or 0
        if pct >= 92 or free < 20:
            out.append((RED, f"{drive}: only {free:.0f} GB free", "DISK"))
        elif (s.disk_trends.get(drive) or 0) <= -5:
            out.append((AMBER, f"{drive}: {-s.disk_trends[drive]:.0f} GB used up in the last hour", "DISK"))
    if s.ram_percent >= 92:
        out.append((RED, f"RAM {s.ram_percent:.0f}% used", "HW"))
    if "(failed)" in (s.chain_last_finished or ""):
        out.append((AMBER, f"chain failed: {s.chain_last_finished.removesuffix(' (failed)')}", "RUN"))
    if s.backup_stale:
        out.append((AMBER, "WSL backup is stale", "SYS"))
    if s.ai_tps_slow and s.vram_card_full:
        out.append((AMBER, f"AI slowed ({s.ai_tps:.0f} tok/s): card {s.vram_used_gb:.1f} GB" + (f" · {s.vram_hog}" if s.vram_hog else ""), "AI"))
    elif s.vram_card_full and s.ai_mode == "desk" and s.ai_state in ("loaded", "sleeping"):
        out.append((AMBER, f"card nearly full ({s.vram_used_gb:.1f} GB): the AI may slow" + (f" · {s.vram_hog}" if s.vram_hog else ""), "AI"))
    if s.vram_tight and not s.vram_evicted and s.ai_mode != "away":
        out.append((AMBER, "VRAM tight: the model may spill", "HW"))
    return out


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
                pass
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
                pass
    except Exception:
        pass
    return len(dirty_repos), dirty_repos, total


def format_rate(kb_s: float) -> str:
    """Formats KB/s into human-readable rate string (KB/s or MB/s)."""
    if kb_s >= 1024.0:
        return f"{kb_s / 1024.0:.1f} MB/s"
    return f"{kb_s:.0f} KB/s"


def format_slides(s: Snapshot) -> list[dict]:
    """Creates rotating slides for single-line ticker view.
    Each item has: category, text, color, badge_color.
    """
    slides = []

    # Slide 0: Hardware / Resources
    gpu_txt = f"{round(s.gpu_load)}%" if s.gpu_load is not None else "--"
    vram_used = f"{s.vram_used_gb:.1f}" if s.vram_used_gb is not None else "--"
    vram_tot = f"{s.vram_total_gb:.0f}" if s.vram_total_gb is not None else "16"
    ram_used = f"{s.ram_used_gb:.1f}G" if s.ram_used_gb else "--"

    hw_color = "#38bdf8"  # Cyan default
    reset_badge = ""
    vram_str = f"VRAM {vram_used}/{vram_tot}G"
    if s.unexpected_reboots > 0 or s.gpu_resets > 0:
        reset_badge = f"  ·  [CRASH x{s.unexpected_reboots + s.gpu_resets}!]"
        hw_color = "#f87171"
    if s.vram_evicted:
        hw_color = "#f87171"  # Red alert
        vram_str = f"VRAM {vram_used}/{vram_tot}G (EVICTED!)"
    elif s.vram_tight:
        hw_color = "#f87171" if reset_badge else "#fbbf24"  # Amber warning
        vram_str = f"VRAM {vram_used}/{vram_tot}G (TIGHT)"
    elif s.vram_top_process:
        vram_str = f"VRAM {vram_used}/{vram_tot}G ({s.vram_top_process})"

    # Telemetry additions: GPU temperature & fan RPM
    gpu_telemetry = []
    if s.gpu_temp is not None:
        if s.gpu_hotspot is not None and s.gpu_hotspot >= HOTSPOT_ALERT_C:
            gpu_telemetry.append(f"{s.gpu_temp}°C (🔥{s.gpu_hotspot}°C!)")
            hw_color = "#f87171"  # Alert pulse on high hotspot
        else:
            gpu_telemetry.append(f"{s.gpu_temp}°C")
    if s.gpu_fan_rpm and s.gpu_fan_rpm > 0:
        gpu_telemetry.append(f"{s.gpu_fan_rpm}rpm")

    gpu_display = f"GPU {gpu_txt} ({' · '.join(gpu_telemetry)})" if gpu_telemetry else f"GPU {gpu_txt}"

    hw_detail = f"CPU {s.cpu_percent:.0f}% · {s.gpu_name or 'GPU'}"
    if s.gpu_temp is not None:
        hotspot_str = f", Hotspot {s.gpu_hotspot}°C" if s.gpu_hotspot else ""
        mem_str = f", Mem {s.gpu_mem_temp}°C" if s.gpu_mem_temp else ""
        fan_str = f" · Fan {s.gpu_fan_rpm or 0} RPM"
        hw_detail += f" · Temp: Edge {s.gpu_temp}°C{hotspot_str}{mem_str}{fan_str}"
    if s.vram_processes:
        top_str = " · ".join([f"{p['name']} {p['dedicated_gb']}G" for p in s.vram_processes[:3]])
        hw_detail += f" · Top VRAM: {top_str}"
    elif s.vram_top_process:
        hw_detail += f" · Top VRAM: {s.vram_top_process}"

    slides.append({
        "tag": "HW",
        "text": f"{gpu_display}  ·  {vram_str}  ·  RAM {ram_used}{reset_badge}",
        "color": hw_color,
        "detail": hw_detail
    })

    # Slide 0b: Away progress (first while Away works, so it's what you see when you glance at it)
    if s.ai_mode == "away" and s.away_line:
        pct = f"{int(s.away_fraction * 100)} %  ·  " if s.away_fraction is not None and s.away_phase == "working" else ""
        eta = f"  ·  {s.away_eta.split(' · ')[0]}" if s.away_eta else ""
        who = "you're here, AI keeps working" if s.away_present else "Away screen on"
        slides.insert(0, {
            "tag": "AWAY",
            "text": f"{pct}{s.away_line}{eta}",
            "color": "#4ade80" if s.away_phase == "done" else "#38bdf8",
            "detail": f"{s.away_line}\n{s.away_eta or 'no time estimate yet'} · {who} · right-click → Stop AI work",
        })

    # Slide 1: Local AI
    model_str = s.ai_model or "No model loaded"
    state_str = "generating ⚡" if s.ai_generating else s.ai_state
    if s.ai_mode == "away" and s.ai_until:
        try:
            until_part = s.ai_until.split("T")[-1][:5]
            mode_str = f"Away until {until_part}"
        except Exception:
            mode_str = "Away mode"
    elif s.ai_mode == "off" and s.ai_reason:
        clean_reason = s.ai_reason.removeprefix("game: ")
        mode_str = f"Off: {clean_reason}"
    else:
        mode_str = f"{s.ai_mode.capitalize()} mode"

    ai_color = "#94a3b8"  # Slate/muted
    if s.ai_generating:
        ai_color = "#4ade80"  # Bright green
    elif s.ai_state == "loaded":
        ai_color = "#4ade80"  # Green
    elif s.ai_state == "sleeping":
        ai_color = "#38bdf8"  # Cyan
    elif s.ai_state == "offline":
        ai_color = "#f87171"  # Red

    lock_flag = " [LOCK]" if s.gpu_locked else ""
    ctx_info = f" (ctx: {s.ai_ctx_size // 1024}k)" if s.ai_ctx_size else ""
    bolt = " ⚡" if s.ai_generating else ""
    model_part = f"{model_str}{bolt} ({state_str})" if s.ai_model or s.ai_state == "offline" else model_str
    slides.append({
        "tag": "AI",
        "text": f"{model_part}  ·  {mode_str}{lock_flag}  ·  " + (f"{s.ai_tps:.0f} tok/s" if s.ai_tps else ":11440"),
        "color": ai_color,
        "detail": f"Active inference on {model_str}{ctx_info}" if s.ai_generating else (f"Reason: {s.ai_reason}" if s.ai_reason else f"llama.cpp router{ctx_info}")
    })

    # Slide 2: Chains
    if s.chain_running:
        step_part = f" ({s.chain_step})" if s.chain_step else ""
        chain_txt = f"Run: {s.chain_running}{step_part}"
        chain_color = "#38bdf8"
    else:
        # say *why* a queued chain waits ("Code review waits for Away mode"), and when the next scheduled one runs
        blocked = next((p for p in s.chain_pending if p.get("blocked")), None)
        if blocked:
            q_part = f"{blocked['chain']} {blocked['blocked']}"
            if s.chain_pending_count > 1:
                q_part += f" (+{s.chain_pending_count - 1})"
        else:
            q_part = f"{s.chain_pending_count} queued" if s.chain_pending_count else "0 queued"
        last = f"  ·  Last: {s.chain_last_finished}" if s.chain_last_finished else ""
        nxt = f"  ·  next: {s.chain_next}" if s.chain_next else ""
        # something waiting is the news: it goes first (the single line is cut off after ~290 px)
        chain_txt = f"Chains: {q_part}{nxt}{last}" if blocked else f"Chains: idle{last}  ·  {q_part}{nxt}"
        chain_color = "#94a3b8"

    slides.append({
        "tag": "RUN",
        "text": chain_txt,
        "color": chain_color,
        "detail": f"Last finished: {s.chain_last_finished}" if s.chain_last_finished else "sol-hud pipelines daemon"
    })

    # Slide 3: Services
    svc_items = []
    for name in SERVICE_ORDER:
        up = s.services.get(name, False)
        dot = "●" if up else "○"
        svc_items.append(f"{name} {dot}")
    svc_txt = "  |  ".join(svc_items)

    svc_detail = "Router :11440, Embed :11443, HUD :7900, chain runner, WSL (its services start on demand)"
    if s.self_cpu > 0 or s.self_ram_mb > 0:
        svc_detail += f"\nHUD Overhead: {s.self_cpu:.1f}% CPU · {s.self_ram_mb:.0f} MB RAM ({s.self_latency_ms:.1f}ms loop)"

    slides.append({
        "tag": "SVC",
        "text": svc_txt,
        "color": "#38bdf8",
        "detail": svc_detail,
    })

    # Slide 4: Disks (if available)
    if s.disks:
        segs, lines = [], []
        for d in s.disks:
            if "free_gb" not in d:
                continue
            drive, trend = d.get("drive"), s.disk_trends.get(d.get("drive"))
            arrow = f" ▼{-trend:.1f}" if trend is not None and trend <= -DISK_TREND_GB else \
                f" ▲{trend:.1f}" if trend is not None and trend >= DISK_TREND_GB else ""
            segs.append((f"{drive}: {d['free_gb']:.0f}G{arrow}", disk_color(d, trend)))
            lines.append(f"{drive}: {d['free_gb']:.0f} GB free ({d.get('percent', 0):.0f}% used)"
                         + (f", {trend:+.1f} GB in the last hour" if trend is not None else ""))
        if segs:
            segs = [("Free ", MUTED)] + joined(segs)
            slides.append({
                "tag": "DISK",
                "text": plain(segs),
                "segments": segs,
                "color": RED if any(c == RED for _, c in segs) else AMBER if any(c == AMBER for _, c in segs) else CYAN,
                "detail": "\n".join(lines) + "\nRed = space used up in the last hour (▼ GB), green = space freed (▲ GB)",
            })

    # Slide 5: System & Backup
    if s.backup_age_h is not None or s.unexpected_reboots is not None:
        if s.backup_age_h is not None:
            age_str = f"{s.backup_age_h:.1f}h ago" if s.backup_age_h < 48 else f"{s.backup_age_h / 24:.1f}d ago"
        else:
            age_str = "no backup"
        crashes = s.unexpected_reboots + s.gpu_resets
        parts = [f"{s.unexpected_reboots} reboot{'s' if s.unexpected_reboots != 1 else ''}"] if s.unexpected_reboots else []
        if s.gpu_resets:
            parts.append(f"{s.gpu_resets} GPU reset{'s' if s.gpu_resets != 1 else ''}")
        reboot_str = f"{' + '.join(parts)} since last review: Away blocked" if crashes \
            else "no new crashes, Away allowed"
        sys_color = "#f87171" if s.backup_stale or crashes > 0 else "#38bdf8"
        slides.append({
            "tag": "SYS",
            "text": f"WSL Backup: {age_str}  ·  Stability: {reboot_str}",
            "color": sys_color,
            "detail": f"Latest backup: {s.backup_latest or 'none'} · counts unexpected reboots (Event 41) and GPU resets "
                      f"after the last review (reports\\stability-ack.json); new ones block Away until reviewed"
        })

    # Slide 6: Daily Note (Polymatica)
    if s.note_exists:
        note_color = "#4ade80" if s.note_words >= 250 else "#38bdf8"
        time_part = f"  ·  Edited {s.note_time}" if s.note_time else ""
        slides.append({
            "tag": "NOTE",
            "text": f"Today: {s.note_words} words{time_part}  ·  Polymatica",
            "color": note_color,
            "detail": f"Daily note ({s.note_words} words, edited {s.note_time or '--'}) · Click to open",
        })
    else:
        y_part = f"  ·  Yesterday: {s.note_words}w" if s.note_words > 0 else ""
        y_detail = f" (yesterday: {s.note_words} words)" if s.note_words > 0 else ""
        slides.append({
            "tag": "NOTE",
            "text": f"No entry today{y_part}  ·  Polymatica Vault",
            "color": "#94a3b8",
            "detail": f"Today's daily note not started yet{y_detail} · Click to create and open",
        })

    # Slide 7: Workspace Git
    if s.git_dirty_count > 0:
        if len(s.git_dirty_repos) <= 3:
            repos_summary = ", ".join(s.git_dirty_repos)
        else:
            repos_summary = f"{', '.join(s.git_dirty_repos[:2])} +{s.git_dirty_count - 2} more"
        oldest = max(s.git_dirty_days.values(), default=0)
        age_part = f"  ·  oldest {oldest:.0f} day{'s' if round(oldest) != 1 else ''}" if oldest >= 1 else ""
        slides.append({
            "tag": "GIT",
            "text": f"{s.git_dirty_count} repos dirty ({repos_summary}){age_part}",
            "color": "#fbbf24",
            "detail": f"Uncommitted: {', '.join(s.git_dirty_repos)} · {s.git_total_repos} total repos",
        })
    elif s.git_total_repos > 0:
        slides.append({
            "tag": "GIT",
            "text": f"All {s.git_total_repos} repos clean  ·  Workspace",
            "color": "#4ade80",
            "detail": f"All {s.git_total_repos} git repositories in D:\\Workspace clean",
        })

    # Slide 7b: what runs from D:\Workspace (projects started from the dashboard, media services that are up)
    if s.apps_running or s.media_running:
        parts = [f"▶ {a}" for a in s.apps_running[:3]] + ([f"+{len(s.apps_running) - 3} more"] if len(s.apps_running) > 3 else [])
        parts += [f"{m} up" for m in s.media_running]
        slides.append({
            "tag": "APPS",
            "text": "  ·  ".join(parts),
            "color": "#38bdf8",
            "detail": "Running: " + ", ".join(s.apps_running + [f"{m} (media)" for m in s.media_running])
                      + " · Click for the dashboard's Projects card",
        })

    # Slide 8: Network Throughput
    net_col = "#4ade80" if (s.net_down_kb > 500 or s.net_up_kb > 500) else "#38bdf8"
    slides.append({
        "tag": "NET",
        "text": f"↓ {format_rate(s.net_down_kb)}  ·  ↑ {format_rate(s.net_up_kb)}  ·  LAN/WAN",
        "color": net_col,
        "detail": f"Throughput: {format_rate(s.net_down_kb)} down, {format_rate(s.net_up_kb)} up",
    })

    # Slide 9: Media Now-Playing (if playing or paused track exists)
    if s.media_title and s.media_status in ("Playing", "Paused"):
        app_tag = f" · {s.media_app}" if s.media_app else ""
        artist_part = f" — {s.media_artist}" if s.media_artist else ""
        if s.media_status == "Playing":
            media_txt = f"▶ {s.media_title}{artist_part}{app_tag}"
            media_col = "#4ade80"
        else:
            media_txt = f"⏸ {s.media_title}{artist_part} (paused)"
            media_col = "#94a3b8"
        slides.append({
            "tag": "MEDIA",
            "text": media_txt,
            "color": media_col,
            "detail": f"Now Playing: {s.media_title}{artist_part} ({s.media_app or 'Media'}) · Click to focus player",
        })

    # The last Away session (a morning summary): shown until you click it or 18 h pass
    if s.night:
        n = s.night
        segs = [(f"Last Away {n['start']}–{n['end']}: ", MUTED), (f"{n['done']} job{'s' if n['done'] != 1 else ''} ✓", GREEN)]
        if n["failed"]:
            segs += [SEP, (f"{len(n['failed'])} ✗ ({', '.join(n['failed'][:2])})", RED)]
        for c in n["chains"]:
            segs += [SEP, (c, chain_result_color(c))]
        segs += [SEP, (f"ended: {n['reason']}", MUTED)]
        slides.append({"tag": "NIGHT", "text": plain(segs), "segments": segs, "color": RED if n["failed"] else GREEN,
                       "detail": f"Away {n['start']}–{n['end']} · jobs done: {n['done']} · failed: {', '.join(n['failed']) or 'none'}"
                                 f" · chains: {', '.join(n['chains']) or 'none'} · ended: {n['reason']}\n"
                                 "Click to open the newest review / report (this summary then goes away)",
                       "level": "info"})

    if s.review:
        slides.append(review_slide(s.review))

    # What needs you, in one slide (the ticker shows it first and holds it longer)
    alerts = attention(s)
    if alerts:
        segs = joined([(text, color) for color, text, _ in alerts])
        slides.append({"tag": "ALERT", "text": plain(segs), "segments": segs,
                       "color": RED if any(c == RED for c, _, _ in alerts) else AMBER,
                       "detail": "\n".join(f"• {t}" for _, t, _ in alerts), "target": alerts[0][2], "level": "alert"})

    for sl in slides:
        sl.setdefault("segments", colorize(sl, s))
        sl.setdefault("level", slide_level(sl, s))
    return slides


def _review_state() -> dict | None:
    try:
        from ..exam_review import ticker_state
        return ticker_state()
    except Exception:  # noqa: BLE001 - a display nicety
        return None


def review_slide(r: dict) -> dict:
    """The exam review helper on the ticker: the last result while it watches (held like an alert so a glance finds
    it), the full explanation in the tooltip. Click opens the dashboard's card."""
    cur, busy = r.get("current") or {}, r.get("status") in ("reading", "explaining", "answering", "waiting")
    prefix = "Exam " if cur and cur.get("result") == "live" else "Review "
    segs: list[tuple[str, str]] = [(prefix, MUTED)]
    if cur and cur.get("result") == "correct":
        segs += [("✓ correct", GREEN)]
    elif cur and cur.get("result") == "live":
        ans = "/".join(cur.get("answer_labels") or cur.get("correct_labels") or [])
        segs += [(f"Ans: {ans or '?'}", GREEN), SEP, (cur.get("topic") or "practice", CYAN)]
    elif cur:
        segs += [(f"✗ {cur.get('topic') or 'missed'}", RED), SEP,
                 (f"you {'/'.join(cur.get('your_labels') or []) or '?'}", AMBER), SEP,
                 (f"correct {'/'.join(cur.get('correct_labels') or []) or '?'}", GREEN)]
    else:
        segs += [(r.get("text") or "watching", MUTED)]
    count_str = (f"{r.get('answered', 0)} live" if r.get("answered") and not (r.get("correct") or r.get("wrong"))
                 else f"{r.get('correct', 0)} ✓ {r.get('wrong', 0)} ✗")
    segs += [SEP, (count_str, TEXT)]
    if busy:
        segs += [SEP, (r.get("text") or r.get("status", ""), CYAN)]
    elif not r.get("running"):
        segs += [SEP, ("stopped", MUTED)]
    if cur and cur.get("result") == "live":
        detail = (f"{cur.get('question', '')}\nRecommended: {cur.get('answer', '')}\n"
                  f"{cur.get('explanation', '')}")
    elif cur and cur.get("result") != "correct":
        detail = (f"{cur.get('question', '')}\nYou: {cur.get('yours', '')} · Correct: {cur.get('correct') or '(not shown)'}\n"
                  f"{cur.get('explanation', '')}")
    else:
        detail = r.get("text") or ""
    return {"tag": "REVIEW", "text": plain(segs), "segments": segs, "color": CYAN if busy else MUTED,
            "detail": detail + "\nClick to open the Exam review card", "level": "alert" if r.get("running") else "info"}


def _part_color(tag: str, part: str, s: Snapshot) -> str:
    """The color of one ' · '-separated part of a slide (see the color meanings at the top of this file)."""
    p = part.strip()
    if tag == "HW":
        if p.startswith("[CRASH"):
            return RED
        if p.startswith("GPU"):
            if s.gpu_hotspot is not None and s.gpu_hotspot >= HOTSPOT_ALERT_C:
                return RED
            return AMBER if (s.gpu_temp or 0) >= 85 or (s.temp_rise_c or 0) >= TEMP_RISE_C else TEXT
        if p.startswith("VRAM"):
            growing = (s.vram_rise_gb or 0) >= VRAM_RISE_GB and s.ai_mode == "desk"
            return RED if s.vram_evicted else AMBER if s.vram_tight or growing else TEXT
        if p.startswith("RAM"):
            return RED if s.ram_percent >= 92 else AMBER if s.ram_percent >= 85 else TEXT
    elif tag == "AI":
        if p == ":11440":
            return MUTED
        if p.endswith("tok/s"):
            return RED if s.ai_tps_slow else GREEN
        if p.startswith(("Away", "Off", "Desk")):
            return CYAN if p.startswith("Away") else AMBER if p.startswith("Off") else TEXT
        if s.ai_generating or s.ai_state == "loaded":
            return GREEN
        return {"sleeping": CYAN, "offline": RED}.get(s.ai_state, MUTED)
    elif tag == "RUN":
        if p.startswith("Run:"):
            return CYAN
        if p.startswith("Last:"):
            return chain_result_color(p)
        if p.startswith("next:"):
            return TEXT
        if p == "0 queued" or p.startswith("Chains: idle"):
            return MUTED
        return AMBER                                   # something is queued or waiting
    elif tag == "SVC":
        return GREEN if "●" in p else (MUTED if p.startswith("WSL") else RED)
    elif tag == "SYS":
        if p.startswith("WSL Backup"):
            if s.backup_stale or s.backup_age_h is None:
                return RED
            return GREEN if s.backup_age_h < 26 else AMBER
        if p.startswith("Stability"):
            return RED if (s.unexpected_reboots + s.gpu_resets) else GREEN
    elif tag == "NOTE":
        late = time.localtime().tm_hour >= 20     # after 20:00 an unfinished daily note is worth a look
        if p.startswith("Today:"):
            return GREEN if s.note_words >= 250 else AMBER if late else TEXT
        if p.startswith("No entry today"):
            return AMBER if late else MUTED
        return MUTED
    elif tag == "GIT":
        if s.git_dirty_count:
            return RED if max(s.git_dirty_days.values(), default=0) > GIT_OLD_DAYS else AMBER
        return GREEN if p.startswith("All") else MUTED
    elif tag == "NET":
        if p == "LAN/WAN":
            return MUTED
        return CYAN if max(s.net_down_kb, s.net_up_kb) >= 1024 else TEXT
    elif tag == "MEDIA":
        return GREEN if s.media_status == "Playing" else MUTED
    elif tag == "APPS":
        return MUTED if p.startswith("+") else CYAN
    elif tag == "AWAY":
        if p.endswith("%"):
            return GREEN
        return TEXT if "left" in p else CYAN
    return TEXT


def colorize(slide: dict, s: Snapshot) -> list[tuple[str, str]]:
    """Split a slide's text on its separators and color each part."""
    text, tag = slide.get("text", ""), slide.get("tag", "")
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"(  ·  |  \|  )", text):
        if chunk in ("  ·  ", "  |  "):
            out.append((chunk, MUTED))
        elif chunk:
            out.append((chunk, _part_color(tag, chunk, s)))
    return out


def slide_level(slide: dict, s: Snapshot) -> str:
    """alert (shown first, held longer) | info (in the rotation) | quiet (only when you click through: all fine)."""
    tag = slide.get("tag")
    if tag == "DISK":
        return "info" if any(c != TEXT for t, c in slide["segments"] if t not in ("Free ",) and c != MUTED) else "quiet"
    if tag == "SVC":
        return "quiet" if all(s.services.get(n, False) for n in ("Router", "Embed", "HUD", "Chains")) else "info"
    if tag == "SYS":
        old_backup = s.backup_age_h is None or s.backup_age_h >= 72
        return "info" if (s.unexpected_reboots + s.gpu_resets) or s.backup_stale or old_backup else "quiet"
    if tag == "GIT":
        return "info" if s.git_dirty_count else "quiet"
    if tag == "NET":
        return "info" if max(s.net_down_kb, s.net_up_kb) >= 1024 else "quiet"
    return "info"


def rotation(slides: list[dict]) -> list[int]:
    """The order the ticker cycles through by itself: the ALERT slide first and between every other slide, quiet
    slides left out (unless that would leave fewer than 3). Clicking still steps through every slide.
    With two alert-level slides (ALERT and the exam REVIEW) they take turns in those between-slots."""
    alert = [i for i, sl in enumerate(slides) if sl.get("level") == "alert"]
    rest = [i for i, sl in enumerate(slides) if sl.get("level") == "info"]
    if len(rest) < 3:
        rest = [i for i, sl in enumerate(slides) if sl.get("level") != "alert"]
    if not alert:
        return rest or list(range(len(slides)))
    order: list[int] = []
    for k, i in enumerate(rest):
        order += [alert[k % len(alert)], i]
    return order or alert


def format_multiline_rows(s: Snapshot) -> list[dict]:
    """Creates 5 rows of data for the multi-line tile view."""
    # Row 1: Hardware
    gpu_txt = f"{round(s.gpu_load)}%" if s.gpu_load is not None else "--"
    vram_used = f"{s.vram_used_gb:.1f}" if s.vram_used_gb is not None else "--"
    vram_tot = f"{s.vram_total_gb:.0f}" if s.vram_total_gb is not None else "16"
    vram_status = "EVICTED" if s.vram_evicted else ("TIGHT" if s.vram_tight else "OK")
    vram_status_col = "#f87171" if s.vram_evicted else ("#fbbf24" if s.vram_tight else "#4ade80")
    if s.vram_evicted:
        vram_display = f"{vram_used}/{vram_tot}G (EVICTED!)"
    elif s.vram_tight:
        vram_display = f"{vram_used}/{vram_tot}G (TIGHT)"
    elif s.vram_top_process:
        vram_display = f"{vram_used}/{vram_tot}G ({s.vram_top_process})"
    else:
        vram_display = f"{vram_used}/{vram_tot}G"

    if s.gpu_temp is not None:
        gpu_display = f"{gpu_txt} ({s.gpu_temp}°C)" if not s.gpu_hotspot else f"{gpu_txt} ({s.gpu_temp}°C/{s.gpu_hotspot}°C)"
    else:
        gpu_display = gpu_txt

    hw_items = [
        ("GPU", gpu_display, RED if (s.gpu_hotspot or 0) >= HOTSPOT_ALERT_C else AMBER if (s.gpu_temp or 0) >= 85 else TEXT),
        ("VRAM", vram_display, vram_status_col),
        ("RAM", f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.0f}G", RED if s.ram_percent >= 92 else AMBER if s.ram_percent >= 85 else TEXT),
    ]
    if s.gpu_fan_rpm is not None:
        hw_items.append(("Fan", f"{s.gpu_fan_rpm} RPM", "#94a3b8"))
    hw_items.append(("CPU", f"{s.cpu_percent:.0f}%", "#94a3b8"))

    row_hw = {
        "title": "HARDWARE",
        "items": hw_items,
    }

    # Row 2: Local AI
    model_str = f"{s.ai_model} ⚡" if s.ai_generating else (s.ai_model or "None")
    if s.ai_generating:
        state_col = "#4ade80"
        state_disp = "generating ⚡"
    else:
        state_col = "#4ade80" if s.ai_state == "loaded" else ("#38bdf8" if s.ai_state == "sleeping" else "#94a3b8")
        state_disp = s.ai_state
    mode_display = s.ai_mode
    if s.ai_mode == "away" and s.ai_until:
        try:
            mode_display = f"away until {s.ai_until.split('T')[-1][:5]}"
        except Exception:
            pass
    elif s.ai_mode == "off" and s.ai_reason:
        mode_display = f"off: {s.ai_reason.removeprefix('game: ')}"

    row_ai = {
        "title": "LOCAL AI",
        "items": [
            ("Model", model_str, "#4ade80" if s.ai_generating else "#e2e8f0"),
            ("State", state_disp, state_col),
            ("Mode", mode_display, "#38bdf8"),
            ("Port", "11440", "#94a3b8"),
        ]
    }
    if s.ai_mode == "away" and s.away_line:
        pct = f"{int(s.away_fraction * 100)}% " if s.away_fraction is not None and s.away_phase == "working" else ""
        row_ai["items"][-1] = ("Job", f"{pct}{s.away_line}", "#38bdf8")
    if not s.ai_model and s.ai_state != "offline":
        row_ai["items"][0] = ("Model", "none loaded", "#94a3b8")

    # Row 3: Chains
    if s.chain_running:
        status_txt = f"{s.chain_running}" + (f" [{s.chain_step}]" if s.chain_step else "")
        status_col = "#38bdf8"
    elif s.chain_last_finished:
        status_txt = f"Idle (Last: {s.chain_last_finished})"
        status_col = "#94a3b8"
    else:
        status_txt = "Idle"
        status_col = "#94a3b8"

    row_chains = {
        "title": "CHAINS",
        "items": [
            ("Current", status_txt, status_col),
            ("Pending", f"{s.chain_pending_count}", "#e2e8f0"),
            ("GPU Lock", "Held" if s.gpu_locked else "Free", "#fbbf24" if s.gpu_locked else "#94a3b8"),
        ]
    }

    # Row 4: Services
    svc_items = []
    for name in SERVICE_ORDER:
        up = s.services.get(name, False)
        off = "off" if name == "WSL" else "down"   # WSL being off is normal (its services start on demand)
        svc_items.append((name, "up" if up else off, "#4ade80" if up else "#64748b"))
    if s.self_cpu > 0 or s.self_ram_mb > 0:
        svc_items.append(("HUD", f"{s.self_cpu:.1f}%", "#38bdf8"))

    row_svc = {
        "title": "SERVICES",
        "items": svc_items
    }

    # Row 5: Storage & Backups
    storage_items = []
    for d in s.disks:
        if "drive" in d and "free_gb" in d:
            trend = s.disk_trends.get(d["drive"])
            arrow = f" ▼{-trend:.1f}" if trend is not None and trend <= -DISK_TREND_GB else f" ▲{trend:.1f}" if trend is not None and trend >= DISK_TREND_GB else ""
            storage_items.append((d["drive"], f"{d['free_gb']:.0f}G free{arrow}", disk_color(d, trend)))
    if s.backup_age_h is not None:
        b_str = f"{s.backup_age_h:.0f}h" if s.backup_age_h < 48 else f"{s.backup_age_h/24:.1f}d"
        b_col = "#f87171" if s.backup_stale else "#4ade80"
        storage_items.append(("Backup", b_str, b_col))
    if s.git_dirty_count > 0:
        storage_items.append(("Git", f"{s.git_dirty_count} dirty", "#fbbf24"))
    elif s.git_total_repos > 0:
        storage_items.append(("Git", "clean", "#4ade80"))
    if s.note_exists:
        note_col = "#4ade80" if s.note_words >= 250 else "#38bdf8"
        storage_items.append(("Note", f"{s.note_words}w", note_col))
    if s.net_down_kb > 0 or s.net_up_kb > 0:
        storage_items.append(("Net", f"↓{format_rate(s.net_down_kb)}", "#38bdf8"))

    row_storage = {
        "title": "SYSTEM",
        "items": storage_items or [("Disks", "available", "#94a3b8")]
    }

    return [row_hw, row_ai, row_chains, row_svc, row_storage]


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
        if gpu_avail:
            # the per-process counters sometimes report more than the card has (dwm "29.6G" on a 16 GB card): skip those
            procs = [p for p in gpu_latest.get("processes", []) if p.get("dedicated_gb", 0) <= (vram_total or 16.0)]
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
                pass
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
                pass
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
                    pass
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
                    pass
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
            pass

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
                    pass
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
                pass
            if self._stop.wait(self.interval):   # the hub slows this down while nobody is looking
                break
