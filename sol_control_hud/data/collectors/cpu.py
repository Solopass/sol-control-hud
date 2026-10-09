"""CPU detail for the dashboard's CPU · GPU card: load per thread, P-cores vs E-cores, the busiest core, the live clock.

i9-14900K on Windows: logical CPUs 0-15 are the 8 P-cores (two threads each, 0/1 = core 0), 16-31 the 16 E-cores
(one thread each). The live clock is Windows' "% Processor Performance" counter x the base clock (psutil only reports
the base 3.2 GHz). No temperature: Windows has no sensor API for it without a kernel driver (LibreHardwareMonitor).
"""
from __future__ import annotations

import platform
import threading

import psutil

try:
    import win32pdh
except ImportError:  # pragma: no cover - pywin32 ships with the venv on SOL
    win32pdh = None

_PERF = r"\Processor Information(_Total)\% Processor Performance"
_UTILITY = r"\Processor Information(*)\% Processor Utility"


class Utility:
    """CPU load the way Task Manager shows it: Windows' "% Processor Utility", per logical CPU and total.

    Why not psutil: psutil reports "% Processor Time" (share of time busy). Task Manager on Windows 10/11 shows
    "% Processor Utility", which also counts how fast the cores run, so it reads higher on a boosting 14900K
    (10-08: 10.0 % vs 7.6 % at the same moment). The HUD should match the number you compare it with. Capped at 100
    like Task Manager. Each instance keeps its own query (a rate is the average since that query's last read), so the
    ticker's snapshot and the dashboard's CPU card don't steal each other's interval. None until it has two reads,
    and None if the counter is unavailable (callers fall back to psutil)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._q = self._c = None

    def read(self) -> tuple[float, list[float]] | None:
        if win32pdh is None:
            return None
        with self._lock:
            try:
                if self._q is None:
                    self._q = win32pdh.OpenQuery()
                    self._c = win32pdh.AddEnglishCounter(self._q, _UTILITY)
                    win32pdh.CollectQueryData(self._q)
                    return None
                win32pdh.CollectQueryData(self._q)
                arr = win32pdh.GetFormattedCounterArray(self._c, win32pdh.PDH_FMT_DOUBLE)
            except Exception:  # noqa: BLE001 - fall back to psutil this time, try a fresh query next time
                self._q = None
                return None
        return parse_utility(arr)


def parse_utility(arr: dict[str, float]) -> tuple[float, list[float]] | None:
    """{"0,0": 12.3, ..., "0,_Total": 9.1, "_Total": 9.1} -> (total, per logical CPU in order), each capped at 100."""
    total = arr.get("_Total", arr.get("0,_Total"))
    per = sorted(((int(k.split(",")[1]), v) for k, v in arr.items()
                  if "," in k and k.split(",")[1].isdigit()), key=lambda kv: (kv[0]))
    if total is None or not per:
        return None
    cap = lambda x: round(min(max(x, 0.0), 100.0), 1)  # noqa: E731
    return cap(total), [cap(v) for _, v in per]


class _Clock:
    """One PDH query kept open (opening one per read costs ~20 ms); each read is the average since the last one."""

    def __init__(self):
        self._lock = threading.Lock()
        self._q = self._c = None

    def ghz(self, base_mhz: float) -> float | None:
        if win32pdh is None or not base_mhz:
            return None
        with self._lock:
            try:
                if self._q is None:
                    self._q = win32pdh.OpenQuery()
                    self._c = win32pdh.AddEnglishCounter(self._q, _PERF)
                    win32pdh.CollectQueryData(self._q)
                    return None                        # the first read has nothing to compare with
                win32pdh.CollectQueryData(self._q)
                pct = win32pdh.GetFormattedCounterValue(self._c, win32pdh.PDH_FMT_DOUBLE)[1]
            except Exception:  # noqa: BLE001 - a display nicety; retry with a fresh query next time
                self._q = None
                return None
        return round(base_mhz * pct / 100 / 1000, 2)


_CLOCK = _Clock()
_CARD_UTILITY = Utility()
_NAME: str | None = None


def _name() -> str:
    global _NAME
    if _NAME is None:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                _NAME = " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
        except OSError:
            _NAME = platform.processor() or "CPU"
    return _NAME


def split(threads: list[float], physical: int) -> dict:
    """P/E split from per-thread load. P-cores are the hyper-threaded ones: logical = 2*P + E and physical = P + E,
    so P = logical - physical. Each core's load = the average of its threads."""
    n = len(threads)
    p = max(0, min(n - physical, physical))
    cores = [(threads[2 * i] + threads[2 * i + 1]) / 2 for i in range(p)] + threads[2 * p:]
    pc, ec = cores[:p], cores[p:]
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    busiest = max(range(len(cores)), key=cores.__getitem__) if cores else None
    return {"p_cores": p, "e_cores": len(ec), "p_load": avg(pc), "e_load": avg(ec),
            "cores": [round(c, 1) for c in cores],
            "busiest": None if busiest is None else {"name": f"P{busiest}" if busiest < p else f"E{busiest - p}",
                                                     "load": round(cores[busiest], 1)}}


def sample() -> dict:
    """{name, load, threads, clock_ghz, base_ghz, p_cores, e_cores, p_load, e_load, cores, busiest}. Load is since the
    previous call (psutil keeps the last reading), so call it on a steady pace."""
    threads = psutil.cpu_percent(percpu=True)            # keeps psutil's interval going for the fallback
    u = _CARD_UTILITY.read()
    if u and len(u[1]) == len(threads):
        threads = u[1]
    physical = psutil.cpu_count(logical=False) or len(threads)
    try:
        base = psutil.cpu_freq().max or psutil.cpu_freq().current
    except Exception:  # noqa: BLE001
        base = 0.0
    load = round(sum(threads) / len(threads), 1) if threads else 0.0
    return {"available": bool(threads), "name": _name(), "load": load, "threads": len(threads),
            "clock_ghz": _CLOCK.ghz(base), "base_ghz": round(base / 1000, 1) if base else None, **split(threads, physical)}


if __name__ == "__main__":
    import time
    sample()
    time.sleep(1)
    print(sample())
