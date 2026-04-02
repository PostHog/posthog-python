# DeepSeek + PostHog AI Examples

Track DeepSeek API calls with PostHog via the OpenAI-compatible API.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Examples

- **chat.py** - Chat completions via DeepSeek

## Run

```bash
source .env
uv run python chat.py
```
