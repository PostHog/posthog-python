# Portkey + PostHog AI Examples

Track Portkey AI gateway calls with PostHog via the OpenAI-compatible API.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **chat.py** - Chat completions via Portkey AI gateway

## Run

```bash
source .env
uv run python chat.py
```
