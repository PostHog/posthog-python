"""OpenAI Responses API with streaming, tracked via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-openai-app",
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

OpenAIInstrumentor().instrument()

import openai  # noqa: E402

client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

stream = client.responses.create(
    model="gpt-4o-mini",
    max_output_tokens=1024,
    stream=True,
    instructions="You are a helpful assistant.",
    input=[{"role": "user", "content": "Write a haiku about product analytics."}],
)

for event in stream:
    if hasattr(event, "type") and event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)

print()
