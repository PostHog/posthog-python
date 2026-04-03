"""Hugging Face chat completions via OpenAI-compatible API, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(
    base_url="https://router.huggingface.co/v1/",
    api_key=os.environ["HUGGINGFACE_API_KEY"],
    posthog_client=posthog,
)

response = client.chat.completions.create(
    model="meta-llama/Llama-3.3-70B-Instruct",
    max_completion_tokens=1024,
    posthog_distinct_id="example-user",
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
posthog.shutdown()
