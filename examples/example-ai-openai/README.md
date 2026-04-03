# OpenAI + PostHog AI Examples

Track OpenAI API calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

## Examples

- **chat_completions.py** - Chat Completions API with tool calling
- **chat_completions_streaming.py** - Chat Completions with streaming
- **responses.py** - Responses API with tool calling
- **responses_streaming.py** - Responses API with streaming
- **embeddings.py** - Text embeddings
- **transcription.py** - Audio transcription (Whisper)
- **image_generation.py** - Image generation via Responses API

## Run

```bash
source .env
uv run python chat_completions.py
uv run python chat_completions_streaming.py
uv run python responses.py
uv run python responses_streaming.py
uv run python embeddings.py
uv run python transcription.py
uv run python image_generation.py
```
