"""Google Gemini chat with tool calling, tracked by PostHog."""

import os
import json
import urllib.request
from google.genai import types
from posthog import Posthog
from posthog.ai.gemini import Client

posthog = Posthog(os.environ["POSTHOG_API_KEY"], host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
client = Client(api_key=os.environ["GEMINI_API_KEY"], posthog_client=posthog)

tool_declarations = [
    {
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
    }
]


def get_weather(latitude: float, longitude: float, location_name: str) -> str:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    current = data["current"]
    return f"Weather in {location_name}: {current['temperature_2m']}°C, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} km/h"


config = types.GenerateContentConfig(
    tools=[types.Tool(function_declarations=tool_declarations)]
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    posthog_distinct_id="example-user",
    contents=[{"role": "user", "parts": [{"text": "What's the weather in London?"}]}],
    config=config,
)

for candidate in response.candidates:
    for part in candidate.content.parts:
        if hasattr(part, "function_call") and part.function_call:
            result = get_weather(
                latitude=part.function_call.args["latitude"],
                longitude=part.function_call.args["longitude"],
                location_name=part.function_call.args["location_name"],
            )
            print(result)
        elif hasattr(part, "text"):
            print(part.text)

posthog.shutdown()
