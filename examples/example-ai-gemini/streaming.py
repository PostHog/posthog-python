"""Google Gemini streaming chat, tracked via OpenTelemetry."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.google_generativeai import (
    GoogleGenerativeAiInstrumentor,
)

resource = Resource(attributes={SERVICE_NAME: "example-gemini-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

GoogleGenerativeAiInstrumentor().instrument()

from google import genai

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

stream = client.models.generate_content_stream(
    model="gemini-2.5-flash",
    contents=[
        {
            "role": "user",
            "parts": [{"text": "Explain product analytics in three sentences."}],
        }
    ],
)

for chunk in stream:
    for candidate in chunk.candidates:
        for part in candidate.content.parts:
            if hasattr(part, "text"):
                print(part.text, end="", flush=True)

print()
provider.shutdown()
