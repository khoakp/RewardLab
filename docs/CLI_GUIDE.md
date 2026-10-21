# ADBAP CLI Workflow Runner Guide (Milestone M2.5)

The `run-workflow` command (also accessible via `adbap run-workflow` or `python -m app.cli`) provides a secure, version-validated command-line interface to evaluate and execute automation workflows against the self-owned RewardLab test platform.

---

## 1. Core Principles & Security Constraints

- **Pre-Dispatch Validation:** Workflows are fully parsed, schema-validated, and verified against `ProviderPolicy` **before** any browser process or context is allocated.
- **Fail-Closed on Policy Denials:** Workflows targeting unauthorized providers (such as Microsoft Rewards) or requesting unapproved capabilities are immediately rejected with exit code `2`. Zero browser instances are launched.
- **No Passwords on CLI Arguments:** Passwords and tokens must **never** be passed as CLI command-line arguments (preventing exposure in `ps`, process trees, or shell history). Use environment variables (`REWARDLAB_PASSWORD` or `REWARDLAB_SESSION_ID`).
- **No Unsafe Override Flags:** Flags such as `--ignore-policy` or `--unsafe-eval` are intentionally omitted.
- **Strict Secret Redaction:** All console outputs (both human-readable tables and JSON format) automatically redact passwords, session tokens, and URL userinfo credentials (`http://[REDACTED]:[REDACTED]@host`).

---

## 2. Command Synopsis & Options

```text
run-workflow [-f FILE] [-a ACCOUNT] [-p PROVIDER] [-b BASE_URL] 
             [-t TASK_ID] [-s SESSION_ID] [--timeout SECONDS] 
             [--validate-only | --dry-run] [--json] [--headless | --no-headless] 
             [-v] [-h]
```

### Argument Reference

| Argument | Type | Default | Description |
|---|---|---|---|
| `-f`, `--file` | Path | Positional or `-f` | Path to workflow YAML or JSON file. |
| `-a`, `--account` | String | `test_01` | Synthetic test account identifier (`test_01` to `test_12`). |
| `-p`, `--provider` | String | `None` | Optional expected provider (verified against workflow definition). |
| `-b`, `--base-url` | String | `http://127.0.0.1:8001` | Base URL for target application (RewardLab loopback). |
| `-t`, `--task-id` | String | `None` | Target task ID for verification steps (e.g. `task_01`). |
| `-s`, `--session-id`| String | `None` | Pre-authenticated session token (or via `REWARDLAB_SESSION_ID`). |
| `--timeout` | Float | Workflow default | Hard timeout bound for context lifecycle in seconds. |
| `--validate-only`, `--dry-run` | Flag | `False` | Validate schema and policy without launching browser or mutating state. |
| `--json` | Flag | `False` | Emit machine-readable JSON to stdout for automation orchestration. |
| `--headless`, `--no-headless` | Flag | `--headless` | Run Chromium in headless or windowed mode. |
| `-v`, `--verbose` | Flag | `False` | Enable verbose debug logging. |
| `-h`, `--help` | Flag | - | Display help synopsis and security notes. |

---

## 3. Exit Codes

The CLI returns deterministic process exit codes suitable for shell scripts, CI/CD, and orchestration engines:

| Code | Name | Description |
|---|---|---|
| `0` | `SUCCESS` | Workflow passed validation (in dry-run) or executed all steps successfully. |
| `1` | `FAILED` | Workflow step execution or assertion failed. |
| `2` | `POLICY_DENIED` | Pre-dispatch or runtime policy rejection (unauthorized provider or capability). |
| `3` | `TIMEOUT` | Context lifecycle or workflow timeout exceeded. |
| `4` | `CANCELLED` | Workflow execution was interrupted (e.g., Ctrl+C or SIGINT). |
| `5` | `USAGE_OR_SCHEMA_ERROR` | Missing arguments, malformed YAML, invalid schema, or file not found. |

