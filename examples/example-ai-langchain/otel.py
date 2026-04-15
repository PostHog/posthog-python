"""LangChain with OpenTelemetry instrumentation, exporting to PostHog."""

import os
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from posthog.ai.otel import PostHogSpanProcessor

tracer_provider = TracerProvider(
    resource=Resource.create(
        {"service.name": "langchain-example", "user.id": "example-user"}
    )
)
tracer_provider.add_span_processor(
    PostHogSpanProcessor(
        api_key=os.environ["POSTHOG_API_KEY"],
        host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
    )
)
trace.set_tracer_provider(tracer_provider)

# Use LangChain as normal — OTEL captures the traces automatically
model = ChatOpenAI(openai_api_key=os.environ["OPENAI_API_KEY"], temperature=0)

response = model.invoke([HumanMessage(content="What is product analytics?")])
print(response.content)
