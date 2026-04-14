"""Anthropic extended thinking, tracked via OpenTelemetry.

Extended thinking lets Claude show its reasoning process before responding.
"""

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

message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=16000,
    thinking={"type": "enabled", "budget_tokens": 10000},
    messages=[
        {
            "role": "user",
            "content": "What is the probability of rolling at least one six in four rolls of a fair die?",
        }
    ],
)

for block in message.content:
    if block.type == "thinking":
        print(f"Thinking: {block.thinking}\n")
    elif block.type == "text":
        print(f"Answer: {block.text}")
