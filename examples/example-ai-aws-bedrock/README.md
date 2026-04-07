# AWS Bedrock + PostHog AI Examples

Track AWS Bedrock LLM calls with PostHog via OpenTelemetry instrumentation.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **chat.py** - Bedrock Converse API with OpenTelemetry tracing to PostHog

## Run

```bash
source .env
uv run python chat.py
```
