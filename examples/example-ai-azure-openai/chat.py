"""Azure OpenAI chat completions, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import AzureOpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21",
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    posthog_client=posthog,
)

response = client.chat.completions.create(
    model="gpt-4o",
    max_completion_tokens=1024,
    posthog_distinct_id="example-user",
    messages=[
        {"role": "user", "content": "Tell me a fun fact about hedgehogs."},
    ],
)

print(response.choices[0].message.content)
posthog.shutdown()
