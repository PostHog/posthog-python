"""Azure OpenAI chat completions, tracked via OpenTelemetry."""

import os

from opentelemetry import trace
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.sdk.trace import TracerProvider

resource = Resource(
    attributes={
        SERVICE_NAME: "example-azure-openai-app",
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

OpenAIInstrumentor().instrument()

from openai import AzureOpenAI  # noqa: E402

client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21",
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
)

response = client.chat.completions.create(
    model="gpt-4o",
    max_completion_tokens=1024,
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
