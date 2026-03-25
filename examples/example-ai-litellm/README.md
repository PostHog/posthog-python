# LiteLLM + PostHog AI Examples

Track LiteLLM calls with PostHog using the built-in callback integration.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **chat.py** - Chat with tool calling (works with any LiteLLM-supported model)
- **streaming.py** - Streaming responses

## Run

```bash
source .env
uv run python chat.py
uv run python streaming.py
```
