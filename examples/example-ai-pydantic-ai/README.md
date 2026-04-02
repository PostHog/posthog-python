# Pydantic AI + PostHog AI Examples

Track Pydantic AI agent calls with PostHog via OpenTelemetry.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Examples

- **agent_with_otel.py** - Agent with tool calling, instrumented via OTEL

## Run

```bash
source .env
uv run python agent_with_otel.py
```
