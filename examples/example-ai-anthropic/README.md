# Anthropic + PostHog AI Examples

Track Anthropic Claude API calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Examples

- **chat.py** - Basic chat with tool calling
- **streaming.py** - Streaming responses
- **extended_thinking.py** - Claude's extended thinking feature

## Run

```bash
source .env
uv run python chat.py
uv run python streaming.py
uv run python extended_thinking.py
```
