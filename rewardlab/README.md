# RewardLab — Owned Test Website

RewardLab is a self-hosted mock rewards portal designed for browser automation integration testing under Milestone M1 of the ADBAP platform.

## Running Locally

```bash
uv python install 3.12
uv sync
uv run uvicorn app:app --port 8001
```

Access the UI at: `http://127.0.0.1:8001/`
Default test account credentials:
- Username: `test_01`
- Password: `test_password_01`
