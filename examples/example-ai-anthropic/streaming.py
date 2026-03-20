"""Anthropic streaming chat, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.anthropic import Anthropic

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], posthog_client=posthog)

stream = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    posthog_distinct_id="example-user",
    messages=[{"role": "user", "content": "Write a haiku about observability."}],
    stream=True,
)

for event in stream:
    if hasattr(event, "type"):
        if event.type == "content_block_delta" and hasattr(event.delta, "text"):
            print(event.delta.text, end="", flush=True)

print()
posthog.shutdown()
