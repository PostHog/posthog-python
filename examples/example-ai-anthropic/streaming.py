"""Anthropic streaming chat, tracked via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-anthropic-app",
        "posthog.distinct_id": "example-user",
        "foo": "bar",
        "conversation_id": "abc-123",
    }
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    PostHogSpanProcessor(
        api_key=os.environ["POSTHOG_API_KEY"],
        host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
    )
)
trace.set_tracer_provider(provider)

AnthropicInstrumentor().instrument()

import anthropic  # noqa: E402

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

stream = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Write a haiku about observability."}],
    stream=True,
)

for event in stream:
    if hasattr(event, "type"):
        if event.type == "content_block_delta" and hasattr(event.delta, "text"):
            print(event.delta.text, end="", flush=True)

print()
