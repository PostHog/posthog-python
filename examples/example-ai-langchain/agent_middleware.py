"""Track a LangChain v1 agent with PostHog middleware."""

import os

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from posthog import Posthog
from posthog.ai.langchain.middleware import PostHogMiddleware


@tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"It is sunny in {city}."


client = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
agent = create_agent(
    model=ChatOpenAI(model="gpt-4.1-mini"),
    tools=[get_weather],
    middleware=[PostHogMiddleware(client, distinct_id="example-user")],
)

result = agent.invoke(
    {"messages": [HumanMessage(content="What is the weather in Berlin?")]}
)
print(result["messages"][-1].content)
client.shutdown()
