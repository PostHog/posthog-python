# LlamaIndex + PostHog AI Examples

Track LlamaIndex LLM calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **chat.py** - LlamaIndex with PostHog tracking via OpenAI wrapper

## Run

```bash
source .env
uv run python chat.py
```
