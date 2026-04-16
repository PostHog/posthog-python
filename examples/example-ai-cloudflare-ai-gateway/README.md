# Cloudflare AI Gateway + PostHog AI Examples

Track Cloudflare AI Gateway API calls with PostHog via the OpenAI-compatible unified endpoint.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync
```

`POSTHOG_API_KEY`, `OPENAI_API_KEY`, `CF_AIG_TOKEN`, `CF_AIG_ACCOUNT_ID`, and `CF_AIG_GATEWAY_ID` are required. `CF_AIG_TOKEN` is your Cloudflare AI Gateway API token, passed via the `cf-aig-authorization` header.

## Examples

- **chat.py** - Chat completions via Cloudflare AI Gateway

## Run

```bash
source .env
uv run python chat.py
```
