# AutoGen + PostHog AI Examples

Track AutoGen agent LLM calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **agent.py** - AutoGen agent with PostHog tracking via OpenAI wrapper

## Run

```bash
source .env
uv run python agent.py
```
