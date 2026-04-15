# Cloudflare AI Gateway + PostHog AI Examples

Track Cloudflare AI Gateway API calls with PostHog via the OpenAI-compatible unified endpoint.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_GATEWAY_ID`, and `OPENAI_API_KEY` are required.

## Examples

- **chat.py** - Chat completions via Cloudflare AI Gateway (`compat` endpoint)

## Run

```bash
source .env
uv run python chat.py
```
