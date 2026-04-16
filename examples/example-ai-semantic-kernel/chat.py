"""Semantic Kernel with OpenTelemetry tracking via OpenAI instrumentation."""

import os
import asyncio
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-semantic-kernel-app",
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

import openai  # noqa: E402
from semantic_kernel import Kernel  # noqa: E402
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion  # noqa: E402

openai_client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

kernel = Kernel()
kernel.add_service(
    OpenAIChatCompletion(ai_model_id="gpt-4o-mini", async_client=openai_client)
)


async def main():
    result = await kernel.invoke_prompt("Tell me a fun fact about hedgehogs.")
    print(result)


asyncio.run(main())
