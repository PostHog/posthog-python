"""OpenAI embeddings, tracked via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-openai-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

OpenAIInstrumentor().instrument()

import openai  # noqa: E402

client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

response = client.embeddings.create(
    model="text-embedding-3-small",
    input="PostHog is an open-source product analytics platform.",
)

embedding = response.data[0].embedding
print(f"Embedding dimensions: {len(embedding)}")
print(f"First 5 values: {embedding[:5]}")
