# Google Gemini + PostHog AI Examples

Track Google Gemini API calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
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
