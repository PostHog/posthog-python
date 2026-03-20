"""Anthropic chat with tool calling, tracked by PostHog."""

import os
import json
import urllib.request
from posthog import Posthog
from posthog.ai.anthropic import Anthropic

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], posthog_client=posthog)

tools = [
    {
        "name": "get_weather",
        "description": "Get current weather for a location",
        "input_schema": {
            "type": "object",
            "properties": {
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
                "location_name": {"type": "string"},
            },
            "required": ["latitude", "longitude", "location_name"],
        },
    }
]


def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    posthog_distinct_id="example-user",
    tools=tools,
    messages=[{"role": "user", "content": "What's the weather like in San Francisco?"}],
)

# Handle tool use if the model requests it.
# In production, send tool results back to the model for a final response.
for block in message.content:
    if block.type == "text":
        print(block.text)
    elif block.type == "tool_use":
        result = get_weather(**block.input)
        print(result)

posthog.shutdown()
