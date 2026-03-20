"""OpenAI Chat Completions API with streaming, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(os.environ["POSTHOG_API_KEY"], host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

stream = client.chat.completions.create(
    model="gpt-4o-mini",
    max_completion_tokens=1024,
    posthog_distinct_id="example-user",
    stream=True,
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain observability in three sentences."},
    ],
)

for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)

print()
posthog.shutdown()
