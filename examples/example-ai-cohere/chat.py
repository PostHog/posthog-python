"""Cohere chat completions via OpenAI-compatible API, tracked by PostHog via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-cohere-app",
        "posthog.distinct_id": "example-user",
        "foo": "bar",
        "conversation_id": "abc-123",
    }
)

exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)

provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

OpenAIInstrumentor().instrument()

import openai  # noqa: E402

client = openai.OpenAI(
    base_url="https://api.cohere.ai/compatibility/v1",
    api_key=os.environ["COHERE_API_KEY"],
)

response = client.chat.completions.create(
    model="command-a-03-2025",
    max_completion_tokens=1024,
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