---

## 4. Usage Examples

### 4.1 Validate-Only (Dry-Run)

Validate a workflow's syntax, structure, and permission policy without launching Chromium:

#### Windows PowerShell:
```powershell
# From backend directory
uv run run-workflow --validate-only -f ../examples/workflows/rewardlab_review_first_task.yaml

# Machine-readable JSON output:
uv run run-workflow --dry-run --json -f ../examples/workflows/rewardlab_review_first_task.yaml
```

#### Linux / macOS:
```bash
# From backend directory
uv run run-workflow --validate-only -f ../examples/workflows/rewardlab_review_first_task.yaml

# Machine-readable JSON output:
uv run run-workflow --dry-run --json -f ../examples/workflows/rewardlab_review_first_task.yaml
```

*Expected JSON output:*
```json
{
  "valid": true,
  "workflow_id": "rewardlab_review_first_task",
  "provider": "rewardlab",
  "schema_version": 1,
  "total_steps": 4,
  "requested_capabilities": [
    "read_tasks",
    "mark_reviewed"
  ]
}
```

---

### 4.2 Full Execution on RewardLab (Real Chromium)

Ensure RewardLab is running locally (e.g., on `http://127.0.0.1:8001`).

#### Windows PowerShell:
```powershell
# 1. Set synthetic account password in session environment
$env:REWARDLAB_PASSWORD = "test_password_01"

# 2. Execute workflow on account test_01
uv run run-workflow `
  --file ../examples/workflows/rewardlab_review_first_task.yaml `
  --account test_01 `
  --base-url http://127.0.0.1:8001

# 3. Clean up credentials from environment
$env:REWARDLAB_PASSWORD = $null
```

#### Linux / macOS:
```bash
# Execute with inline environment variable
REWARDLAB_PASSWORD="test_password_01" uv run run-workflow \
  --file ../examples/workflows/rewardlab_review_first_task.yaml \
  --account test_01 \
  --base-url http://127.0.0.1:8001
```

*Expected Human Output:*
```text
=== ADBAP Workflow Execution Summary ===
Execution ID : a1b2c3d4-e5f6-7890-abcd-ef0123456789
Workflow     : rewardlab_review_first_task (provider: rewardlab)
Account      : test_01
Target URL   : http://127.0.0.1:8001
Headless     : True
----------------------------------------
[1/4] navigate (open_tasks) ................. [SUCCESS] (184.2ms)
[2/4] extract_text (read_task) .............. [SUCCESS] (42.1ms)
[3/4] click (mark_reviewed) ................. [SUCCESS] (215.7ms)
[4/4] verify_state (verify_completed) ....... [SUCCESS] (31.0ms)
----------------------------------------
Overall Status       : SUCCESS
Duration             : 0.621s
Steps Executed       : 4/4
Requires Verification: False
Can Retry            : False
========================================
```

---

### 4.3 JSON Orchestration Mode (for M3 Multi-Account Runner)

Pipelines and orchestration schedulers can capture execution results via `--json`:

#### Windows PowerShell:
```powershell
$env:REWARDLAB_PASSWORD = "test_password_01"
$result = uv run run-workflow --json -f ../examples/workflows/rewardlab_review_first_task.yaml | ConvertFrom-Json
$env:REWARDLAB_PASSWORD = $null

Write-Host "Execution Status: $($result.status)"
Write-Host "Execution ID:     $($result.execution_id)"
Write-Host "Duration:         $($result.duration_seconds)s"
```

#### Linux / macOS:
```bash
REWARDLAB_PASSWORD="test_password_01" uv run run-workflow --json \
  -f ../examples/workflows/rewardlab_review_first_task.yaml | jq '{status, duration: .duration_seconds, steps: .steps_executed}'
