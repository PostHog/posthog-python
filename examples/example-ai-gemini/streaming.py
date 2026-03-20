"""Google Gemini streaming chat, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.gemini import Client

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = Client(api_key=os.environ["GEMINI_API_KEY"], posthog_client=posthog)

stream = client.models.generate_content_stream(
    model="gemini-2.5-flash",
    posthog_distinct_id="example-user",
    contents=[
        {
            "role": "user",
            "parts": [{"text": "Explain product analytics in three sentences."}],
        }
    ],
)

for chunk in stream:
    for candidate in chunk.candidates:
        for part in candidate.content.parts:
            if hasattr(part, "text"):
                print(part.text, end="", flush=True)

print()
posthog.shutdown()
