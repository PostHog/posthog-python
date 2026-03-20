"""OpenAI Chat Completions API with tool calling, tracked by PostHog."""

import os
import json
import urllib.request
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(os.environ["POSTHOG_API_KEY"], host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "location_name": {"type": "string"},
                },
                "required": ["latitude", "longitude", "location_name"],
            },
        },
    }
]


def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


response = client.chat.completions.create(
    model="gpt-4o-mini",
    max_completion_tokens=1024,
    posthog_distinct_id="example-user",
    tools=tools,
    tool_choice="auto",
    messages=[
        {"role": "system", "content": "You are a helpful assistant with access to weather data."},
        {"role": "user", "content": "What's the weather like in Dublin, Ireland?"},
    ],
)

message = response.choices[0].message

if message.content:
    print(message.content)

if message.tool_calls:
    for tool_call in message.tool_calls:
        args = json.loads(tool_call.function.arguments)
        result = get_weather(**args)
        print(result)

posthog.shutdown()
