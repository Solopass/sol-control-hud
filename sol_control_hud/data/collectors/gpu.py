"""GPU load and VRAM from Windows performance counters (the same source as Task Manager)."""
from __future__ import annotations

import functools
import re
import threading
import time
import winreg
from typing import Callable

import psutil
import win32pdh

_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"

UTIL = r"\GPU Engine(*)\Utilization Percentage"
ADAPTER_MEM = r"\GPU Adapter Memory(*)\Dedicated Usage"
PROC_DEDICATED = r"\GPU Process Memory(*)\Dedicated Usage"
PROC_SHARED = r"\GPU Process Memory(*)\Shared Usage"
COUNTERS = (UTIL, ADAPTER_MEM, PROC_DEDICATED, PROC_SHARED)


def adapter_info() -> dict:
    """Name and dedicated VRAM of the first discrete adapter with a reported memory size."""
    best = {"name": None, "vram_total_gb": None}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as cls:
            for i in range(winreg.QueryInfoKey(cls)[0]):
                sub = winreg.EnumKey(cls, i)
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        size = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
                        name = winreg.QueryValueEx(k, "DriverDesc")[0]
                except OSError:
                    continue
                gb = round(int(size) / 1024**3, 1)
                if best["vram_total_gb"] is None or gb > best["vram_total_gb"]:
                    best = {"name": name, "vram_total_gb": gb}
    except OSError:
        pass
    return best


def process_name(pid: int, cache: dict[int, str]) -> str:
    if pid not in cache:
        try:
            cache[pid] = psutil.Process(pid).name().removesuffix(".exe")
        except (psutil.Error, OSError):
            cache[pid] = f"pid {pid}"
    return cache[pid]


VENDORS = {0x1002: "amd", 0x8086: "intel", 0x10DE: "nvidia"}


def chip_map() -> dict[str, str]:
    """{counter luid: "amd" | "intel" | ...} for the real graphics chips (Microsoft's software renderer left out)."""
    try:
        from .budget import adapters
        return {a["luid"].lower(): VENDORS[a["vendor"]] for a in adapters() if a.get("vendor") in VENDORS and a.get("luid")}
    except Exception:  # noqa: BLE001 - without it the sampler falls back to "the adapter with the most memory"
        return {}


class GpuSampler:
    """Samples GPU utilization, adapter VRAM and per-process VRAM every `interval` seconds in a background thread.

    Each counter is added once as a wildcard and read as an array, which PDH re-enumerates on every collect: a process
    that starts later (llama-server when a model loads) shows up in the next sample. Until 10-08 every instance was
    expanded and added one by one (~780 counters, 157 ms) and the query rebuilt every 15 s to catch new ones; tested
    10-08 with a WPF window started mid-query, the wildcard array saw its 8 engine + 1 memory instances like a fresh
    expansion did. The query is still rebuilt every `rescan` s (now cheap) to refresh the chip map."""

    def __init__(self, interval: float = 2.0, rescan: float = 15.0):
        self.interval, self.rescan = interval, rescan
        self.latest: dict = {"available": False}
        self.last_util: dict[str, float] = {}   # raw engine values, for displays.misplaced (no second sampler)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._names: dict[int, str] = {}
        self._last_values: dict[str, dict[str, float]] = {}
        self._rescan_requested = threading.Event()
        self.info = adapter_info()

    def request_rescan(self) -> None:
        """Trigger an immediate counter query rebuild on the next loop iteration."""
        self._rescan_requested.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="gpu-sampler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _build(self):
        self.info["chips"] = chip_map()          # each rescan: the Intel driver can arrive while the HUD runs (10-07)
        query = win32pdh.OpenQuery()
        handles = {c: win32pdh.AddCounter(query, c) for c in COUNTERS}
        win32pdh.CollectQueryData(query)  # utilization is a rate: needs a first sample before values are valid
        return query, handles

    def _run(self) -> None:
        query = handles = None
        built = 0.0
        while not self._stop.is_set():
            try:
                trigger = self._rescan_requested.is_set()
                if trigger:
                    self._rescan_requested.clear()
                if query is None or trigger or time.monotonic() - built >= self.rescan:
                    if query is not None:
                        win32pdh.CloseQuery(query)
                    query, handles = self._build()
                    built = time.monotonic()
                    fresh = True
                else:
                    fresh = False
            except Exception as e:  # counters missing (e.g. no GPU driver)
                self.latest = {"available": False, "error": str(e)}
                query = None
                if self._stop.wait(self.interval * 5):
                    return
                continue
            if self._stop.wait(self.interval):
                return
            try:
                win32pdh.CollectQueryData(query)
                if len(self._names) > 2000:
                    self._names.clear()
                values = {c: _read_all(c, handles[c], win32pdh.PDH_FMT_DOUBLE if c == UTIL else win32pdh.PDH_FMT_LARGE,
                                       self._last_values.get(c, {})) for c in COUNTERS}
                self._last_values = values
                self.last_util = values[UTIL]
                self.latest = summarize(values[UTIL], values[ADAPTER_MEM], values[PROC_DEDICATED], values[PROC_SHARED],
                                        lambda pid: process_name(pid, self._names), self.info)
            except Exception as e:
                if not fresh:
                    self.latest = {"available": False, "error": str(e)}


