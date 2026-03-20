"""Anthropic extended thinking, tracked by PostHog.

Extended thinking lets Claude show its reasoning process before responding.
"""

import os
from posthog import Posthog
from posthog.ai.anthropic import Anthropic

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], posthog_client=posthog)

message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=16000,
    posthog_distinct_id="example-user",
    thinking={"type": "enabled", "budget_tokens": 10000},
    messages=[
        {
            "role": "user",
            "content": "What is the probability of rolling at least one six in four rolls of a fair die?",
        }
    ],
)

for block in message.content:
    if block.type == "thinking":
        print(f"Thinking: {block.thinking}\n")
    elif block.type == "text":
        print(f"Answer: {block.text}")

posthog.shutdown()
