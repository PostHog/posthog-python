"""OpenAI image generation via Responses API, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

response = client.responses.create(
    model="gpt-image-1-mini",
    input="A hedgehog wearing a PostHog t-shirt, pixel art style",
    tools=[{"type": "image_generation"}],
    posthog_distinct_id="example-user",
)

for output_item in response.output:
    if hasattr(output_item, "type") and output_item.type == "image_generation_call":
        image_base64 = output_item.result
        print(f"Generated image: {len(image_base64)} chars of base64 data")

posthog.shutdown()
