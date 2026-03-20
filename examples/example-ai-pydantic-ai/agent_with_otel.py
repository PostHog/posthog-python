"""Pydantic AI agent with OpenTelemetry instrumentation, exporting to PostHog."""

import os
import json
import urllib.request
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# Configure OTEL to export traces to PostHog
posthog_api_key = os.environ["POSTHOG_API_KEY"]
posthog_host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"{posthog_host}/i/v0/ai/otel"
os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {posthog_api_key}"

tracer_provider = TracerProvider(
    resource=Resource.create({"service.name": "pydantic-ai-example", "user.id": "example-user"})
)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

# Create an agent with a tool
model = OpenAIModel("gpt-4o-mini")
agent = Agent(model, system_prompt="You are a helpful assistant with access to weather data.")


@agent.tool
def get_weather(ctx: RunContext[None], latitude: float, longitude: float, location_name: str) -> str:
    """Get current weather for a location."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


# Enable automatic OTEL instrumentation for all agents
Agent.instrument_all()

result = agent.run_sync("What's the weather in Amsterdam?")
print(result.output)

tracer_provider.shutdown()
