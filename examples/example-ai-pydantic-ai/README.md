# Pydantic AI + PostHog AI Examples

Track Pydantic AI agent calls with PostHog via OpenTelemetry.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

## Examples

- **agent_with_otel.py** - Agent with tool calling, instrumented via OTEL

## Run

```bash
source .env
python agent_with_otel.py
```
