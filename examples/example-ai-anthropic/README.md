# Anthropic + PostHog AI Examples

Track Anthropic Claude API calls with PostHog.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

## Examples

- **chat.py** - Basic chat with tool calling
- **streaming.py** - Streaming responses
- **extended_thinking.py** - Claude's extended thinking feature

## Run

```bash
source .env
python chat.py
python streaming.py
python extended_thinking.py
```
