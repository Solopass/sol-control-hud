"""One-call process snapshot for Windows (pid, name, cpu seconds, working set), for the Top processes card.

Why: psutil's per-process `cpu_times()` / `memory_info()` each ask Windows for the whole system process table, so a
scan of ~400 processes costs ~400 table reads (about 1 s of CPU on this PC). `NtQuerySystemInformation(
SystemProcessInformation)` returns the same numbers for every process in a single call (a few ms). `snapshot()` raises
OSError / NotImplementedError when it can't be used; the caller then falls back to psutil.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_SystemProcessInformation = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


class _SPI(ctypes.Structure):
    """SYSTEM_PROCESS_INFORMATION up to WorkingSetSize (64-bit layout)."""
    _fields_ = [("NextEntryOffset", ctypes.c_ulong), ("NumberOfThreads", ctypes.c_ulong),
                ("WorkingSetPrivateSize", ctypes.c_longlong), ("HardFaultCount", ctypes.c_ulong),
                ("NumberOfThreadsHighWatermark", ctypes.c_ulong), ("CycleTime", ctypes.c_ulonglong),
                ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
                ("ImageName", _UNICODE_STRING), ("BasePriority", ctypes.c_long),
                ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p),
                ("HandleCount", ctypes.c_ulong), ("SessionId", ctypes.c_ulong), ("UniqueProcessKey", ctypes.c_void_p),
                ("PeakVirtualSize", ctypes.c_size_t), ("VirtualSize", ctypes.c_size_t),
                ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t)]


def parse(buf, total: int) -> list[tuple[int, str, float, int]]:
    """Walk the returned process table: [(pid, name, cpu_seconds, working_set_bytes)]. `buf` is a ctypes buffer."""
    out = []
    base = ctypes.addressof(buf)
    off = 0
    while off < total:
        e = _SPI.from_address(base + off)
        name = ""
        if e.ImageName.Buffer and e.ImageName.Length:
            name = ctypes.wstring_at(e.ImageName.Buffer, e.ImageName.Length // 2)
        out.append((int(e.UniqueProcessId or 0), name, (e.UserTime + e.KernelTime) / 1e7, int(e.WorkingSetSize)))
        if not e.NextEntryOffset:
            break
        off += e.NextEntryOffset
    return out


def snapshot() -> list[tuple[int, str, float, int]]:
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise NotImplementedError("64-bit Windows only")
    nt = ctypes.WinDLL("ntdll")
    nt.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    nt.NtQuerySystemInformation.restype = ctypes.c_long
    size = 1 << 20
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        need = wintypes.ULONG(0)
        status = nt.NtQuerySystemInformation(_SystemProcessInformation, buf, size, ctypes.byref(need))
        if status & 0xFFFFFFFF == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, need.value + (64 << 10))
            continue
        if status < 0:
            raise OSError(f"NtQuerySystemInformation failed: {status & 0xFFFFFFFF:#x}")
        return parse(buf, need.value or size)
    raise OSError("process table kept growing")
