# OpenAI Agents SDK + PostHog AI Examples

Track OpenAI Agents SDK calls with PostHog.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your API keys in .env
```

## Examples

- **multi_agent.py** - Triage agent routing to specialist agents via handoffs
- **single_agent.py** - Single agent with weather and math tools
- **guardrails.py** - Input/output guardrails for content filtering
- **custom_spans.py** - Custom spans for tracking non-LLM operations within a trace

## Run

```bash
source .env
python multi_agent.py
python single_agent.py
python guardrails.py
python custom_spans.py
```
