---
last_updated: 2026-10-21
scope: gate_report
milestone: M3.6
status: accepted
verdict: M3.6 PASSED
---

# Gate Report: Milestone M3.6 — Local Load Testing & Performance Benchmarking

## 1. Executive Summary & Verdict

- **Milestone:** M3.6 — Local Load Testing & Performance Benchmarking of Local Worker and Bounded Scheduler across 12 synthetic RewardLab accounts at concurrency levels 1, 2, and 3.
- **Verdict:** **`M3.6 PASSED`**
- **Scope Verified:**
  - **Empirical Execution with Real Chromium:** 18 total benchmark executions (3 warm-up runs + 15 measured runs across concurrency 1, 2, and 3; 5 independent runs per level) against a live RewardLab HTTP service on Windows 11. No mock executors or synthetic delay simulations.
  - **12 Synthetic Accounts Workload:** All 12 accounts (`test_01` to `test_12`) executed a canonical 4-step workflow (`rewardlab_review_first_task.yaml`, SHA256: `52f044e74b878d9b19af48912a61c969bf98bbfd6cea4c6e512fabc9b65c2ec1`), inspecting tasks in isolated browser contexts, performing idempotent reviews, and verifying point awards (+10 points) via server-side verification.
  - **Comprehensive Resource & Latency Tracking:** Measured worker Python process RSS/CPU, Playwright Chromium process tree RSS/CPU (including `node.exe` and `chrome-headless-shell.exe` processes via Win32 `K32EnumProcesses` and `K32GetProcessMemoryInfo`), monotonic queue wait time, execution duration, and end-to-end latency with p50/p95 percentiles.
  - **Correctness Invariants Maintained:** Across all 180 evaluated benchmark jobs, verified **0** account lock conflicts, **0** duplicate executions or point over-allocations, **0** orphaned locks, **0** job losses, **0** browser crashes, and **0** SQLite lock/busy errors.
  - **Release R1 Milestone Completion:** Completes the final required milestone of Milestone M3 (Multi-account local orchestration), proving that the platform reliably schedules, locks, recovers, and executes multi-account browser automation workloads locally.

---

## 2. Test Environment & Workload Configuration

### 2.1 Host Machine Specification

| Parameter | Observed Value | Notes |
|---|---|---|
| **Operating System** | Windows 11 Pro (10.0.26300-SP0) | 64-bit AMD64 architecture |
| **Processor** | Intel(R) Core(TM) (Model 183 Stepping 1) | 20 logical execution cores |
| **Python Runtime** | CPython 3.12.15 | Virtualenv managed via `uv` |
| **Browser Runtime** | Chromium Headless Shell 156.0.8078.4 | Playwright 1.58+ async driver |
| **Database Engine** | SQLite 3 with WAL Mode & `BEGIN IMMEDIATE` | `busy_timeout=10000ms`, `foreign_keys=ON` |
| **Target Service** | RewardLab local FastAPI loopback server | Running on loopback TCP port |

### 2.2 Workload Characteristics

- **Workflow ID:** `rewardlab_review_first_task` (Version 1)
- **Workflow Hash (SHA256):** `52f044e74b878d9b19af48912a61c969bf98bbfd6cea4c6e512fabc9b65c2ec1`
- **Steps per Workflow:** 4 steps
  1. `navigate`: Open task review page (`/tasks/task_01`)
  2. `extract_text`: Extract fake task description
  3. `click`: Click `"Mark as reviewed"` button (mutating action, `requires_verification=True`)
  4. `verify_state`: Server-side API verification of completed status
- **Accounts:** 12 synthetic accounts (`test_01` to `test_12`)
- **Isolation Protocol:** Each run initializes a clean file-backed SQLite database, registers providers/workflows/accounts, generates encrypted session credentials in `SecureSessionStore`, and seeds RewardLab with clean task fixtures.
- **Browser Lifecycle:** Ephemeral isolated `BrowserContext` created per job execution; single shared headless Chromium process per worker instance; contexts destroyed immediately in `finally` upon job completion.

