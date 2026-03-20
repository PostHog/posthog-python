"""Google Gemini image generation, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.gemini import Client

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = Client(api_key=os.environ["GEMINI_API_KEY"], posthog_client=posthog)

response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    posthog_distinct_id="example-user",
    contents=[{"role": "user", "parts": [{"text": "Generate a pixel art hedgehog"}]}],
)

for candidate in response.candidates:
    for part in candidate.content.parts:
        if hasattr(part, "inline_data") and part.inline_data:
            print(
                f"Generated image: {part.inline_data.mime_type}, {len(part.inline_data.data)} bytes"
            )
        elif hasattr(part, "text"):
            print(part.text)

posthog.shutdown()
