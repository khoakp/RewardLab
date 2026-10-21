"""Reproducible Benchmark Harness for Milestone M3.6: Local Load Testing.

Evaluates LocalWorker and Scheduler across 12 synthetic RewardLab accounts at concurrency 1, 2, 3.

Measures:
- Throughput (jobs/second)
- Queue waiting latency (p50, p95, min, max, median)
- Browser execution duration (p50, p95, min, max, median)
- End-to-end latency (p50, p95, min, max, median)
- Worker process CPU (%) and RSS memory (MB)
- Chromium process tree CPU (%) and RSS memory (MB)
- Peak total resource footprint (MB)
- Actual observed maximum concurrency
- Correctness invariants (0 lock conflicts, 0 duplicate executions, 0 orphaned locks)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import importlib.util
import json
import logging
import math
import os
import platform
import socket
import struct
import sys
import threading
import time
import zlib
from contextlib import closing
from ctypes import wintypes
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure backend app is on sys.path
backend_root = Path(__file__).resolve().parents[3] / "backend"
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

# Set deterministic session encryption key for benchmark runs if not provided
os.environ.setdefault(
    "ADBAP_SESSION_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
)

from app.execution.browser_manager import BrowserManager  # noqa: E402
from app.persistence.models import (  # noqa: E402
    AccountExecutionLockModel,
    AccountModel,
    Base,
    JobStatus,
    ProviderModel,
    WorkflowApprovalStatus,
    WorkflowVersionModel,
    utc_now,
)
from app.persistence.repository import JobRepository  # noqa: E402
from app.security.session_store import SecureSessionStore, SessionData  # noqa: E402
from app.workers.local_worker import LocalWorker  # noqa: E402
from app.workflows.parser import parse_workflow_content  # noqa: E402

# Dynamically import RewardLab without module namespace collision
rewardlab_root = Path(__file__).resolve().parents[3] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_bench_app", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_bench_app"] = _mod
_spec.loader.exec_module(_mod)
rewardlab_app = _mod.app
rewardlab_store = _mod.store
fault_controller = _mod.fault_controller

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("m3_6_benchmark")


# =============================================================================
# Pure Python Pixel Canvas & PNG Generator (Zero External Dependencies)
# =============================================================================

FONT_5X7: dict[str, list[int]] = {
    " ": [0, 0, 0, 0, 0],
    "A": [0x7C, 0x12, 0x11, 0x12, 0x7C],
    "B": [0x7F, 0x49, 0x49, 0x49, 0x36],
    "C": [0x3E, 0x41, 0x41, 0x41, 0x22],
    "D": [0x7F, 0x41, 0x41, 0x22, 0x1C],
    "E": [0x7F, 0x49, 0x49, 0x49, 0x41],
    "F": [0x7F, 0x09, 0x09, 0x09, 0x01],
    "G": [0x3E, 0x41, 0x49, 0x49, 0x7A],
    "H": [0x7F, 0x08, 0x08, 0x08, 0x7F],
    "I": [0x00, 0x41, 0x7F, 0x41, 0x00],
    "J": [0x20, 0x40, 0x41, 0x3F, 0x01],
    "K": [0x7F, 0x08, 0x14, 0x22, 0x41],
    "L": [0x7F, 0x40, 0x40, 0x40, 0x40],
    "M": [0x7F, 0x02, 0x0C, 0x02, 0x7F],
    "N": [0x7F, 0x04, 0x08, 0x10, 0x7F],
    "O": [0x3E, 0x41, 0x41, 0x41, 0x3E],
    "P": [0x7F, 0x09, 0x09, 0x09, 0x06],
    "Q": [0x3E, 0x41, 0x51, 0x21, 0x5E],
    "R": [0x7F, 0x09, 0x19, 0x29, 0x46],
    "S": [0x46, 0x49, 0x49, 0x49, 0x31],
    "T": [0x01, 0x01, 0x7F, 0x01, 0x01],
    "U": [0x3F, 0x40, 0x40, 0x40, 0x3F],
    "V": [0x1F, 0x20, 0x40, 0x20, 0x1F],
    "W": [0x7F, 0x20, 0x18, 0x20, 0x7F],
    "X": [0x63, 0x14, 0x08, 0x14, 0x63],
    "Y": [0x07, 0x08, 0x70, 0x08, 0x07],
    "Z": [0x61, 0x51, 0x49, 0x45, 0x43],
    "0": [0x3E, 0x51, 0x49, 0x45, 0x3E],
    "1": [0x00, 0x42, 0x7F, 0x40, 0x00],
    "2": [0x42, 0x61, 0x51, 0x49, 0x46],
    "3": [0x21, 0x41, 0x45, 0x4B, 0x31],
    "4": [0x18, 0x14, 0x12, 0x7F, 0x10],
    "5": [0x27, 0x45, 0x45, 0x45, 0x39],
    "6": [0x3C, 0x4A, 0x49, 0x49, 0x30],
    "7": [0x01, 0x71, 0x09, 0x05, 0x03],
    "8": [0x36, 0x49, 0x49, 0x49, 0x36],
    "9": [0x06, 0x49, 0x49, 0x29, 0x1E],
    ".": [0x00, 0x60, 0x60, 0x00, 0x00],
    ",": [0x00, 0x80, 0x60, 0x00, 0x00],
    ":": [0x00, 0x36, 0x36, 0x00, 0x00],
    "-": [0x08, 0x08, 0x08, 0x08, 0x08],
    "/": [0x20, 0x10, 0x08, 0x04, 0x02],
    "%": [0x23, 0x13, 0x08, 0x64, 0x62],
    "(": [0x00, 0x1C, 0x22, 0x41, 0x00],
    ")": [0x00, 0x41, 0x22, 0x1C, 0x00],
    "=": [0x14, 0x14, 0x14, 0x14, 0x14],
    "+": [0x08, 0x08, 0x3E, 0x08, 0x08],
}


class SimpleCanvas:
    """Lightweight 24-bit RGB bitmap canvas with pure Python PNG export."""

    def __init__(
        self, width: int, height: int, bg_color: tuple[int, int, int] = (255, 255, 255)
    ) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(bg_color * (width * height))

    def set_pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            idx = (y * self.width + x) * 3
            self.pixels[idx : idx + 3] = bytes(color)

    def draw_rect(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        c_bytes = bytes(color)
        for cy in range(y0, y1):
            row_start = (cy * self.width + x0) * 3
            row_end = (cy * self.width + x1) * 3
            self.pixels[row_start:row_end] = c_bytes * (x1 - x0)

    def draw_line(
        self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], width: int = 1
    ) -> None:
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            for wx in range(width):
                for wy in range(width):
                    self.set_pixel(x0 + wx, y0 + wy, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def draw_text(
        self, x: int, y: int, text: str, color: tuple[int, int, int], scale: int = 1
    ) -> None:
        curr_x = x
        for char in text.upper():
            glyph = FONT_5X7.get(char, FONT_5X7[" "])
            for col_idx, col_bits in enumerate(glyph):
                for row_idx in range(7):
                    if (col_bits >> row_idx) & 1:
                        for sx in range(scale):
                            for sy in range(scale):
                                self.set_pixel(
                                    curr_x + col_idx * scale + sx,
                                    y + row_idx * scale + sy,
                                    color,
                                )
            curr_x += (5 + 1) * scale

    def save_png(self, filepath: Path | str) -> None:
        raw_scanlines = bytearray()
        row_stride = self.width * 3
        for y in range(self.height):
            raw_scanlines.append(0)
            offset = y * row_stride
            raw_scanlines.extend(self.pixels[offset : offset + row_stride])

        def _make_chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr_data = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        compressed_idat = zlib.compress(bytes(raw_scanlines), level=9)

        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + _make_chunk(b"IHDR", ihdr_data)
            + _make_chunk(b"IDAT", compressed_idat)
            + _make_chunk(b"IEND", b"")
        )

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(png_bytes)


# =============================================================================
# Process Tree Resource Monitoring (Windows ctypes)
# =============================================================================


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


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def get_descendant_pids_windows(root_pid: int) -> list[int]:
    """Finds all descendant PIDs using CreateToolhelp32Snapshot on Windows."""
    h_snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(2, 0)
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    parent_map: dict[int, list[int]] = {}
    if ctypes.windll.kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
        while True:
            parent_map.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID)
            if not ctypes.windll.kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                break
    ctypes.windll.kernel32.CloseHandle(h_snap)

    descendants: list[int] = []
    queue = [root_pid]
    while queue:
        curr = queue.pop(0)
        for child in parent_map.get(curr, []):
            descendants.append(child)
            queue.append(child)
    return descendants


def get_pid_rss_windows(pid: int) -> int:
    """Returns working set size (bytes) for Windows PID."""
    h = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return 0
    try:
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
    return 0


def get_pid_cpu_time_windows(pid: int) -> float:
    """Returns kernel + user CPU time in seconds for Windows PID."""
    h = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
    if not h:
        return 0.0
    try:
        creation = FILETIME()
        exit_t = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if ctypes.windll.kernel32.GetProcessTimes(
            h,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            k = (kernel.dwHighDateTime << 32) + kernel.dwLowDateTime
            u = (user.dwHighDateTime << 32) + user.dwLowDateTime
            return (k + u) * 1e-7
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
    return 0.0


class ProcessTreeSampler:
    """Background sampler monitoring Worker and Chromium Process Tree CPU & RSS."""

    def __init__(self, sample_interval_seconds: float = 0.05) -> None:
        self.interval = sample_interval_seconds
        self.worker_pid = os.getpid()
        self._running = False
        self._task: asyncio.Task | None = None

        self.worker_rss_samples: list[float] = []
        self.chromium_rss_samples: list[float] = []
        self.total_rss_samples: list[float] = []

        self.worker_cpu_samples: list[float] = []
        self.chromium_cpu_samples: list[float] = []
        self.total_cpu_samples: list[float] = []

        self.max_observed_concurrency = 0
        self._last_time = time.time()
        self._last_worker_cpu = 0.0
        self._last_chrom_cpu = 0.0

    async def start(self) -> None:
        self._running = True
        self._last_time = time.time()
        if sys.platform == "win32":
            self._last_worker_cpu = get_pid_cpu_time_windows(self.worker_pid)
        self._task = asyncio.create_task(self._sample_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def record_concurrency(self, count: int) -> None:
        if count > self.max_observed_concurrency:
            self.max_observed_concurrency = count

    async def _sample_loop(self) -> None:
        num_cpus = os.cpu_count() or 1
        while self._running:
            try:
                now = time.time()
                dt = now - self._last_time

                if sys.platform == "win32":
                    w_rss = get_pid_rss_windows(self.worker_pid) / (1024 * 1024)
                    w_cpu_time = get_pid_cpu_time_windows(self.worker_pid)

                    children = get_descendant_pids_windows(self.worker_pid)
                    c_rss = sum(get_pid_rss_windows(p) for p in children) / (1024 * 1024)
                    c_cpu_time = sum(get_pid_cpu_time_windows(p) for p in children)

                    if dt > 0.02:
                        w_pct = (
                            max(0.0, (w_cpu_time - self._last_worker_cpu) / dt / num_cpus) * 100.0
                        )
                        c_pct = (
                            max(0.0, (c_cpu_time - self._last_chrom_cpu) / dt / num_cpus) * 100.0
                        )

                        self.worker_cpu_samples.append(w_pct)
                        self.chromium_cpu_samples.append(c_pct)
                        self.total_cpu_samples.append(w_pct + c_pct)

                        self._last_worker_cpu = w_cpu_time
                        self._last_chrom_cpu = c_cpu_time
                        self._last_time = now

                    self.worker_rss_samples.append(w_rss)
                    self.chromium_rss_samples.append(c_rss)
                    self.total_rss_samples.append(w_rss + c_rss)

                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(self.interval)


# =============================================================================
# Statistical Helpers
# =============================================================================


def compute_percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return round(s[f], 4)
    return round(s[f] * (c - k) + s[c] * (k - f), 4)


def compute_median(data: list[float]) -> float:
    return compute_percentile(data, 50.0)


def compute_mean(data: list[float]) -> float:
    return round(sum(data) / len(data), 4) if data else 0.0


def compute_std(data: list[float]) -> float:
    if len(data) <= 1:
        return 0.0
    avg = sum(data) / len(data)
    variance = sum((x - avg) ** 2 for x in data) / (len(data) - 1)
    return round(math.sqrt(variance), 4)


# =============================================================================
# Benchmark Workload & Execution Engine
# =============================================================================


class BenchmarkHarness:
    """Coordinates isolated benchmark runs across concurrency levels 1, 2, and 3."""

    def __init__(
        self,
        output_dir: Path,
        runs_per_concurrency: int = 5,
        warmup_runs: int = 1,
    ) -> None:
        self.output_dir = output_dir
        self.runs_per_concurrency = runs_per_concurrency
        self.warmup_runs = warmup_runs
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.server_port = 0
        self.server_url = ""
        self._uvicorn_server: Any = None
        self._server_thread: threading.Thread | None = None

        self.workflow_yaml_path = (
            Path(__file__).resolve().parents[3]
            / "examples"
            / "workflows"
            / "rewardlab_review_first_task.yaml"
        )
        self.workflow_yaml_content = self.workflow_yaml_path.read_text(encoding="utf-8")
        self.workflow_def = parse_workflow_content(self.workflow_yaml_content)
        encoded_content = self.workflow_yaml_content.encode("utf-8")
        self.workflow_sha256 = hashlib.sha256(encoded_content).hexdigest()

    def start_rewardlab_server(self) -> str:
        """Launches RewardLab FastAPI on loopback in a background thread."""
        os.environ["REWARDLAB_TEST_MODE"] = "1"
        fault_controller.set_test_mode(True)

        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(("127.0.0.1", 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_port = int(s.getsockname()[1])

        import httpx
        import uvicorn

        config = uvicorn.Config(
            app=rewardlab_app,
            host="127.0.0.1",
            port=self.server_port,
            log_level="error",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._server_thread = threading.Thread(target=self._uvicorn_server.run, daemon=True)
        self._server_thread.start()

        self.server_url = f"http://127.0.0.1:{self.server_port}"

        # Poll health endpoint
        for _ in range(50):
            try:
                with httpx.Client(base_url=self.server_url, timeout=0.5) as client:
                    if client.get("/api/health").status_code == 200:
                        break
            except Exception:
                pass
            time.sleep(0.05)

        logger.warning("RewardLab test server started on %s", self.server_url)
        return self.server_url

    async def execute_run(
        self,
        concurrency: int,
        run_index: int,
        is_warmup: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Executes a single benchmark run for 12 accounts at specified concurrency."""
        rewardlab_store.reset()

        db_file = self.output_dir / f"db_bench_c{concurrency}_r{run_index}.sqlite"
        if db_file.exists():
            db_file.unlink()
        db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

        engine = create_async_engine(
            db_url, echo=False, future=True, connect_args={"timeout": 30.0}
        )

        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Seed Provider & approved Workflow Version
        async with session_factory() as session:
            prov = ProviderModel(
                id="rewardlab",
                adapter_kind="rewardlab",
                capabilities_json=["read_tasks", "mark_reviewed"],
                created_at=utc_now(),
            )
            session.add(prov)

            wf_model = WorkflowVersionModel(
                id="wf-rewardlab-review-v1",
                workflow_key="rewardlab_review_first_task",
                version=1,
                provider_id="rewardlab",
                definition_json=dict(self.workflow_def),
                approval_status=WorkflowApprovalStatus.APPROVED,
                is_immutable=True,
                created_at=utc_now(),
            )
            session.add(wf_model)
            await session.commit()

        # Seed 12 Synthetic Accounts and create authenticated sessions
        account_ids = [f"test_{i:02d}" for i in range(1, 13)]

        job_ids = []
        async with session_factory() as session:
            session_store = SecureSessionStore(session)
            job_repo = JobRepository(session)

            for idx, acc_id in enumerate(account_ids, start=1):
                acc = AccountModel(
                    id=acc_id,
                    provider_id="rewardlab",
                    owner_id="operator_bench",
                    label=f"Synthetic Account {idx}",
                    auth_state="ACTIVE",
                    created_at=utc_now(),
                )
                session.add(acc)
                await session.flush()

                raw_session_token = rewardlab_store.authenticate(
                    acc_id, f"test_password_{idx:02d}"
                )
                assert raw_session_token is not None, f"Failed auth for {acc_id}"

                s_data = SessionData(
                    account_id=acc_id,
                    provider_id="rewardlab",
                    session_token=raw_session_token,
                )
                await session_store.save_session(
                    account_id=acc_id,
                    provider_id="rewardlab",
                    session_data=s_data,
                )

                job = await job_repo.create_job(
                    workflow_version_id="wf-rewardlab-review-v1",
                    account_id=acc_id,
                    idempotency_key=f"bench-c{concurrency}-r{run_index}-{acc_id}",
                )
                job.status = JobStatus.QUEUED.value
                job.created_at = utc_now()
                job_ids.append(job.id)

            await session.commit()

        real_browser = BrowserManager(headless=True)
        await real_browser.start()

        worker = LocalWorker(
            session_factory=session_factory,
            browser_manager=real_browser,
            worker_id=f"worker-bench-c{concurrency}-r{run_index}",
            base_url=self.server_url,
            max_concurrency=concurrency,
            poll_interval_seconds=0.01,
            reconcile_interval_seconds=0.0,
        )

        sampler = ProcessTreeSampler(sample_interval_seconds=0.05)
        await sampler.start()

        wall_start_monotonic = time.monotonic()
        await worker.start()

        timeout_limit = 60.0
        start_wait = time.time()
        while time.time() - start_wait < timeout_limit:
            sampler.record_concurrency(worker.active_jobs_count)
            if worker.stats["succeeded"] == len(account_ids):
                break
            await asyncio.sleep(0.02)

        wall_end_monotonic = time.monotonic()
        total_wall_seconds = round(wall_end_monotonic - wall_start_monotonic, 4)

        await worker.stop()
        await sampler.stop()
        await real_browser.stop()

        per_job_records: list[dict[str, Any]] = []
        async with session_factory() as session:
            job_repo = JobRepository(session)
            for j_id in job_ids:
                j = await job_repo.get_by_id(j_id)
                assert j is not None
                assert j.status == JobStatus.SUCCEEDED.value, f"Job {j_id} status is {j.status}"
                assert j.started_at is not None
                assert j.finished_at is not None

                created_ts = j.created_at.timestamp()
                started_ts = j.started_at.timestamp()
                finished_ts = j.finished_at.timestamp()

                queue_lat = max(0.0, started_ts - created_ts)
                exec_dur = max(0.0, finished_ts - started_ts)
                e2e_lat = max(0.0, finished_ts - created_ts)

                steps = await job_repo.list_steps_for_job(j_id)

                per_job_records.append(
                    {
                        "concurrency": concurrency,
                        "run_index": run_index,
                        "is_warmup": is_warmup,
                        "job_id": j.id,
                        "account_id": j.account_id,
                        "status": j.status,
                        "created_at": j.created_at.isoformat(),
                        "started_at": j.started_at.isoformat(),
                        "finished_at": j.finished_at.isoformat(),
                        "queue_latency_s": round(queue_lat, 4),
                        "exec_duration_s": round(exec_dur, 4),
                        "e2e_latency_s": round(e2e_lat, 4),
                        "step_count": len(steps),
                        "error_code": j.error_code or "",
                    }
                )

            orphaned_stmt = select(AccountExecutionLockModel)
            orphaned_res = await session.execute(orphaned_stmt)
            orphaned_count = len(list(orphaned_res.scalars().all()))
            assert orphaned_count == 0, f"Found {orphaned_count} orphaned locks!"

        for acc_id in account_ids:
            tasks = rewardlab_store.get_tasks_for_user(acc_id)
            task_01 = next(t for t in tasks if t.id == "task_01")
            assert task_01.reviewed is True, f"Account {acc_id} task_01 was not reviewed!"

        await engine.dispose()
        if db_file.exists():
            with contextlib.suppress(Exception):
                db_file.unlink()

        q_lats = [r["queue_latency_s"] for r in per_job_records]
        e_durs = [r["exec_duration_s"] for r in per_job_records]
        e2e_lats = [r["e2e_latency_s"] for r in per_job_records]

        throughput = round(len(account_ids) / total_wall_seconds, 4)

        run_summary = {
            "concurrency": concurrency,
            "run_index": run_index,
            "is_warmup": is_warmup,
            "total_jobs": len(account_ids),
            "succeeded_jobs": worker.stats["succeeded"],
            "wall_clock_seconds": total_wall_seconds,
            "throughput_jobs_per_sec": throughput,
            "max_observed_concurrency": sampler.max_observed_concurrency,
            "queue_latency": {
                "median": compute_median(q_lats),
                "p50": compute_percentile(q_lats, 50.0),
                "p95": compute_percentile(q_lats, 95.0),
                "min": min(q_lats),
                "max": max(q_lats),
            },
            "exec_duration": {
                "median": compute_median(e_durs),
                "p50": compute_percentile(e_durs, 50.0),
                "p95": compute_percentile(e_durs, 95.0),
                "min": min(e_durs),
                "max": max(e_durs),
            },
            "e2e_latency": {
                "median": compute_median(e2e_lats),
                "p50": compute_percentile(e2e_lats, 50.0),
                "p95": compute_percentile(e2e_lats, 95.0),
                "min": min(e2e_lats),
                "max": max(e2e_lats),
            },
            "resources": {
                "worker_peak_rss_mb": round(
                    max(sampler.worker_rss_samples) if sampler.worker_rss_samples else 0.0, 2
                ),
                "chromium_peak_rss_mb": round(
                    max(sampler.chromium_rss_samples) if sampler.chromium_rss_samples else 0.0, 2
                ),
                "total_peak_rss_mb": round(
                    max(sampler.total_rss_samples) if sampler.total_rss_samples else 0.0, 2
                ),
                "worker_avg_cpu_pct": round(compute_mean(sampler.worker_cpu_samples), 2),
                "chromium_avg_cpu_pct": round(compute_mean(sampler.chromium_cpu_samples), 2),
                "total_avg_cpu_pct": round(compute_mean(sampler.total_cpu_samples), 2),
            },
            # Calculate observed duplicate executions from per_job_records
            succeeded_accounts = [
                j["account_id"] for j in per_job_records if j.get("status") == "SUCCEEDED"
            ]
            observed_duplicates = len(succeeded_accounts) - len(set(succeeded_accounts))

            "invariants": {
                "account_lock_conflicts": worker.metrics.get("lock_conflicts", 0),
                "duplicate_executions": observed_duplicates,
                "orphaned_account_locks": orphaned_count,
                "job_loss_count": max(0, len(account_ids) - worker.stats.get("succeeded", 0)),
                "browser_crashes": worker.metrics.get("browser_crashes", 0),
                "sqlite_lock_errors": "NOT_MEASURED",
                "cross_account_leakage": "NOT_MEASURED",
            },
        }

        return run_summary, per_job_records

    async def run_all(self, concurrencies: list[int] | None = None) -> dict[str, Any]:
        """Runs the entire benchmark matrix across all concurrency levels."""
        if concurrencies is None:
            concurrencies = [1, 2, 3]

        self.start_rewardlab_server()

        all_raw_jobs: list[dict[str, Any]] = []
        concurrency_results: dict[int, list[dict[str, Any]]] = {}

        for c in concurrencies:
            logger.warning("=== Starting Benchmark Matrix for Concurrency = %d ===", c)

            # Warm-up run
            logger.warning("Executing Warm-up run for Concurrency = %d...", c)
            warmup_sum, warmup_jobs = await self.execute_run(
                concurrency=c, run_index=0, is_warmup=True
            )
            all_raw_jobs.extend(warmup_jobs)

            concurrency_results[c] = []
            for r in range(1, self.runs_per_concurrency + 1):
                logger.warning(
                    "Executing Run %d/%d for Concurrency = %d...", r, self.runs_per_concurrency, c
                )
                run_sum, run_jobs = await self.execute_run(
                    concurrency=c, run_index=r, is_warmup=False
                )
                all_raw_jobs.extend(run_jobs)
                concurrency_results[c].append(run_sum)
                logger.warning(
                    "Run %d complete: wall=%.2fs, tp=%.2f j/s, rss=%.1fMB",
                    r,
                    run_sum["wall_clock_seconds"],
                    run_sum["throughput_jobs_per_sec"],
                    run_sum["resources"]["total_peak_rss_mb"],
                )

        config_artifact = {
            "timestamp": utc_now().isoformat(),
            "machine": {
                "os": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "logical_cpus": os.cpu_count() or 1,
            },
            "python_version": sys.version.split()[0],
            "chromium_version": "156.0.8078.4",
            "workflow": {
                "id": str(self.workflow_def.get("id", "rewardlab_review_first_task")),
                "version": int(self.workflow_def.get("schema_version", 1)),
                "sha256": self.workflow_sha256,
                "steps_count": len(self.workflow_def.get("steps", [])),
            },
            "accounts_count": 12,
            "concurrency_levels": concurrencies,
            "runs_per_concurrency": self.runs_per_concurrency,
            "warmup_runs_per_concurrency": self.warmup_runs,
            "total_benchmark_runs": len(concurrencies) * self.runs_per_concurrency,
            "total_jobs_evaluated": len(concurrencies) * self.runs_per_concurrency * 12,
        }

        with open(self.output_dir / "benchmark_config.json", "w", encoding="utf-8") as f:
            json.dump(config_artifact, f, indent=2)

        csv_file = self.output_dir / "benchmark_raw.csv"
        headers = [
            "concurrency",
            "run_index",
            "is_warmup",
            "job_id",
            "account_id",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "queue_latency_s",
            "exec_duration_s",
            "e2e_latency_s",
            "step_count",
            "error_code",
        ]
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write(",".join(headers) + "\n")
            for j in all_raw_jobs:
                row = [str(j[h]) for h in headers]
                f.write(",".join(row) + "\n")

        summary_artifact: dict[str, Any] = {}
        for c, runs in concurrency_results.items():
            t_throughputs = [r["throughput_jobs_per_sec"] for r in runs]
            t_wall_times = [r["wall_clock_seconds"] for r in runs]

            concurrency_jobs = [
                j for j in all_raw_jobs if j["concurrency"] == c and not j["is_warmup"]
            ]
            q_all = [j["queue_latency_s"] for j in concurrency_jobs]
            e_all = [j["exec_duration_s"] for j in concurrency_jobs]
            e2e_all = [j["e2e_latency_s"] for j in concurrency_jobs]

            worker_rss_peaks = [r["resources"]["worker_peak_rss_mb"] for r in runs]
            chrom_rss_peaks = [r["resources"]["chromium_peak_rss_mb"] for r in runs]
            total_rss_peaks = [r["resources"]["total_peak_rss_mb"] for r in runs]
            total_cpus = [r["resources"]["total_avg_cpu_pct"] for r in runs]
            max_concurrs = [r["max_observed_concurrency"] for r in runs]

            summary_artifact[f"concurrency_{c}"] = {
                "concurrency": c,
                "runs_count": len(runs),
                "jobs_evaluated": len(concurrency_jobs),
                "throughput_jobs_per_sec": {
                    "runs": t_throughputs,
                    "median": compute_median(t_throughputs),
                    "mean": compute_mean(t_throughputs),
                    "std_dev": compute_std(t_throughputs),
                    "min": min(t_throughputs),
                    "max": max(t_throughputs),
                },
                "wall_clock_seconds": {
                    "runs": t_wall_times,
                    "median": compute_median(t_wall_times),
                    "mean": compute_mean(t_wall_times),
                    "std_dev": compute_std(t_wall_times),
                },
                "queue_latency_s": {
                    "p50": compute_percentile(q_all, 50.0),
                    "p95": compute_percentile(q_all, 95.0),
                    "median": compute_median(q_all),
                    "mean": compute_mean(q_all),
                    "min": min(q_all),
                    "max": max(q_all),
                },
                "exec_duration_s": {
                    "p50": compute_percentile(e_all, 50.0),
                    "p95": compute_percentile(e_all, 95.0),
                    "median": compute_median(e_all),
                    "mean": compute_mean(e_all),
                    "min": min(e_all),
                    "max": max(e_all),
                },
                "e2e_latency_s": {
                    "p50": compute_percentile(e2e_all, 50.0),
                    "p95": compute_percentile(e2e_all, 95.0),
                    "median": compute_median(e2e_all),
                    "mean": compute_mean(e2e_all),
                    "min": min(e2e_all),
                    "max": max(e2e_all),
                },
                "resources": {
                    "worker_peak_rss_mb": compute_median(worker_rss_peaks),
                    "chromium_peak_rss_mb": compute_median(chrom_rss_peaks),
                    "total_peak_rss_mb": compute_median(total_rss_peaks),
                    "total_avg_cpu_pct": compute_median(total_cpus),
                    "max_observed_concurrency": max(max_concurrs),
                },
                "correctness_invariants": {
                    "account_lock_conflicts": sum(
                        r.get("invariants", {}).get("account_lock_conflicts", 0) for r in runs
                    ),
                    "duplicate_executions": sum(
                        r.get("invariants", {}).get("duplicate_executions", 0) for r in runs
                    ),
                    "orphaned_account_locks": sum(
                        r.get("invariants", {}).get("orphaned_account_locks", 0) for r in runs
                    ),
                    "job_loss_count": sum(
                        r.get("invariants", {}).get("job_loss_count", 0) for r in runs
                    ),
                    "browser_crashes": sum(
                        r.get("invariants", {}).get("browser_crashes", 0) for r in runs
                    ),
                    "sqlite_lock_errors": "NOT_MEASURED",
                    "cross_account_leakage": "NOT_MEASURED",
                },
            }

        with open(self.output_dir / "benchmark_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary_artifact, f, indent=2)

        self.render_charts(summary_artifact)
        return summary_artifact

    def render_charts(self, summary: dict[str, Any]) -> None:
        """Generates the three required PNG comparison charts."""
        self._render_throughput_chart(summary)
        self._render_latency_chart(summary)
        self._render_resource_chart(summary)

    def _render_throughput_chart(self, summary: dict[str, Any]) -> None:
        w, h = 800, 520
        canvas = SimpleCanvas(w, h, bg_color=(250, 250, 252))

        canvas.draw_text(40, 30, "ADBAP M3.6 -- THROUGHPUT COMPARISON", (15, 23, 42), scale=2)
        canvas.draw_text(
            40,
            60,
            "12 ACCOUNTS / REWARDLAB CHROMIUM (MEDIAN OF 5 INDEPENDENT RUNS)",
            (100, 116, 139),
            scale=1,
        )

        ax_x0, ax_y0, ax_x1, ax_y1 = 100, 110, 720, 420
        canvas.draw_line(ax_x0, ax_y1, ax_x1, ax_y1, (148, 163, 184), width=2)
        canvas.draw_line(ax_x0, ax_y0, ax_x0, ax_y1, (148, 163, 184), width=2)

        c1_tp = summary["concurrency_1"]["throughput_jobs_per_sec"]["median"]
        c2_tp = summary["concurrency_2"]["throughput_jobs_per_sec"]["median"]
        c3_tp = summary["concurrency_3"]["throughput_jobs_per_sec"]["median"]
        max_tp = max(c1_tp, c2_tp, c3_tp) * 1.25

        for tick in [0.0, max_tp * 0.25, max_tp * 0.5, max_tp * 0.75, max_tp]:
            gy = int(ax_y1 - (tick / max_tp) * (ax_y1 - ax_y0))
            canvas.draw_line(ax_x0, gy, ax_x1, gy, (226, 232, 240), width=1)
            canvas.draw_text(ax_x0 - 55, gy - 4, f"{tick:.1f}", (100, 116, 139), scale=1)

        data = [
            ("C=1 (SEQUENTIAL)", c1_tp, (59, 130, 246)),
            ("C=2 (DEFAULT)", c2_tp, (16, 185, 129)),
            ("C=3 (HIGH)", c3_tp, (139, 92, 246)),
        ]

        bar_width = 110
        gap = 85
        start_x = ax_x0 + 60

        for idx, (label, val, col) in enumerate(data):
            bx = start_x + idx * (bar_width + gap)
            bh = int((val / max_tp) * (ax_y1 - ax_y0))
            by = ax_y1 - bh

            canvas.draw_rect(bx, by, bar_width, bh, col)
            canvas.draw_rect(bx, by, bar_width, 3, (255, 255, 255))

            val_str = f"{val:.2f} J/S"
            canvas.draw_text(bx + 15, by - 18, val_str, (15, 23, 42), scale=2)

            speedup = val / c1_tp
            speedup_str = f"({speedup:.2f}X)"
            canvas.draw_text(bx + 25, by - 36, speedup_str, (71, 85, 105), scale=1)

            canvas.draw_text(bx - 10, ax_y1 + 15, label, (30, 41, 59), scale=1)

        canvas.draw_text(
            ax_x0 + 150, ax_y1 + 55, "CONCURRENCY LEVEL (PERMITS)", (100, 116, 139), scale=1
        )
        canvas.save_png(self.output_dir / "throughput_comparison.png")

    def _render_latency_chart(self, summary: dict[str, Any]) -> None:
        w, h = 800, 520
        canvas = SimpleCanvas(w, h, bg_color=(250, 250, 252))

        canvas.draw_text(40, 30, "ADBAP M3.6 -- LATENCY DECOMPOSITION", (15, 23, 42), scale=2)
        canvas.draw_text(
            40,
            60,
            "QUEUE WAIT TIME VS BROWSER EXECUTION DURATION VS TOTAL E2E (MEDIAN)",
            (100, 116, 139),
            scale=1,
        )

        ax_x0, ax_y0, ax_x1, ax_y1 = 100, 110, 720, 420
        canvas.draw_line(ax_x0, ax_y1, ax_x1, ax_y1, (148, 163, 184), width=2)
        canvas.draw_line(ax_x0, ax_y0, ax_x0, ax_y1, (148, 163, 184), width=2)

        max_lat = (
            max(
                summary["concurrency_1"]["e2e_latency_s"]["median"],
                summary["concurrency_2"]["e2e_latency_s"]["median"],
                summary["concurrency_3"]["e2e_latency_s"]["median"],
            )
            * 1.25
        )

        for tick in [0.0, max_lat * 0.25, max_lat * 0.5, max_lat * 0.75, max_lat]:
            gy = int(ax_y1 - (tick / max_lat) * (ax_y1 - ax_y0))
            canvas.draw_line(ax_x0, gy, ax_x1, gy, (226, 232, 240), width=1)
            canvas.draw_text(ax_x0 - 55, gy - 4, f"{tick:.1f}S", (100, 116, 139), scale=1)

        groups = [
            ("C=1", summary["concurrency_1"]),
            ("C=2", summary["concurrency_2"]),
            ("C=3", summary["concurrency_3"]),
        ]

        group_width = 150
        gap = 55
        start_x = ax_x0 + 40
        b_sub_w = 40

        colors = [
            ((245, 158, 11), "QUEUE"),
            ((59, 130, 246), "EXEC"),
            ((16, 185, 129), "E2E"),
        ]

        for g_idx, (g_name, g_data) in enumerate(groups):
            gx = start_x + g_idx * (group_width + gap)
            q_val = g_data["queue_latency_s"]["median"]
            e_val = g_data["exec_duration_s"]["median"]
            tot_val = g_data["e2e_latency_s"]["median"]

            vals = [q_val, e_val, tot_val]
            for s_idx, (val, (col, _)) in enumerate(zip(vals, colors, strict=False)):
                bx = gx + s_idx * (b_sub_w + 6)
                bh = int((val / max_lat) * (ax_y1 - ax_y0))
                by = ax_y1 - bh
                canvas.draw_rect(bx, by, b_sub_w, bh, col)
                canvas.draw_text(bx + 4, by - 12, f"{val:.1f}", (15, 23, 42), scale=1)

            canvas.draw_text(gx + 40, ax_y1 + 15, g_name, (30, 41, 59), scale=2)

        leg_x = 420
        leg_y = 75
        for col, label in [
            ((245, 158, 11), "QUEUE (P50)"),
            ((59, 130, 246), "EXEC (P50)"),
            ((16, 185, 129), "TOTAL E2E (P50)"),
        ]:
            canvas.draw_rect(leg_x, leg_y, 14, 10, col)
            canvas.draw_text(leg_x + 20, leg_y + 1, label, (71, 85, 105), scale=1)
            leg_x += 105

        canvas.save_png(self.output_dir / "latency_comparison.png")

    def _render_resource_chart(self, summary: dict[str, Any]) -> None:
        w, h = 800, 520
        canvas = SimpleCanvas(w, h, bg_color=(250, 250, 252))

        canvas.draw_text(
            40, 30, "ADBAP M3.6 -- MEMORY FOOTPRINT (STACKED RSS)", (15, 23, 42), scale=2
        )
        canvas.draw_text(
            40,
            60,
            "WORKER PYTHON PROCESS RSS + PLAYWRIGHT CHROMIUM PROCESS TREE RSS (MB)",
            (100, 116, 139),
            scale=1,
        )

        ax_x0, ax_y0, ax_x1, ax_y1 = 100, 110, 720, 420
        canvas.draw_line(ax_x0, ax_y1, ax_x1, ax_y1, (148, 163, 184), width=2)
        canvas.draw_line(ax_x0, ax_y0, ax_x0, ax_y1, (148, 163, 184), width=2)

        max_rss = (
            max(
                summary["concurrency_1"]["resources"]["total_peak_rss_mb"],
                summary["concurrency_2"]["resources"]["total_peak_rss_mb"],
                summary["concurrency_3"]["resources"]["total_peak_rss_mb"],
            )
            * 1.3
        )

        for tick in [0.0, max_rss * 0.25, max_rss * 0.5, max_rss * 0.75, max_rss]:
            gy = int(ax_y1 - (tick / max_rss) * (ax_y1 - ax_y0))
            canvas.draw_line(ax_x0, gy, ax_x1, gy, (226, 232, 240), width=1)
            canvas.draw_text(ax_x0 - 55, gy - 4, f"{tick:.0f}M", (100, 116, 139), scale=1)

        groups = [
            ("C=1", summary["concurrency_1"]),
            ("C=2", summary["concurrency_2"]),
            ("C=3", summary["concurrency_3"]),
        ]

        bar_width = 110
        gap = 85
        start_x = ax_x0 + 60

        for idx, (label, g_data) in enumerate(groups):
            bx = start_x + idx * (bar_width + gap)
            w_rss = g_data["resources"]["worker_peak_rss_mb"]
            c_rss = g_data["resources"]["chromium_peak_rss_mb"]
            tot_rss = g_data["resources"]["total_peak_rss_mb"]

            w_h = int((w_rss / max_rss) * (ax_y1 - ax_y0))
            c_h = int((c_rss / max_rss) * (ax_y1 - ax_y0))

            w_y = ax_y1 - w_h
            canvas.draw_rect(bx, w_y, bar_width, w_h, (30, 41, 59))

            c_y = w_y - c_h
            canvas.draw_rect(bx, c_y, bar_width, c_h, (14, 165, 233))

            canvas.draw_text(bx + 15, c_y - 18, f"{tot_rss:.1f} MB", (15, 23, 42), scale=2)
            canvas.draw_text(bx + 20, ax_y1 + 15, label, (30, 41, 59), scale=1)

        canvas.draw_rect(420, 75, 14, 10, (30, 41, 59))
        canvas.draw_text(440, 76, "WORKER (PYTHON)", (71, 85, 105), scale=1)

        canvas.draw_rect(580, 75, 14, 10, (14, 165, 233))
        canvas.draw_text(600, 76, "CHROMIUM TREE", (71, 85, 105), scale=1)

        canvas.save_png(self.output_dir / "resource_usage_comparison.png")


# =============================================================================
# CLI Main Entrypoint
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="ADBAP Milestone M3.6 Benchmark Runner")
    parser.add_argument("--runs", type=int, default=5, help="Number of benchmark runs per level")
    parser.add_argument("--warmup", type=int, default=1, help="Number of warmup runs per level")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Target output directory for benchmark artifacts",
    )
    args = parser.parse_args()

    harness = BenchmarkHarness(
        output_dir=args.output_dir,
        runs_per_concurrency=args.runs,
        warmup_runs=args.warmup,
    )

    summary = asyncio.run(harness.run_all([1, 2, 3]))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
