"""LiteLLM chat with PostHog tracking via built-in callback."""

import os
import json
import urllib.request
import litellm

# Enable PostHog callbacks — LiteLLM has built-in PostHog support
os.environ["POSTHOG_API_KEY"] = os.environ.get("POSTHOG_API_KEY", "")
os.environ["POSTHOG_API_URL"] = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
litellm.success_callback = ["posthog"]
litellm.failure_callback = ["posthog"]

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


# LiteLLM supports any model — just change the model string
response = litellm.completion(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are a helpful assistant with access to weather data."},
        {"role": "user", "content": "What's the weather in Paris?"},
    ],
    tools=tools,
    tool_choice="auto",
    metadata={"distinct_id": "example-user"},
)

message = response.choices[0].message

if message.content:
    print(message.content)

if hasattr(message, "tool_calls") and message.tool_calls:
    for tool_call in message.tool_calls:
        args = json.loads(tool_call.function.arguments)
        result = get_weather(**args)
        print(result)
