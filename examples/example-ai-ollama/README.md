# Ollama + PostHog AI Examples

Track Ollama API calls with PostHog via the OpenAI-compatible API.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Make sure Ollama is running locally: ollama serve
uv sync
```

## Examples

- **chat.py** - Chat completions via Ollama

## Run

```bash
source .env
uv run python chat.py
```
