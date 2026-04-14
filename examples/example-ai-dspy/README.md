# DSPy + PostHog AI Examples

Track DSPy LLM calls with PostHog via LiteLLM callbacks.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **predict.py** - DSPy prediction with PostHog tracking via LiteLLM

## Run

```bash
source .env
uv run python predict.py
```
