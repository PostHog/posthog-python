# FastAPI Exception Capture Usage

Minimal reproduction of PostHog exception capture issue.

## Quick Start

```bash
cd posthog-python/playgrounds/fastapi-exception-capture
export POSTHOG_API_KEY="your_key"
uv sync
uv run python app.py
```

## Test

```bash
curl http://localhost:8000/test-exception
```
