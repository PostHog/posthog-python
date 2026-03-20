"""LiteLLM streaming chat with PostHog tracking."""

import os
import litellm

os.environ["POSTHOG_API_KEY"] = os.environ.get("POSTHOG_API_KEY", "")
os.environ["POSTHOG_API_URL"] = os.environ.get(
    "POSTHOG_HOST", "https://us.i.posthog.com"
)
litellm.success_callback = ["posthog"]
litellm.failure_callback = ["posthog"]

response = litellm.completion(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain feature flags in three sentences."},
    ],
    stream=True,
    metadata={"distinct_id": "example-user"},
)

for chunk in response:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)

print()
