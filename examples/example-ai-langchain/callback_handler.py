"""LangChain with OpenTelemetry instrumentation for automatic tracking."""

import os
import json
import urllib.request
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.langchain import LangchainInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-langchain-app",
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

LangchainInstrumentor().instrument()

from langchain_openai import ChatOpenAI  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402


@tool
def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    """Get current weather for a location.

    Args:
        latitude: The latitude of the location
        longitude: The longitude of the location
        location_name: A human-readable name for the location
    """
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


tools = [get_weather]
tool_map = {t.name: t for t in tools}

model = ChatOpenAI(openai_api_key=os.environ["OPENAI_API_KEY"], temperature=0)
model_with_tools = model.bind_tools(tools)

messages = [HumanMessage(content="What's the weather in Berlin?")]

response = model_with_tools.invoke(messages)

if response.content:
    print(response.content)

# In production, send tool results back to the model for a final response.
if response.tool_calls:
    for tool_call in response.tool_calls:
        result = tool_map[tool_call["name"]].invoke(tool_call["args"])
        print(result)
