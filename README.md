# Adaptive Distributed Browser Automation Platform (ADBAP)

ADBAP is an authorized, provider-agnostic, distributed browser automation platform. Full browser automation executes against the owned RewardLab test website with synthetic test accounts. Microsoft Rewards integration is **companion-only** (official links, reminders, manual progress tracking).

## System Requirements

- **Python:** 3.12+ (managed via `uv`)
- **Package & Runtime Manager:** [uv](https://docs.astral.sh/uv/)
- **OS:** Windows 10/11 or Linux / macOS

---

## Developer Quickstart

### 1. Install `uv`

- **Windows (PowerShell):**
  ```powershell
  winget install astral-sh.uv
  # Or via Astral standalone installer:
  irm https://astral.sh/uv/install.ps1 | iex
  ```
- **Linux / macOS:**
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. Environment Setup & Dependency Installation

```bash
cd backend
uv python install 3.12
uv sync
```

### 3. Run Quality Gates (Linter & Tests)

From the `backend/` directory:
```bash
# Code style and import order
uv run ruff check .

# Unit and integration tests
uv run pytest -v
```

### 4. Run the Control Plane API Locally

```bash
uv run uvicorn app.main:app --port 8000
```

### 5. Verify API Endpoints

- **Health check (Windows PowerShell):**
  ```powershell
  Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -Method Get | ConvertTo-Json
  ```
- **Health check (Linux / macOS curl):**
  ```bash
  curl -s http://127.0.0.1:8000/api/v1/health
  ```

- **Verify Policy Rejection (Windows PowerShell):**
  ```powershell
  try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/workflows/validate" `
      -Method Post -ContentType "application/json" `
      -Body '{"schema_version": 1, "provider": "microsoft_rewards", "steps": [{"id": "s1", "action": "navigate", "target_url": "https://rewards.bing.com"}]}'
  } catch {
    $_.ErrorDetails.Message
  }
  ```
  *Expected Output:* HTTP 403 with `{"code":"POLICY_DENIED", ...}`.

---

## 6. CLI Workflow Runner (Milestone M2.5)

Run authorized automation workflows against RewardLab via the `run-workflow` command:

- **Validate workflow (dry-run without browser launch):**
  ```powershell
  uv run run-workflow --validate-only -f ../examples/workflows/rewardlab_review_first_task.yaml
  ```
- **Execute workflow against RewardLab (Windows PowerShell):**
  ```powershell
  $env:REWARDLAB_PASSWORD = "test_password_01"
  uv run run-workflow -f ../examples/workflows/rewardlab_review_first_task.yaml -a test_01 -b http://127.0.0.1:8001
  $env:REWARDLAB_PASSWORD = $null
  ```
- **Execute workflow against RewardLab (Linux):**
  ```bash
  REWARDLAB_PASSWORD="test_password_01" uv run run-workflow \
    -f ../examples/workflows/rewardlab_review_first_task.yaml -a test_01 -b http://127.0.0.1:8001
  ```
- **Machine-readable JSON output for orchestration:**
  ```bash
  uv run run-workflow --json -f ../examples/workflows/rewardlab_review_first_task.yaml
  ```

For full documentation, exit codes, and security options, see [docs/CLI_GUIDE.md](file:///D:/Projects/AutomationTool/docs/CLI_GUIDE.md).

---

## Architectural Documentation

- **Operating rules & constraints:** `AGENTS.md`
- **CLI usage guide:** `docs/CLI_GUIDE.md`
- **Canonical architecture contracts:** `docs/ARCHITECTURE.md`
- **Milestone roadmap & status:** `docs/IMPLEMENTATION_PLAN.md` and `docs/ai-context/01_status.md`
- **Security & provider policies:** `docs/ai-context/08_rules_security_policy.md`
