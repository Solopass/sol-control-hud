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


class GpuSampler:
    """Samples GPU utilization, adapter VRAM and per-process VRAM every `interval` seconds in a background thread.

    Wildcard counters only expand to instances that exist when the query is built, so a process that starts later
    (e.g. llama-server when a model loads) would never be sampled. The query is rebuilt every `rescan` seconds."""

    def __init__(self, interval: float = 2.0, rescan: float = 15.0,
                 expand: Callable[[str], list[str]] | None = None):
        self.interval, self.rescan = interval, rescan
        self.expand = expand or win32pdh.ExpandCounterPath
        self.latest: dict = {"available": False}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._names: dict[int, str] = {}
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
        query = win32pdh.OpenQuery()
        handles = {c: [(p, win32pdh.AddCounter(query, p)) for p in self.expand(c)] for c in COUNTERS}
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
                values = {c: _read_all(handles[c], win32pdh.PDH_FMT_DOUBLE if c == UTIL else win32pdh.PDH_FMT_LARGE) for c in COUNTERS}
                self.latest = summarize(values[UTIL], values[ADAPTER_MEM], values[PROC_DEDICATED], values[PROC_SHARED],
                                        lambda pid: process_name(pid, self._names), self.info)
            except Exception as e:
                if not fresh:
                    self.latest = {"available": False, "error": str(e)}


def _read_all(handles, fmt) -> dict[str, float]:
    out = {}
    for path, h in handles:
        try:
            out[path] = win32pdh.GetFormattedCounterValue(h, fmt)[1]
        except Exception:  # instance went away (process exited) since the query was built
            continue
    return out


def summarize(util: dict[str, float], adapter_mem: dict[str, float], proc_dedicated: dict[str, float],
              proc_shared: dict[str, float], name_of: Callable[[int], str], info: dict) -> dict:
    """Pure: raw counter values keyed by counter path -> the /api/status 'gpu' block."""
    vram: dict[str, float] = {}
    for path, v in adapter_mem.items():
        vram[_luid(path)] = v
    if not vram:
        return {"available": False}
    luid = max(vram, key=vram.get)  # the adapter holding the most dedicated memory is the discrete GPU

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

    return {
        "available": True,
        "name": info.get("name"),
        "load_percent": max(engines.values(), default=0.0),
        "engines": engines,
        "vram_used_gb": round(vram[luid] / 1024**3, 2),
        "vram_total_gb": info.get("vram_total_gb"),
        "processes": processes,
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
