# Google Gemini + PostHog AI Examples

Track Google Gemini API calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Examples

- **chat.py** - Chat with tool calling
- **streaming.py** - Streaming responses
- **image_generation.py** - Image generation

## Run

```bash
source .env
uv run python chat.py
uv run python streaming.py
uv run python image_generation.py
```
