# LangGraph + PostHog AI Examples

Track LangGraph agent LLM calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **agent.py** - LangGraph ReAct agent with PostHog callback handler

## Run

```bash
source .env
uv run python agent.py
```
