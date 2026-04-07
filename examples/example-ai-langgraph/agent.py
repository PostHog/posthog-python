"""LangGraph agent with OpenTelemetry instrumentation for tracking LLM calls."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.langchain import LangchainInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-langgraph-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

LangchainInstrumentor().instrument()

from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool


@tool
def get_weather(city: str) -> str:
    """Get the weather for a given city."""
    return f"It's always sunny in {city}!"


model = ChatOpenAI(api_key=os.environ["OPENAI_API_KEY"])
agent = create_react_agent(model, tools=[get_weather])

result = agent.invoke(
    {"messages": [{"role": "user", "content": "What's the weather in Paris?"}]},
)

print(result["messages"][-1].content)
provider.shutdown()
