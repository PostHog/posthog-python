"""Mirascope with PostHog tracking via OpenAI wrapper."""

import os
from mirascope.llm import call
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)


@call(model="openai/gpt-4o-mini", client=openai_client)
def recommend_book(genre: str):
    return f"Recommend a {genre} book."


response = recommend_book("fantasy")

print(response.content)
posthog.shutdown()
