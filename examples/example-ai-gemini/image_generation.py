"""Google Gemini image generation, tracked via OpenTelemetry."""

import logging
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.google_generativeai import (
    GoogleGenerativeAiInstrumentor,
)

# Suppress verbose Gemini SDK logging of base64 image data
logging.getLogger("google.genai").setLevel(logging.WARNING)

resource = Resource(attributes={SERVICE_NAME: "example-gemini-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

GoogleGenerativeAiInstrumentor().instrument()

from google import genai

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=[{"role": "user", "parts": [{"text": "Generate a pixel art hedgehog"}]}],
)

for candidate in response.candidates:
    for part in candidate.content.parts:
        if hasattr(part, "inline_data") and part.inline_data:
            print(
                f"Generated image: {part.inline_data.mime_type}, {len(part.inline_data.data)} bytes"
            )
        elif hasattr(part, "text"):
            print(part.text)
