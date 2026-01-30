# PostHog Pydantic AI Integration

This module provides PostHog instrumentation for [Pydantic AI](https://ai.pydantic.dev/) agents.

## Quick Start

```python
from posthog import Posthog
from posthog.ai.pydantic_ai import instrument_pydantic_ai
from pydantic_ai import Agent

# Initialize PostHog
posthog = Posthog(project_api_key="phc_xxx", host="https://us.i.posthog.com")

# Instrument all Pydantic AI agents (call once at startup)
instrument_pydantic_ai(posthog, distinct_id="user_123")

# Use Pydantic AI normally - all calls are automatically traced
agent = Agent("openai:gpt-4o")
result = await agent.run("What's the weather in San Francisco?")
```

## How It Works

```
┌─────────────────┐     ┌──────────────┐     ┌────────────────────────┐     ┌─────────┐
│ agent.run()     │────>│ OTel Spans   │────>│ PydanticAISpanExporter │────>│ PostHog │
│                 │     │ (Pydantic AI │     │ (normalizes messages,  │     │ Events  │
│                 │     │  native)     │     │  maps tool attributes) │     │         │
└─────────────────┘     └──────────────┘     └────────────────────────┘     └─────────┘
```

1. **Pydantic AI** emits OpenTelemetry spans natively via `Agent.instrument_all()`
2. **PydanticAISpanExporter** transforms Pydantic-specific formats to standard GenAI conventions
3. **PostHogSpanExporter** converts spans to PostHog `$ai_generation` and `$ai_span` events

## Configuration Options

```python
instrument_pydantic_ai(
    client=posthog,           # PostHog client instance
    distinct_id="user_123",   # User identifier for events
    properties={              # Additional properties for all events
        "$ai_session_id": "session_abc",
    },
    groups={                  # PostHog groups
        "company": "acme",
    },
    debug=False,              # Enable debug logging
)
```

Privacy mode is inherited from the client - set `privacy_mode=True` when creating your PostHog client to exclude message content.

## What Gets Captured

### Model Calls (`$ai_generation` events)

Every LLM API call creates an event with:
- Model name and provider
- Input/output messages (unless privacy mode is enabled on the client)
- Token usage (input, output)
- Latency
- Error status

### Tool Calls (`$ai_span` events)

When agents use tools:
- Tool name
- Arguments passed to the tool
- Tool result/response
- Latency

## Pydantic AI-Specific Handling

This integration handles Pydantic AI's specific message and attribute formats:

### Message Normalization

Pydantic AI uses a "parts" format for messages:
```python
# Pydantic AI format
{"parts": [{"content": "Hello", "type": "text"}], "role": "user"}

# Normalized to OpenAI format for PostHog
{"content": "Hello", "role": "user"}
```

### Tool Attribute Mapping

Pydantic AI uses non-standard attribute names:
```python
# Pydantic AI attributes
"tool_arguments": '{"city": "SF"}'
"tool_response": "Sunny, 72F"

# Mapped to GenAI standard
"gen_ai.tool.call.arguments": '{"city": "SF"}'
"gen_ai.tool.call.result": "Sunny, 72F"
```

## Requirements

- `pydantic-ai >= 0.1.0`
- `opentelemetry-sdk`

Install with:
```bash
pip install posthog pydantic-ai opentelemetry-sdk
```

## Advanced: Using the Exporter Directly

For more control, use `PydanticAISpanExporter` directly:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings
from posthog.ai.pydantic_ai import PydanticAISpanExporter

exporter = PydanticAISpanExporter(
    client=posthog,
    distinct_id="user_123",
)

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(exporter))

Agent.instrument_all(InstrumentationSettings(
    tracer_provider=provider,
    include_content=True,
))
```
