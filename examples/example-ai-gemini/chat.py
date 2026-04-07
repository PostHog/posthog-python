"""Google Gemini chat with tool calling, tracked via OpenTelemetry."""

import os
import json
import urllib.request
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
from google.genai import types

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

tool_declarations = [
    {
        "name": "get_weather",
        "description": "Get current weather for a location",
        "parameters": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
                "location_name": {"type": "string"},
            },
            "required": ["latitude", "longitude", "location_name"],
        },
    }
]


def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


config = types.GenerateContentConfig(
    tools=[types.Tool(function_declarations=tool_declarations)]
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=[{"role": "user", "parts": [{"text": "What's the weather in London?"}]}],
    config=config,
)

# In production, send tool results back to the model for a final response.
for candidate in response.candidates:
    for part in candidate.content.parts:
        if hasattr(part, "function_call") and part.function_call:
            result = get_weather(
                latitude=part.function_call.args["latitude"],
                longitude=part.function_call.args["longitude"],
                location_name=part.function_call.args["location_name"],
            )
            print(result)
        elif hasattr(part, "text"):
            print(part.text)

provider.shutdown()
