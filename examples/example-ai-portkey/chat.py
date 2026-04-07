"""Portkey AI gateway chat completions, tracked by PostHog."""

import os
from portkey_ai import PORTKEY_GATEWAY_URL
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(
    base_url=PORTKEY_GATEWAY_URL,
    api_key=os.environ["PORTKEY_API_KEY"],
    posthog_client=posthog,
)

response = client.chat.completions.create(
    model="@openai/gpt-5-mini",
    max_completion_tokens=1024,
    posthog_distinct_id="example-user",
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
posthog.shutdown()
