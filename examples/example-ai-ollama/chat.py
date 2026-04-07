"""Ollama chat completions via OpenAI-compatible API, tracked by PostHog via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-ollama-app"})

exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)

provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

OpenAIInstrumentor().instrument()

import openai

client = openai.OpenAI(
    base_url="http://localhost:11434/v1",
    api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
)

response = client.chat.completions.create(
    model="llama3.2",
    max_completion_tokens=1024,
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
provider.shutdown()
