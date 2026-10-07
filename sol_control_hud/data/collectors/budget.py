"""Windows' VRAM budget for a program (DXGI IDXGIAdapter3::QueryVideoMemoryInfo), read-only.

The totals ("15.9 GB card, 10.2 GB used") don't say how much one program may keep in VRAM: Windows' video memory
manager gives each process a *budget*, and above it keeps the rest in system RAM (plans\\SOL_FAST_VRAM_SPILL_PLAN.md).
A background process like this probe gets the same kind of budget as llama-server, so it tells us what a model gets.

    python -m sol_control_hud.data.collectors.budget      # prints the discrete GPU's local (VRAM) budget
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

HRESULT = ctypes.c_long


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str):
        super().__init__()
        a, b, c, d, e = s.strip("{}").split("-")
        self.Data1, self.Data2, self.Data3 = int(a, 16), int(b, 16), int(c, 16)
        self.Data4[:] = list(bytes.fromhex(d + e))


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", wintypes.WCHAR * 128), ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", ctypes.c_int64), ("Flags", wintypes.UINT)]


class DXGI_QUERY_VIDEO_MEMORY_INFO(ctypes.Structure):
    _fields_ = [("Budget", ctypes.c_uint64), ("CurrentUsage", ctypes.c_uint64),
                ("AvailableForReservation", ctypes.c_uint64), ("CurrentReservation", ctypes.c_uint64)]


IID_IDXGIFactory1 = GUID("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIAdapter3 = GUID("645967A4-1392-4310-A798-8053CE3E93FD")


def _method(obj, index: int, *argtypes):
    """The COM method at `index` of obj's vtable, as a callable taking (obj, *args)."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtbl[2])(obj)


def adapters() -> list[dict]:
    """Every graphics adapter Windows offers apps: [{name, vendor}] (vendor 0x8086 = Intel, 0x1002 = AMD,
    0x1414 = Microsoft's software renderer). The Intel chip only shows up once it is enabled in the BIOS."""
    factory = ctypes.c_void_p()
    if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
        return []
    out = []
    try:
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if _method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(factory, i, ctypes.byref(adapter)) != 0:
                break                                    # EnumAdapters1: DXGI_ERROR_NOT_FOUND ends the list
            desc = DXGI_ADAPTER_DESC1()
            _method(adapter, 10, ctypes.POINTER(DXGI_ADAPTER_DESC1))(adapter, ctypes.byref(desc))    # GetDesc1
            out.append({"name": desc.Description, "vendor": int(desc.VendorId)})
            _release(adapter)
            i += 1
    finally:
        _release(factory)
    return out


def vram_budget() -> dict:
    """{name, vram_gb, budget_gb, usage_gb} for the adapter with the most dedicated VRAM (the RX 9070 XT). usage is this
    probe's own (~0); the budget is what Windows would let a background program keep in VRAM right now."""
    factory = ctypes.c_void_p()
    if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
        return {"available": False, "error": "CreateDXGIFactory1 failed"}
    best = None
    try:
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if _method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(factory, i, ctypes.byref(adapter)) != 0:
                break                                    # EnumAdapters1: DXGI_ERROR_NOT_FOUND ends the list
            desc = DXGI_ADAPTER_DESC1()
            _method(adapter, 10, ctypes.POINTER(DXGI_ADAPTER_DESC1))(adapter, ctypes.byref(desc))    # GetDesc1
            if best is None or desc.DedicatedVideoMemory > best[1].DedicatedVideoMemory:
                if best:
                    _release(best[0])
                best = (adapter, desc)
            else:
                _release(adapter)
            i += 1
        if best is None:
            return {"available": False, "error": "no adapter"}
        adapter3 = ctypes.c_void_p()
        if _method(best[0], 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                best[0], ctypes.byref(IID_IDXGIAdapter3), ctypes.byref(adapter3)) != 0:
            return {"available": False, "error": "no IDXGIAdapter3"}
        info = DXGI_QUERY_VIDEO_MEMORY_INFO()
        hr = _method(adapter3, 14, wintypes.UINT, ctypes.c_int, ctypes.POINTER(DXGI_QUERY_VIDEO_MEMORY_INFO))(
            adapter3, 0, 0, ctypes.byref(info))           # QueryVideoMemoryInfo(node 0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL)
        _release(adapter3)
        if hr != 0:
            return {"available": False, "error": f"QueryVideoMemoryInfo 0x{hr & 0xFFFFFFFF:08x}"}
        gb = 1024 ** 3
        return {"available": True, "name": best[1].Description, "vram_gb": round(best[1].DedicatedVideoMemory / gb, 2),
                "budget_gb": round(info.Budget / gb, 2), "usage_gb": round(info.CurrentUsage / gb, 3)}
    finally:
        if best:
            _release(best[0])
        _release(factory)


if __name__ == "__main__":
    print(vram_budget())
