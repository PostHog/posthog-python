# LiteLLM + PostHog AI Examples

Track LiteLLM calls with PostHog using the built-in callback integration.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

## Examples

- **chat.py** - Chat with tool calling (works with any LiteLLM-supported model)
- **streaming.py** - Streaming responses

## Run

```bash
source .env
python chat.py
python streaming.py
```
