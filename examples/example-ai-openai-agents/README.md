# OpenAI Agents SDK + PostHog AI Examples

Track OpenAI Agents SDK calls with PostHog.

## Setup

```bash
cp .env.example .env
# Fill in your API keys in .env
uv sync
```

## Examples

- **multi_agent.py** - Triage agent routing to specialist agents via handoffs
- **single_agent.py** - Single agent with weather and math tools
- **guardrails.py** - Input/output guardrails for content filtering
- **custom_spans.py** - Custom spans for tracking non-LLM operations within a trace

## Run

```bash
source .env
uv run python multi_agent.py
uv run python single_agent.py
uv run python guardrails.py
uv run python custom_spans.py
```
