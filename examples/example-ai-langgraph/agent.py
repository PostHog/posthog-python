"""LangGraph agent with OpenTelemetry instrumentation for tracking LLM calls."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from posthog.ai.otel import PostHogSpanProcessor
from opentelemetry.instrumentation.langchain import LangchainInstrumentor

resource = Resource(
    attributes={
        SERVICE_NAME: "example-langgraph-app",
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

LangchainInstrumentor().instrument()

from langgraph.prebuilt import create_react_agent  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langchain_core.tools import tool  # noqa: E402


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