---

## 3. Empirical Benchmark Results

### 3.1 Aggregate Performance Summary (5 Independent Runs per Level)

| Metric | Concurrency 1 (Sequential) | Concurrency 2 (Default) | Concurrency 3 (High Concurrency) |
|---|---|---|---|
| **Jobs Evaluated (5 runs)** | 60 | 60 | 60 |
| **Wall Clock Duration (s)** | 4.88s (median) / 4.96s (mean) | 2.61s (median) / 2.66s (mean) | 2.47s (median) / 2.47s (mean) |
| **Throughput (jobs/sec)** | **2.46 j/s** (median) / 2.43 (mean) | **4.60 j/s** (median) / 4.51 (mean) | **4.86 j/s** (median) / 4.87 (mean) |
| **Throughput Delta vs C=1** | Baseline (1.0x) | **+86.9%** (1.87x) | **+97.6%** (1.98x) |
| **Throughput Delta vs C=2** | - | Baseline | **+5.7%** (diminishing return) |
| **Queue Latency p50 (s)** | 2.72s | 1.60s (-41.2%) | 1.43s (-47.4%) |
| **Queue Latency p95 (s)** | 4.86s | 2.65s (-45.5%) | 2.29s (-52.9%) |
| **Execution Duration p50 (s)** | 0.35s | 0.37s (+5.7%) | 0.47s (+34.3%) |
| **Execution Duration p95 (s)** | 0.61s | 0.61s (+0.0%) | 0.66s (+8.2%) |
| **End-to-End Latency p50 (s)** | 3.09s | 2.00s (-35.3%) | 1.91s (-38.2%) |
| **End-to-End Latency p95 (s)** | 5.18s | 3.00s (-42.1%) | 2.75s (-46.9%) |
| **Worker Process RSS (MB)** | 109.38 MB (peak) | 114.21 MB (peak) | 117.87 MB (peak) |
| **Chromium Process Tree RSS (MB)** | 376.03 MB (peak) | 491.66 MB (peak) | 577.86 MB (peak) |
| **Total System RSS (MB)** | **485.09 MB** (peak) | **605.08 MB** (peak) | **695.84 MB** (peak) |
| **Average CPU Utilization (%)** | 6.24% | 9.21% | 11.94% |
| **Max Observed Concurrency** | 1 | 2 | 3 |
| **Job Pass Rate** | 100.0% (60/60) | 100.0% (60/60) | 100.0% (60/60) |

### 3.2 Throughput Stability Across Repeated Runs

| Concurrency Level | Run 1 | Run 2 | Run 3 | Run 4 | Run 5 | Median | Std Dev |
|---|---|---|---|---|---|---|---|
| **Concurrency 1** | 2.49 j/s | 2.41 j/s | 2.46 j/s | 2.59 j/s | 2.18 j/s | **2.46 j/s** | 0.15 j/s |
| **Concurrency 2** | 4.39 j/s | 4.60 j/s | 4.65 j/s | 4.68 j/s | 4.24 j/s | **4.60 j/s** | 0.19 j/s |
| **Concurrency 3** | 4.86 j/s | 4.99 j/s | 4.74 j/s | 5.05 j/s | 4.71 j/s | **4.86 j/s** | 0.15 j/s |

---

## 4. Analysis & Engineering Tradeoffs

### 4.1 Scaling Efficiency & Diminishing Returns

1. **Near-Linear Scaling from C=1 to C=2:**
   - Doubling the worker concurrency permit limit from 1 to 2 increases median throughput from 2.46 jobs/s to 4.60 jobs/s, representing an **86.9% throughput increase** (efficiency: ~93.5% of theoretical 2x doubling).
   - Median end-to-end latency falls from 3.09s to 2.00s (-35.3%), driven by a dramatic reduction in queue waiting time (p50: 2.72s down to 1.60s).
   - Browser execution duration per job remains virtually flat (p50: 0.35s at C=1 vs 0.37s at C=2), demonstrating that two simultaneous browser contexts incur negligible CPU/IPC contention on the shared Chromium instance.

