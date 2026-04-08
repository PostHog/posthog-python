"""CrewAI with OpenTelemetry instrumentation for tracking."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.crewai import CrewAIInstrumentor

resource = Resource(attributes={SERVICE_NAME: "example-crewai-app"})
exporter = OTLPSpanExporter(
    endpoint=f"{os.environ.get('POSTHOG_HOST', 'https://us.i.posthog.com')}/i/v0/ai/otel",
    headers={"Authorization": f"Bearer {os.environ['POSTHOG_API_KEY']}"},
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

CrewAIInstrumentor().instrument()

from crewai import Agent, Task, Crew

researcher = Agent(
    role="Researcher",
    goal="Find interesting facts about hedgehogs",
    backstory="You are an expert wildlife researcher.",
)

task = Task(
    description="Research three fun facts about hedgehogs.",
    expected_output="A list of three fun facts.",
    agent=researcher,
)

crew = Crew(
    agents=[researcher],
    tasks=[task],
)

result = crew.kickoff()
print(result)
