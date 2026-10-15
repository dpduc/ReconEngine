"""Stage timings and process memory, recorded in every run's manifest."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class StageTimer:
    """Wall-clock seconds per named stage; a stage entered twice accumulates."""

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + time.perf_counter() - start


@contextmanager
def nothing() -> Iterator[None]:
    yield


def performance(timer: StageTimer, started: float, cpu_started: float, *, units: int,
                input_bytes: int) -> dict[str, Any]:
    """A run's manifest `performance` block; `started` / `cpu_started` are its perf_counter / process_time."""
    return {
        "units": units,
        "input_bytes": input_bytes,
        "total_seconds": round(time.perf_counter() - started, 6),
        "cpu_seconds": round(time.process_time() - cpu_started, 6),
        "peak_rss_bytes": peak_rss_bytes(),
        "stages": {name: round(seconds, 6) for name, seconds in timer.stages.items()},
    }


def peak_rss_bytes() -> int:
    """Peak resident memory of this process so far (0 when the platform does not report it)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        current = ctypes.WinDLL("kernel32").GetCurrentProcess  # own handles: don't retype the shared windll
        current.restype = wintypes.HANDLE  # a 64-bit pseudo-handle; the default int restype truncates it
        info = ctypes.WinDLL("psapi").GetProcessMemoryInfo
        info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        ok = info(current(), ctypes.byref(counters), counters.cb)
        return int(counters.PeakWorkingSetSize) if ok else 0
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)  # Linux reports KiB