def _read_all(counter: str, handle, fmt, previous: dict[str, float]) -> dict[str, float]:
    r"""{full counter path: value} for every instance of a wildcard counter, keyed exactly like the expanded paths
    (`\GPU Engine(pid_1_..._engtype_3D)\Utilization Percentage`) so _pid / _luid / _engtype parse them as before.
    If the whole array can't be read this once, the last good values stand in (one sample, not a blank card)."""
    head, tail = counter.split("(*)")
    try:
        return {f"{head}({inst}){tail}": v for inst, v in win32pdh.GetFormattedCounterArray(handle, fmt).items()}
    except Exception:  # noqa: BLE001 - e.g. a rate counter whose instance appeared between two collects
        return previous


def summarize(util: dict[str, float], adapter_mem: dict[str, float], proc_dedicated: dict[str, float],
              proc_shared: dict[str, float], name_of: Callable[[int], str], info: dict) -> dict:
    """Pure: raw counter values keyed by counter path -> the /api/status 'gpu' block."""
    vram: dict[str, float] = {}
    for path, v in adapter_mem.items():
        vram[_luid(path)] = v
    if not vram:
        return {"available": False}
    # The AMD card by its maker (10-07: with the Intel UHD 770 on and apps moved to it, "the adapter holding the most
    # memory" could pick the wrong one); that guess is only the fallback when the chips aren't known.
    chips = {k.lower(): v for k, v in (info.get("chips") or {}).items()}
    amd = [l for l, c in chips.items() if c == "amd" and l in vram]
    luid = amd[0] if amd else max(vram, key=vram.get)

    # sum per engine type across processes; like Task Manager, overall load = busiest engine type
    per_engine: dict[str, float] = {}
    for path, v in util.items():
        if _luid(path) == luid:
            per_engine[_engtype(path)] = per_engine.get(_engtype(path), 0.0) + v
    engines = {e: round(min(v, 100.0), 1) for e, v in per_engine.items() if v > 0.05}

    # per-process memory on the discrete adapter only (browsers moved to the iGPU must not count against this card)
    procs: dict[int, dict] = {}
    for source, key in ((proc_dedicated, "dedicated_gb"), (proc_shared, "shared_gb")):
        for path, v in source.items():
            pid = _pid(path)
            if pid is None or _luid(path) != luid:
                continue
            p = procs.setdefault(pid, {"pid": pid, "dedicated_gb": 0.0, "shared_gb": 0.0})
            p[key] += v / 1024**3
    processes = []
    for p in procs.values():
        if p["dedicated_gb"] + p["shared_gb"] < 0.01:
            continue
        processes.append({"pid": p["pid"], "name": name_of(p["pid"]),
                          "dedicated_gb": round(p["dedicated_gb"], 2), "shared_gb": round(p["shared_gb"], 2)})
    processes.sort(key=lambda p: p["dedicated_gb"], reverse=True)

    # Which chip each app draws on (the dashboard's VRAM and Localhost cards). An app moved to the Intel chip keeps a
    # sliver on the AMD card to show its windows (the monitors hang off it), often as big as its Intel memory
    # (Discord 10-07: 0.09 GB each). An app that draws on the AMD card never touches the Intel chip, so any real
    # memory on another chip means it draws there.
    per: dict[int, dict[str, float]] = {}
    for source in (proc_dedicated, proc_shared):
        for path, v in source.items():
            pid, chip = _pid(path), chips.get(_luid(path))
            if pid is None or chip is None or v <= 0:
                continue
            per.setdefault(pid, {})[chip] = per.setdefault(pid, {}).get(chip, 0.0) + v / 1024**3
    main = chips.get(luid, "amd")
    pid_chips = {}
    for pid, gb in per.items():
        away = {c: v for c, v in gb.items() if c != main and v >= 0.02}
        if away:
            pid_chips[pid] = max(away, key=away.get)
        elif gb.get(main, 0) >= 0.02:
            pid_chips[pid] = main
    other = [c for c in set(chips.values()) if c != main]
    on_other = sorted(({"pid": pid, "name": name_of(pid), "chip": c, "gb": round(sum(per[pid].values()), 2)}
                       for pid, c in pid_chips.items() if c in other), key=lambda p: p["gb"], reverse=True)

    return {
        "available": True,
        "name": info.get("name"),
        "load_percent": max(engines.values(), default=0.0),
        "engines": engines,
        "vram_used_gb": round(vram[luid] / 1024**3, 2),
        "vram_total_gb": info.get("vram_total_gb"),
        "processes": processes,
        "pid_chips": pid_chips,          # pid -> "amd" / "intel": where that app draws
        "on_other_chips": on_other,      # apps drawing on the Intel chip, biggest first
        "chips": sorted(set(chips.values())),
        "sampled_at": time.time(),
    }


@functools.lru_cache(maxsize=8192)   # the same ~700 counter paths every 2 s: parse each once
def _engtype(path: str) -> str:
    m = re.search(r"engtype_([A-Za-z0-9 ]+)\)", path)
    return m.group(1) if m else "other"


@functools.lru_cache(maxsize=8192)   # the same ~700 counter paths every 2 s: parse each once
def _luid(path: str) -> str:
    m = re.search(r"luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+", path)
    return m.group(0).lower() if m else path


@functools.lru_cache(maxsize=8192)   # the same ~700 counter paths every 2 s: parse each once
def _pid(path: str) -> int | None:
    m = re.search(r"pid_(\d+)_", path)
    return int(m.group(1)) if m else None