```

---

### 4.4 Policy Denial Example (Zero Browser Context Allocated)

```bash
uv run run-workflow -f rejected_rewards_workflow.yaml
echo "Exit code: $?"
# Output:
# Policy Denied: Automation is prohibited for provider 'microsoft_rewards'.
# Exit code: 2
```

---

## 5. Personal Operator Console (`adbap jobs`)

The `adbap jobs` command suite enables an operator to observe paused automation jobs, inspect step execution history, and safely cancel or reject jobs requiring operator review without blind retries or duplicate side effects.

### 5.1 Command Synopsis

```text
adbap jobs list [--status STATUS] [--account ACCOUNT] [--limit N] [--offset N] [--json]
adbap jobs show <job-id> [--json]
adbap jobs review <job-id> --decision {cancel,reject} --reason "..." [--actor NAME] [--json]
```

### 5.2 Subcommands

| Subcommand | Description |
|---|---|
| `list` | Lists jobs filtered by status (default: `PAUSED_FOR_REVIEW`) or account ID with formatted table or JSON. Redacts all free-text credentials and URL tokens. |
| `show` | Displays job details, execution outcome, causal verification evidence (verified/unverified mutations, causal mappings), executed steps, pause reason, and audit trail. Sanitizes all free-text secrets at read time. |
| `review` | Applies an atomic operator review decision (`cancel` or `reject`). Mandatory non-empty `--reason` (1..1024 chars). Write-time sanitized before SQLite commit. Transitions directly to `SUCCEEDED` are strictly prohibited. |

> [!SECURITY]
> **CLI Argument Visibility Warning:** Arguments passed via the command line (such as `--reason`) may be temporarily visible in OS process listings (`ps -ef` or Task Manager). Operators must **never** pass raw passwords, cookies, or authorization tokens in CLI arguments.

### 5.3 Review Workflow Example

```bash
# 1. List paused jobs
uv run adbap jobs list --status PAUSED_FOR_REVIEW

# 2. Inspect the paused job with causal verification evidence
uv run adbap jobs show job_0192d4f2

# 3. Reject or cancel the job with explicit reasoning (atomic compare-and-set)
uv run adbap jobs review job_0192d4f2 --decision cancel --reason "Operator verified mutation manually on server"
```

---

## 6. Lightweight Workflow Inspector (`adbap inspect-workflow`)

The `adbap inspect-workflow` command provides a fast, zero-browser static inspection of workflow definitions.

### 6.1 Command Synopsis

```text
adbap inspect-workflow <workflow-file> [--json]
```

### 6.2 Preflight Evaluation Semantics

`adbap inspect-workflow` outputs one of three explicit preflight verdicts:

| Verdict | Meaning |
|---|---|
| `ALLOWED` | All statically verifiable conditions (provider authorization, requested capabilities, step action types, schemes, domain allowlists, destination ports) are valid. |
| `DENIED` | One or more static conditions violate provider policy (e.g. unknown provider, companion-only Microsoft Rewards, disallowed action, forbidden port, out-of-allowlist domain). |
| `UNRESOLVED` | The workflow contains dynamic components (such as `{template_var}` parameters in navigation URLs) that static analysis cannot guarantee pre-dispatch. Requires runtime enforcement. |

> [!NOTE]
> Static `ALLOWED` indicates the plan satisfies static policy constraints, but does not guarantee runtime safety. Runtime defenses (HTTP redirect guards, DNS validation, and network interception) remain strictly active during browser execution.

### 6.3 Capabilities

- **Zero Browser Allocation:** Inspects workflows without launching Chromium or opening network sockets.
- **Pre-Dispatch Policy Validation:** Statically evaluates `ProviderPolicy` (action allowlists, domain allowlists, schemes, ports, and capabilities).
- **Causal Mapping Analysis:** Maps mutating steps to their corresponding `verifies_step_ids` postconditions and alerts on unverified mutations or missing verification steps.
- **Locator Strategy Inspection:** Prints targeted actions and locator strategies for auditability.

