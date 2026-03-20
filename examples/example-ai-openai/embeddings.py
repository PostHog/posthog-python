"""OpenAI embeddings, tracked by PostHog."""

import os
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

response = client.embeddings.create(
    model="text-embedding-3-small",
    input="PostHog is an open-source product analytics platform.",
    posthog_distinct_id="example-user",
)

embedding = response.data[0].embedding
print(f"Embedding dimensions: {len(embedding)}")
print(f"First 5 values: {embedding[:5]}")

posthog.shutdown()
