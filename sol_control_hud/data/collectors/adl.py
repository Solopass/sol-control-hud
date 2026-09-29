"""AMD Display Library (ADL) sensor collector for modern Radeon GPUs.
Queries GPU edge temp, hotspot/junction temp, memory temp, and fan RPM via atiadlxx.dll.
"""
from __future__ import annotations

import ctypes
from ctypes import (
    Structure,
    WINFUNCTYPE,
    byref,
    c_int,
    c_void_p,
    cast,
    create_string_buffer,
    sizeof,
)
from dataclasses import dataclass
import threading


@dataclass
class GpuSensors:
    temp_edge: int | None = None
    temp_hotspot: int | None = None
    temp_mem: int | None = None
    fan_rpm: int | None = None


# ADL constants
ADL_OK = 0
ADL_PMLOG_MAX_SENSORS = 256
PMLOG_TEMPERATURE_EDGE = 8
PMLOG_TEMPERATURE_MEM = 9
PMLOG_FAN_RPM = 14
PMLOG_TEMPERATURE_HOTSPOT = 27

ADL_MAIN_MALLOC_CALLBACK = WINFUNCTYPE(c_void_p, c_int)


class _ADLSingleSensorData(Structure):
    _fields_ = [
        ("iSupported", c_int),
        ("iValue", c_int),
    ]


class _ADLPMLogDataOutput(Structure):
    _fields_ = [
        ("iSize", c_int),
        ("sensors", _ADLSingleSensorData * ADL_PMLOG_MAX_SENSORS),
    ]


class AdlReader:
    """Manages ADL2 connection to read PMLog sensor telemetry on Windows."""

    def __init__(self, dll_name: str = "atiadlxx.dll"):
        self.dll_name = dll_name
        self._lock = threading.Lock()
        self._initialized = False
        self._supported = False
        self._adl = None
        self._ctx = c_void_p()
        self._allocations: list[ctypes.Array] = []
        self._malloc_cb = None
        self._log_data = _ADLPMLogDataOutput()
        self._log_data.iSize = sizeof(_ADLPMLogDataOutput)

    def _malloc(self, size: int) -> int:
        buf = create_string_buffer(size)
        self._allocations.append(buf)
        return cast(buf, c_void_p).value

    def _ensure_init(self) -> bool:
        if self._initialized:
            return self._supported
        self._initialized = True
        try:
            self._adl = ctypes.WinDLL(self.dll_name)
            if not hasattr(self._adl, "ADL2_Main_Control_Create") or not hasattr(self._adl, "ADL2_New_QueryPMLogData_Get"):
                return False
            self._malloc_cb = ADL_MAIN_MALLOC_CALLBACK(self._malloc)
            res = self._adl.ADL2_Main_Control_Create(self._malloc_cb, 1, byref(self._ctx))
            if res != ADL_OK:
                return False
            self._supported = True
            return True
        except (OSError, AttributeError):
            self._supported = False
            return False

    def query_sensors(self, adapter_index: int = 0) -> GpuSensors:
        with self._lock:
            if not self._ensure_init():
                return GpuSensors()
            try:
                res = self._adl.ADL2_New_QueryPMLogData_Get(self._ctx, adapter_index, byref(self._log_data))
                if res != ADL_OK:
                    return GpuSensors()

                sensors = self._log_data.sensors
                edge = sensors[PMLOG_TEMPERATURE_EDGE]
                hotspot = sensors[PMLOG_TEMPERATURE_HOTSPOT]
                mem = sensors[PMLOG_TEMPERATURE_MEM]
                fan = sensors[PMLOG_FAN_RPM]

                return GpuSensors(
                    temp_edge=edge.iValue if edge.iSupported else None,
                    temp_hotspot=hotspot.iValue if hotspot.iSupported else None,
                    temp_mem=mem.iValue if mem.iSupported else None,
                    fan_rpm=fan.iValue if fan.iSupported else None,
                )
            except Exception:
                return GpuSensors()


_READER = AdlReader()


def get_gpu_sensors(adapter_index: int = 0) -> GpuSensors:
    """Convenience helper to query GPU edge/hotspot/mem temp and fan RPM."""
    return _READER.query_sensors(adapter_index)
