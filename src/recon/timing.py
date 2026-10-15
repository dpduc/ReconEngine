"""Stage timings and process memory, recorded in every run's manifest."""

from __future__ import annotations

import os
import sys
import threading
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


class RunMeter:
    """Wall clock, CPU and peak memory of one run, for its manifest `performance` block.

    The process-lifetime peak cannot be reset, so a long-lived process (the app) would report an
    earlier, larger run's peak for every later run. The run's own peak is therefore:

    - the lifetime peak at the end, when it rose during the run (the new high was set by this run);
    - else the highest current RSS sampled every `interval` seconds on a background thread, which
      can miss a spike shorter than the interval.
    """

    def __init__(self, interval: float = 0.01) -> None:
        self.timer = StageTimer()
        self.interval = interval
        self._sampled = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="run-memory", daemon=True)

    def __enter__(self) -> RunMeter:
        self._process_peak_before = process_peak_rss_bytes()
        self._started, self._cpu_started = time.perf_counter(), time.process_time()
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _sample(self) -> None:
        self._sampled = max(self._sampled, current_rss_bytes())

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def peak_rss_bytes(self) -> int:
        """This run's peak resident memory so far (0 when the platform reports neither figure)."""
        self._sample()
        process_peak = process_peak_rss_bytes()
        return process_peak if process_peak > self._process_peak_before else self._sampled

    def performance(self, *, units: int, input_bytes: int) -> dict[str, Any]:
        return {
            "units": units,
            "input_bytes": input_bytes,
            "total_seconds": round(time.perf_counter() - self._started, 6),
            "cpu_seconds": round(time.process_time() - self._cpu_started, 6),
            "peak_rss_bytes": self.peak_rss_bytes(),
            "process_peak_rss_bytes": process_peak_rss_bytes(),
            "stages": {name: round(seconds, 6) for name, seconds in self.timer.stages.items()},
        }


def _windows_memory() -> Any:
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
    return counters if info(current(), ctypes.byref(counters), counters.cb) else None


def process_peak_rss_bytes() -> int:
    """Peak resident memory of this process so far (0 when the platform does not report it)."""
    if sys.platform == "win32":
        counters = _windows_memory()
        return int(counters.PeakWorkingSetSize) if counters else 0
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)  # Linux reports KiB


def current_rss_bytes() -> int:
    """Resident memory of this process now (0 when the platform does not report it, e.g. macOS)."""
    if sys.platform == "win32":
        counters = _windows_memory()
        return int(counters.WorkingSetSize) if counters else 0
    try:
        with open("/proc/self/statm") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0
