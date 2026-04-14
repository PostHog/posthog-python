# LiteLLM + PostHog AI Examples

Track LiteLLM calls with PostHog using the built-in callback integration.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
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
