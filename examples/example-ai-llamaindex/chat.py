"""LlamaIndex with OpenTelemetry instrumentation for tracking."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.llamaindex import LlamaIndexInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-llamaindex-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

LlamaIndexInstrumentor().instrument()

from llama_index.llms.openai import OpenAI as LlamaOpenAI

llm = LlamaOpenAI(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"])

response = llm.complete("Tell me a fun fact about hedgehogs.")
print(response)
