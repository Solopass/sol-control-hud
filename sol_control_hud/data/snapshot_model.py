"""The Snapshot dataclass and the constants the readers, the collector and the text all share.
Split out of snapshot.py on 2026-10-08 (moved verbatim); snapshot.py re-exports every name."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


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
    vram_evicted: bool = False                 # the runner's shared (system RAM) memory is over the line: memory only
    vram_spill_impact: str = ""                # while evicted: "slow" / "fine" = an answer since the spill was slow / ran
                                               # at its usual speed; "unknown" = nothing answered since (spill_impact)
    vram_spilled_gb: float = 0.0
    hud_alert: str | None = None               # the HUD's own trouble (health.py): a sampler stopped, crashing               # the AI runner's memory in system RAM (sol-fast keeps ~0.62 by design)
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


def clean_log_line(line: str) -> str:
    """Strips leading ISO or space-separated timestamp and bracketed log level."""
    line = re.sub(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s*", "", line.strip())
    line = re.sub(r"^\[\w+\]\s*", "", line)
    return line


RUN_RESULT = re.compile(r"^(?P<name>.+?)(?:\.md)?: run \d+ (?P<status>succeeded|failed|cancelled|waiting)\b")
RUN_WORDS = {"succeeded": "finished", "failed": "failed", "cancelled": "cancelled", "waiting": "waiting"}


VRAM_RISE_GB = 1.0          # VRAM grown this much in 10 min (at the desk): amber, something is eating it
TEMP_RISE_C = 10            # GPU edge temperature up this much in 5 min: amber
GIT_OLD_DAYS = 3.0          # a repo with changes older than this: red
CARD_FULL_GB = 11.5         # card in use: Windows lets programs use ~12.2 GB, past it the model is squeezed (10-07: 26 tok/s)


NO_SPILL = object()
SPILL_WORD = {"unknown": "spill? speed not measured", "fine": "spill, speed OK", "slow": "EVICTED!", "": "spill"}
