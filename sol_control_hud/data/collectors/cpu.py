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
    threads = psutil.cpu_percent(percpu=True)
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
