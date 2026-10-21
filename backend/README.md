# ADBAP Backend

Adaptive Distributed Browser Automation Platform (ADBAP) Control Plane.

## Setup & Running

```bash
uv python install 3.12
uv venv --python 3.12
uv sync
uv run uvicorn app.main:app --port 8000
```