2. **Diminishing Returns at C=3:**
   - Increasing concurrency from 2 to 3 improves throughput by only **+5.7%** (4.60 jobs/s to 4.86 jobs/s), falling far short of theoretical linear scaling (+50%).
   - Median browser execution duration increases significantly from 0.37s to 0.47s (**+27.0% execution slowdown** per job), reflecting IPC message contention, JavaScript event loop queuing, and DOM layout contention across three active Playwright contexts within the single browser process tree.
   - Total memory footprint rises from 605 MB to ~696 MB (+91 MB RSS) without commensurate throughput gains.

### 4.2 Memory & Resource Footprint

- **Worker Python Process Stability:** Across all concurrency levels, the Python backend worker process remains lean and stable, ranging from 109.38 MB (C=1) to 117.87 MB (C=3) peak RSS. Memory is well-bounded with zero leaks across jobs.
- **Chromium Process Tree Growth:** Chromium process tree RSS grows with each active context:
  - 1 active context: ~376 MB peak RSS
  - 2 active contexts: ~492 MB peak RSS (+116 MB)
  - 3 active contexts: ~578 MB peak RSS (+86 MB)
- **CPU Utilization:** Average CPU stays comfortably low across 20 logical execution cores: 6.24% at C=1, 9.21% at C=2, and 11.94% at C=3. Execution is IO-bound (browser rendering and loopback network requests) rather than CPU-bound.

### 4.3 Database & Transaction Performance

- **Zero SQLite Lock Contention:** The 4-phase transaction split designed in M3.4 (`Claim -> Start -> Execute Browser -> Finalize`) proved 100% effective:
  - Phase 1 (Claim) and Phase 2 (Start) execute in under 2ms.
  - During Phase 3 (Browser Execution), **zero database connections or locks are held**.
  - Phase 4 (Finalize) executes in under 2ms.
  - Across all 18 runs (216 jobs claimed and finalized under concurrent workers), **0 SQLite busy or lock errors** occurred.

### 4.4 Default Concurrency Recommendation

- **Recommendation:** **Retain Concurrency = 2 as the production default for local workers.**
- **Rationale:**
  - Concurrency 2 provides the "sweet spot" of performance: an 87% throughput boost over sequential execution, a 35% reduction in latency, and a modest ~600 MB total RAM footprint.
  - Concurrency 3 yields only a 5.7% throughput improvement while increasing per-job browser latency by 27% and RAM consumption by 15%, introducing diminishing returns.
  - On constrained machines or single-node edge setups, Concurrency 2 minimizes memory pressure and avoids browser tab thrashing. Concurrency 3 remains available as an optional CLI flag (`--concurrency 3`) for high-spec workstations.

---

## 5. Correctness & Safety Invariants Verification

| Invariant | Target | Observed (180 Jobs) | Status | Verification Method |
|---|---|---|---|---|
| **Account Mutual Exclusion** | 0 concurrent jobs per account | **0 conflicts** | **PASSED** | Database PRIMARY KEY constraint on `account_execution_locks` |
| **Duplicate Side Effects** | 0 duplicate point awards | **0 duplicates** | **PASSED** | RewardLab server audit: each account completed exactly once per run (+10 points) |
| **Orphaned Account Locks** | 0 remaining locks after stop | **0 orphaned locks** | **PASSED** | Post-run DB query on `account_execution_locks`: table empty |
| **Job State Integrity** | 100% reach terminal state | **100% SUCCEEDED** | **PASSED** | All 180 benchmark jobs transitioned `QUEUED -> LEASED -> RUNNING -> SUCCEEDED` |
| **Cross-Account Session Isolation** | 0 credential or cookie leaks | **0 leaks** | **PASSED** | Ephemeral `BrowserContext` per job with unique cookie storage; verified by RewardLab |
| **Database Busy / Lock Errors** | 0 SQLite errors | **0 errors** | **PASSED** | 4-phase transaction split prevents lock holding during browser automations |
| **Browser Crashes** | 0 browser disconnections | **0 crashes** | **PASSED** | Zero unexpected disconnections or crash events recorded |
| **Secret Redaction** | 0 plaintext secrets in logs/CSV | **0 plaintext secrets** | **PASSED** | `benchmark_raw.csv` and runner logs contain no tokens, cookies, or credentials |

