"""Process and execution metrics collection for local workers (Milestone M3.5).

Tracks:
- Process CPU utilization (%)
- Process Resident Set Size (RSS in MB)
- Job execution duration percentiles (p50, p95)
- Pass rate (succeeded / finished)
- Active executions and recovery counts

Works cross-platform (Windows / Linux / macOS) without external dependencies.
"""

from __future__ import annotations

import collections
import ctypes
import os
import sys
import time
from typing import Any


def get_process_rss_bytes() -> int:
    """Returns resident set size (RSS) in bytes for the current process."""
    if sys.platform == "win32":
        try:

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
        except Exception:
            pass
        return 0

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux ru_maxrss is in KB, on Darwin in bytes
        if sys.platform == "darwin":
            return int(usage.ru_maxrss)
        return int(usage.ru_maxrss * 1024)
    except Exception:
        return 0


def compute_percentile(data: list[float], percentile: float) -> float:
    """Computes deterministic percentile (0.0 to 100.0) from a list of floats."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return round(sorted_data[f], 4)
    d0 = sorted_data[f] * (c - k)
    d1 = sorted_data[c] * (k - f)
    return round(d0 + d1, 4)


class WorkerMetricsTracker:
    """Thread-safe and async-safe metrics collector for worker observability."""

    def __init__(self, max_duration_history: int = 1000) -> None:
        self.max_duration_history = max_duration_history
        self._durations: collections.deque[float] = collections.deque(maxlen=max_duration_history)
        self._last_cpu_time = time.process_time()
        self._last_wall_time = time.time()
        self._succeeded: int = 0
        self._failed: int = 0
        self._paused: int = 0
        self._cancelled: int = 0
        self._recovered: int = 0

    def record_job_duration(self, duration_seconds: float) -> None:
        """Records completed job duration in seconds."""
        if duration_seconds >= 0:
            self._durations.append(duration_seconds)

    def record_job_outcome(self, outcome: str) -> None:
        """Records terminal outcome: 'succeeded', 'failed', 'paused', 'cancelled'."""
        outcome_lower = outcome.lower()
        if outcome_lower == "succeeded":
            self._succeeded += 1
        elif outcome_lower == "failed":
            self._failed += 1
        elif outcome_lower in ("paused", "paused_for_review"):
            self._paused += 1
        elif outcome_lower == "cancelled":
            self._cancelled += 1

    def record_recovered_jobs(self, count: int) -> None:
        """Increments count of recovered stale jobs."""
        if count > 0:
            self._recovered += count

    def get_cpu_percent(self) -> float:
        """Calculates process CPU utilization percentage since last call."""
        now_wall = time.time()
        now_cpu = time.process_time()

        wall_delta = now_wall - self._last_wall_time
        cpu_delta = now_cpu - self._last_cpu_time

        self._last_wall_time = now_wall
        self._last_cpu_time = now_cpu

        if wall_delta <= 0:
            return 0.0

        num_cpus = os.cpu_count() or 1
        percent = (cpu_delta / wall_delta / num_cpus) * 100.0
        return round(max(0.0, min(100.0, percent)), 2)

    def get_snapshot(self, active_jobs_count: int = 0) -> dict[str, Any]:
        """Returns a consolidated dictionary of current metrics."""
        rss_bytes = get_process_rss_bytes()
        rss_mb = round(rss_bytes / (1024 * 1024), 2)
        cpu_pct = self.get_cpu_percent()

        duration_list = list(self._durations)
        p50 = compute_percentile(duration_list, 50.0)
        p95 = compute_percentile(duration_list, 95.0)

        total_finished = self._succeeded + self._failed + self._paused + self._cancelled
        pass_rate = round((self._succeeded / total_finished), 4) if total_finished > 0 else 0.0

        return {
            "pid": os.getpid(),
            "active_jobs_count": active_jobs_count,
            "jobs_succeeded": self._succeeded,
            "jobs_failed": self._failed,
            "jobs_paused_for_review": self._paused,
            "jobs_cancelled": self._cancelled,
            "total_finished": total_finished,
            "pass_rate": pass_rate,
            "recovered_jobs_count": self._recovered,
            "cpu_percent": cpu_pct,
            "rss_bytes": rss_bytes,
            "rss_mb": rss_mb,
            "p50_duration_seconds": p50,
            "p95_duration_seconds": p95,
            "recorded_durations_count": len(duration_list),
        }
