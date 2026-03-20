"""LangChain with PostHog callback handler for automatic tracking."""

import os
import json
import urllib.request
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage
from posthog import Posthog
from posthog.ai.langchain import CallbackHandler

posthog = Posthog(os.environ["POSTHOG_API_KEY"], host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
callback_handler = CallbackHandler(client=posthog)


@tool
def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    """Get current weather for a location.

    Args:
        latitude: The latitude of the location
        longitude: The longitude of the location
        location_name: A human-readable name for the location
    """
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


tools = [get_weather]
tool_map = {t.name: t for t in tools}

model = ChatOpenAI(openai_api_key=os.environ["OPENAI_API_KEY"], temperature=0)
model_with_tools = model.bind_tools(tools)

messages = [HumanMessage(content="What's the weather in Berlin?")]

response = model_with_tools.invoke(messages, config={"callbacks": [callback_handler]})

if response.content:
    print(response.content)

if response.tool_calls:
    for tool_call in response.tool_calls:
        result = tool_map[tool_call["name"]].invoke(tool_call["args"])
        print(result)

posthog.shutdown()
