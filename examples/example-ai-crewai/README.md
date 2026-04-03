# CrewAI + PostHog AI Examples

Track CrewAI agent LLM calls with PostHog via LiteLLM callbacks.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **crew.py** - CrewAI crew with PostHog tracking via LiteLLM

## Run

```bash
source .env
uv run python crew.py
```
