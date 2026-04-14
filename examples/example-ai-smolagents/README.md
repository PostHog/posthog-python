# smolagents + PostHog AI Examples

Track smolagents LLM calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **agent.py** - smolagents CodeAgent with PostHog tracking via OpenAI wrapper

## Run

```bash
source .env
uv run python agent.py
```
