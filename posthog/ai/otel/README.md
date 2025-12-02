# PostHog OpenTelemetry Integration

This module provides a generic OpenTelemetry `SpanExporter` that translates OTel spans into PostHog AI analytics events.

## Overview

Many AI/LLM frameworks use OpenTelemetry for instrumentation. This exporter allows PostHog to receive telemetry from any OTel-instrumented framework by converting spans to PostHog events.

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────┐     ┌─────────┐
│ AI Framework    │────>│ OTel Spans   │────>│ PostHogSpanExporter │────>│ PostHog │
│ (Pydantic AI,   │     │ (native)     │     │ (translates spans)  │     │ Events  │
│  LlamaIndex...) │     └──────────────┘     └─────────────────────┘     └─────────┘
└─────────────────┘
```

## Usage

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from posthog import Posthog
from posthog.ai.otel import PostHogSpanExporter

# Create PostHog client
posthog = Posthog(project_api_key="phc_xxx", host="https://us.i.posthog.com")

# Create exporter and tracer provider
exporter = PostHogSpanExporter(
    client=posthog,
    distinct_id="user_123",
    privacy_mode=False,  # Set True to exclude message content
)

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(exporter))

# Use this provider with your OTel-instrumented framework
```

## Span to Event Mapping

The exporter classifies spans and maps them to PostHog events:

| Span Type | PostHog Event | Detection |
|-----------|---------------|-----------|
| Model request | `$ai_generation` | Span name starts with "chat" or has `gen_ai.request.model` |
| Tool execution | `$ai_span` | Span name contains "tool" or has `gen_ai.tool.name` |
| Agent orchestration | (skipped) | Span name contains "agent" |

## GenAI Semantic Conventions

The exporter follows [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

| OTel Attribute | PostHog Property |
|----------------|------------------|
| `gen_ai.request.model` | `$ai_model` |
| `gen_ai.system` | `$ai_provider` |
| `gen_ai.usage.input_tokens` | `$ai_input_tokens` |
| `gen_ai.usage.output_tokens` | `$ai_output_tokens` |
| `gen_ai.input.messages` | `$ai_input` |
| `gen_ai.output.messages` | `$ai_output_choices` |
| `gen_ai.tool.name` | `$ai_span_name` |
| `gen_ai.tool.call.arguments` | `$ai_tool_arguments` |
| `gen_ai.tool.call.result` | `$ai_tool_result` |

## Configuration Options

| Parameter | Type | Description |
|-----------|------|-------------|
| `client` | `Posthog` | PostHog client instance |
| `distinct_id` | `str` | User identifier (falls back to trace ID if not set) |
| `privacy_mode` | `bool` | Exclude message content from events |
| `properties` | `dict` | Additional properties to include in all events |
| `groups` | `dict` | PostHog groups for all events |
| `debug` | `bool` | Enable debug logging |

## Framework-Specific Exporters

For frameworks with non-standard attribute names or message formats, use the framework-specific exporter wrapper:

- **Pydantic AI**: Use `posthog.ai.pydantic_ai.PydanticAISpanExporter` or the simpler `instrument_pydantic_ai()` function

These wrappers normalize framework-specific formats before passing spans to `PostHogSpanExporter`.
