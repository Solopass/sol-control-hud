"""Browsers that look like they are decoding video on the CPU instead of the graphics card.

Why: on 10-08 a YouTube video in Brave froze and stuttered badly. The card was nearly idle (3 % busy), nothing on
the whole machine was using hardware video decode, and Brave was burning 37 % of one core - software decode, at
4K. Restarting Brave fixed it and the cause was never found; the suspicion is a race at boot (Brave started 25 s
after the machine came up, while the GPU preferences were being re-applied). A race recurs, and nothing on the
machine said a word the first time. This says it.

The signal is a combination, because no single counter means "software decode":
  - nothing anywhere on the machine is using the video decode engine, **and**
  - a browser is burning real CPU, **and**
  - it is drawing (3D work), so something is actually playing rather than a tab mining in the background,
  - held for several samples in a row, so a burst of page JavaScript does not trip it.
Even then this is "looks like", not "is": a heavy web app can burn a core with the decode engine legitimately idle.
The wording it produces says looks-like, and the fix it suggests (restart the browser) is cheap and harmless.
"""
from __future__ import annotations

import time
from typing import Callable

import psutil

from . import gpu

BROWSERS = {"brave", "chrome", "msedge", "vivaldi", "firefox", "chromium", "opera", "librewolf"}
PLAYERS = {"vlc", "mpc-hc64", "mpc-be64", "mpv", "PotPlayerMini64"}
WATCHED = {n.lower() for n in BROWSERS | PLAYERS}

HW_DECODE_IDLE = 1.0      # total % across every process: below this, nothing is decoding on the GPU
CPU_BUSY = 25.0           # % of one core, summed over an app's processes
MIN_3D = 0.3              # it is drawing something, so a video is plausibly on screen
NEEDED_SAMPLES = 3        # in a row, so a burst of page JavaScript does not trip it


def _engine_totals(util: dict[str, float]) -> tuple[float, dict[str, float]]:
    """(hardware decode across the whole machine, 3D per process name)."""
    decode = 0.0
    three_d: dict[str, float] = {}
    names: dict[int, str] = {}
    for path, value in util.items():
        kind = gpu._engtype(path).lower()
        if kind in ("videodecode", "videoprocessing"):
            decode += float(value)
        elif kind == "3d":
            pid = gpu._pid(path)
            if pid is not None:
                name = gpu.process_name(pid, names)
                three_d[name] = three_d.get(name, 0.0) + float(value)
    return decode, three_d


def cpu_seconds_by_app(procs: Callable[[], list] | None = None) -> dict[str, float]:
    """Total CPU seconds per watched app, summed over its processes (a browser is dozens)."""
    out: dict[str, float] = {}
    for p in (procs or (lambda: psutil.process_iter(["name"])))():
        try:
            name = (p.info.get("name") or "").removesuffix(".exe")
            if name.lower() not in WATCHED:
                continue
            t = p.cpu_times()
            out[name] = out.get(name, 0.0) + t.user + t.system
        except (psutil.Error, OSError, AttributeError):
            continue
    return out


class SoftwareDecodeWatch:
    """Keeps just enough history to tell a sustained burn from a burst. One instance, updated every few seconds."""

    def __init__(self, needed: int = NEEDED_SAMPLES, cpu_busy: float = CPU_BUSY):
        self.needed, self.cpu_busy = needed, cpu_busy
        self._cpu: dict[str, float] = {}
        self._at: float | None = None
        self.streak: dict[str, int] = {}

    def update(self, util: dict[str, float], cpu_now: dict[str, float] | None = None,
               now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        cpu_now = cpu_seconds_by_app() if cpu_now is None else cpu_now
        decode, three_d = _engine_totals(util or {})
        elapsed = (now - self._at) if self._at is not None else 0.0
        previous, self._cpu, self._at = self._cpu, cpu_now, now

        rows = []
        for name, seconds in cpu_now.items():
            if name.lower() not in WATCHED:
                continue              # the rule filters too, not only the gatherer: a caller may pass its own CPU
            was = previous.get(name)
            if was is None or elapsed <= 0:
                continue                                  # first sight of this app: no rate yet
            percent = max(0.0, (seconds - was) / elapsed * 100.0)
            drawing = three_d.get(name, 0.0)
            suspect = (decode < HW_DECODE_IDLE and percent >= self.cpu_busy and drawing >= MIN_3D)
            self.streak[name] = self.streak.get(name, 0) + 1 if suspect else 0
            rows.append({"name": name, "cpu_percent": round(percent, 1), "gpu_3d": round(drawing, 1),
                         "samples": self.streak[name], "suspect": self.streak[name] >= self.needed})
        for gone in set(self.streak) - set(cpu_now):
            self.streak.pop(gone, None)

        rows.sort(key=lambda r: -r["cpu_percent"])
        return {"available": True, "hardware_decode_percent": round(decode, 1),
                "apps": rows, "suspects": [r for r in rows if r["suspect"]]}
