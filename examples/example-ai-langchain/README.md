# LangChain + PostHog AI Examples

Track LangChain LLM calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Examples

- **callback_handler.py** - PostHog callback handler with tool calling
- **otel.py** - OpenTelemetry instrumentation exporting to PostHog

## Run

```bash
source .env
uv run python callback_handler.py
uv run python otel.py
```
