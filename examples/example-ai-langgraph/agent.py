"""LangGraph agent with PostHog callback handler for tracking LLM calls."""

import os
from posthog import Posthog
from posthog.ai.langchain import CallbackHandler
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
callback_handler = CallbackHandler(client=posthog)


@tool
def get_weather(city: str) -> str:
    """Get the weather for a given city."""
    return f"It's always sunny in {city}!"


model = ChatOpenAI(api_key=os.environ["OPENAI_API_KEY"])
agent = create_react_agent(model, tools=[get_weather])

result = agent.invoke(
    {"messages": [{"role": "user", "content": "What's the weather in Paris?"}]},
    config={"callbacks": [callback_handler]},
)

print(result["messages"][-1].content)
posthog.shutdown()
