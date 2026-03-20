"""LangChain with OpenTelemetry instrumentation, exporting to PostHog."""

import os
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
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
    resource=Resource.create({"service.name": "langchain-example", "user.id": "example-user"})
)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)

# Use LangChain as normal — OTEL captures the traces automatically
model = ChatOpenAI(openai_api_key=os.environ["OPENAI_API_KEY"], temperature=0)

response = model.invoke([HumanMessage(content="What is product analytics?")])
print(response.content)

tracer_provider.shutdown()
