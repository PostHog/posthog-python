"""OpenAI Chat Completions API with tool calling, tracked via OpenTelemetry."""

import os
import json
import urllib.request
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

import openai

client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

tools = [
    {
        "type": "function",
        "function": {
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
        },
    }
]


def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


response = client.chat.completions.create(
    model="gpt-4o-mini",
    max_completion_tokens=1024,
    tools=tools,
    tool_choice="auto",
    messages=[
        {
            "role": "system",
            "content": "You are a helpful assistant with access to weather data.",
        },
        {"role": "user", "content": "What's the weather like in Dublin, Ireland?"},
    ],
)

message = response.choices[0].message

if message.content:
    print(message.content)

# In production, send tool results back to the model for a final response.
if message.tool_calls:
    for tool_call in message.tool_calls:
        args = json.loads(tool_call.function.arguments)
        result = get_weather(**args)
        print(result)
