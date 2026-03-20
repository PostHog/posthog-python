"""OpenAI image generation, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

# Note: posthog.ai does not wrap images.generate yet,
# so this call is not automatically tracked.
response = client.images.generate(
    model="gpt-image-1",
    prompt="A hedgehog wearing a PostHog t-shirt, pixel art style",
    size="1024x1024",
)

image_base64 = response.data[0].b64_json
print(f"Generated image: {len(image_base64)} chars of base64 data")

posthog.shutdown()
