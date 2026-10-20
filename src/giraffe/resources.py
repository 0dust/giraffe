"""Best-effort process RAM measurements without optional dependencies."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from typing import Callable


SAMPLING = "about once per second; short spikes between samples can be missed"


def current_rss_bytes() -> int | None:
    try:
        if sys.platform.startswith("linux"):
            resident = int(Path("/proc/self/statm").read_text().split()[1])
            value = resident * os.sysconf("SC_PAGE_SIZE")
        elif sys.platform == "darwin":
            class TaskBasicInfo(ctypes.Structure):
                _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                            ("resident_size_max", ctypes.c_uint64), ("user_time", ctypes.c_int * 2),
                            ("system_time", ctypes.c_int * 2), ("policy", ctypes.c_int),
                            ("suspend_count", ctypes.c_int)]

            library = ctypes.CDLL(None)
            library.mach_task_self.restype = ctypes.c_uint
            library.task_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_uint)]
            library.task_info.restype = ctypes.c_int
            info = TaskBasicInfo()
            count = ctypes.c_uint(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_int))
            if library.task_info(library.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count)):
                return None
            value = info.resident_size
        else:
            return None
        return int(value) if value > 0 else None
    except Exception:
        return None


def peak_rss_bytes() -> int | None:
    try:
        import resource
        if sys.platform not in {"linux", "darwin"}:
            return None
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        value *= 1 if sys.platform == "darwin" else 1024
        return int(value) if value > 0 else None
    except Exception:
        return None


class PhaseSampler:
    def __init__(self, phase: str, *, interval: float = 1.0,
                 on_sample: Callable[[int | None], None] | None = None):
        self.phase = phase
        self.peaks: dict[str, int | None] = {phase: None}
        self.last_rss_bytes: int | None = None
        self.interval = interval
        self.on_sample = on_sample
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    def sample(self):
        with self._lock:
            value = current_rss_bytes()
            self.last_rss_bytes = value
            if value is not None:
                self.peaks[self.phase] = max(self.peaks.get(self.phase) or 0, value)
            callback = self.on_sample
            if callback:
                callback(value)
        return value

    def detach(self):
        with self._lock:
            self.on_sample = None

    def set_phase(self, phase: str):
        with self._lock:
            self.sample()
            self.phase = phase
            self.peaks.setdefault(phase, None)
            self.sample()

    def _poll(self):
        while not self._stop.wait(self.interval):
            self.sample()

    def __enter__(self):
        self.initial_rss_bytes = self.sample()
        self._thread = threading.Thread(target=self._poll, name="giraffe-ram", daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread:
            self._thread.join()
        self.sample()

    def __exit__(self, *_):
        self.close()
