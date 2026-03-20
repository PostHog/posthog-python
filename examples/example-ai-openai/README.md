# OpenAI + PostHog AI Examples

Track OpenAI API calls with PostHog.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
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
python chat_completions.py
python chat_completions_streaming.py
python responses.py
python responses_streaming.py
python embeddings.py
python transcription.py
python image_generation.py
```
