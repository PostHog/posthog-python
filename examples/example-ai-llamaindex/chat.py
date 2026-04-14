"""LlamaIndex with OpenTelemetry instrumentation for tracking."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.llamaindex import LlamaIndexInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-llamaindex-app",
        "posthog.distinct_id": "example-user",
        "foo": "bar",
        "conversation_id": "abc-123",
    }
)
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    PostHogSpanProcessor(
        api_key=os.environ["POSTHOG_API_KEY"],
        host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
    )
)
trace.set_tracer_provider(provider)

LlamaIndexInstrumentor().instrument()

from llama_index.llms.openai import OpenAI as LlamaOpenAI  # noqa: E402

llm = LlamaOpenAI(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"])

response = llm.complete("Tell me a fun fact about hedgehogs.")
print(response)
