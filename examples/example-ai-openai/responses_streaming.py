"""OpenAI Responses API with streaming, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(os.environ["POSTHOG_API_KEY"], host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

stream = client.responses.create(
    model="gpt-4o-mini",
    max_output_tokens=1024,
    posthog_distinct_id="example-user",
    stream=True,
    instructions="You are a helpful assistant.",
    input=[{"role": "user", "content": "Write a haiku about product analytics."}],
)

for event in stream:
    if hasattr(event, "type") and event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)

print()
posthog.shutdown()
