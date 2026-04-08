"""smolagents with OpenTelemetry tracking via OpenAI instrumentation."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-smolagents-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

OpenAIInstrumentor().instrument()

import openai  # noqa: E402
from smolagents import CodeAgent, OpenAIServerModel  # noqa: E402

openai_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

model = OpenAIServerModel(model_id="gpt-4o-mini", client=openai_client)

agent = CodeAgent(tools=[], model=model)
result = agent.run("What is a fun fact about hedgehogs?")
print(result)
