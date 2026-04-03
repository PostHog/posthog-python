# Mirascope + PostHog AI Examples

Track Mirascope LLM calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **chat.py** - Mirascope call decorator with PostHog tracking

## Run

```bash
source .env
uv run python chat.py
```