---

## 6. Artifact Inventory

All benchmark evidence, raw measurements, configuration files, and visual charts are preserved in `docs/spikes/m3_6/`:

1. **`benchmark_config.json`**: Machine specifications, OS, Python/Chromium versions, workflow SHA256, and benchmark parameters.
2. **`benchmark_raw.csv`**: Granular per-job records (216 rows) containing job IDs, account IDs, timestamps, queue latencies, execution durations, and step counts.
3. **`benchmark_summary.json`**: Complete statistical summary across concurrency levels 1, 2, and 3 (p50, p95, min, max, mean, std dev, resource peaks).
4. **`throughput_comparison.png`**: High-contrast chart comparing jobs/second across concurrency 1, 2, and 3.
5. **`latency_comparison.png`**: High-contrast chart comparing Queue Latency, Browser Execution Duration, and End-to-End Latency.
6. **`resource_usage_comparison.png`**: High-contrast chart comparing Worker RSS, Chromium RSS, and Total RSS Memory footprint.
7. **`benchmark_runner.py`**: Fully reproducible benchmark harness script with zero external visualization dependencies (pure Python PNG canvas).

---

## 7. Quality Gates & Regression Verification

- **Backend Test Suite:** `uv run pytest -q` in `backend/` -> **214/214 passed** in 80.06s.
- **RewardLab Test Suite:** `uv run pytest -q` in `rewardlab/` -> **64/64 passed** in 1.90s.
- **Total Test Suite:** **278/278 passed** across the entire workspace.
- **Linter Checks:** `uv run ruff check .` in `backend/` and `rewardlab/` -> **0 errors, all checks passed**.
- **Benchmark Runner Quality:** `uv run ruff check ../docs/spikes/m3_6/benchmark_runner.py` -> **0 errors, all checks passed**.
- **Zero Regressions:** All existing capabilities from M0, M1, M2.1–M2.5, and M3.1–M3.5 verified intact.

---

## 8. Release R1 Readiness Summary

With the successful completion and verification of Milestone M3.6, **Release R1 (Multi-Account Orchestration)** is formally verified and ready.

### 8.1 Capabilities Proven in Release R1
- **12 Synthetic Accounts:** Full multi-account support for synthetic test accounts (`test_01` to `test_12`).
- **Encrypted Session Store:** Per-account AES-256-GCM encrypted session storage with account-binding verification and owner authorization.
- **Robust Job State Machine:** Strict transitions across 10 persistent states with terminal state immutability and idempotency keys.
- **Atomic Per-Account Locking:** Database-level single-account mutual exclusion preventing concurrent executions on the same account across independent worker processes.
- **Local Background Worker:** Independent asynchronous worker process with bounded concurrency, 4-phase transaction split, and graceful shutdown.
- **Crash Recovery & Reconciliation:** Automatic startup sweeper, background orphan sweeper, atomic lease renewal with monotonic fencing, and fail-closed side-effect protection preserving account safety holds.
- **Empirical Performance Benchmarking:** Validated throughput scaling (+87% at Concurrency 2) and resource profiling on real Chromium.

### 8.2 Boundary Constraints & Next Milestone
- Release R1 is scoped to **local single-node orchestration** with file-backed SQLite persistence.
- **Out of Scope for R1:** Multi-node distributed scheduling, PostgreSQL clustering, remote node agents (M4), Web Dashboard UX (M5), Workflow Recorder (M6), LLM Assisted Workflows (M7), and Android Termux agents (M8).
- **Next Step:** Await operator direction before commencing **Milestone M4: Distributed Scheduler and Worker Nodes**.
