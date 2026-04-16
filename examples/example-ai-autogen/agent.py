"""AutoGen with OpenTelemetry tracking via OpenAI instrumentation."""

import os
import asyncio
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-autogen-app",
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
from autogen_agentchat.agents import AssistantAgent  # noqa: E402
from autogen_ext.models.openai import OpenAIChatCompletionClient  # noqa: E402

openai_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

model_client = OpenAIChatCompletionClient(
    model="gpt-4o-mini",
    openai_client=openai_client,
)

agent = AssistantAgent("assistant", model_client=model_client)


async def main():
    result = await agent.run(task="Tell me a fun fact about hedgehogs.")
    print(result)
    await model_client.close()


asyncio.run(main())
