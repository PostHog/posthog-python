"""LlamaIndex with PostHog tracking via OpenAI wrapper."""

import os
from llama_index.llms.openai import OpenAI as LlamaOpenAI
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

llm = LlamaOpenAI(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"])
llm._client = openai_client

response = llm.complete("Tell me a fun fact about hedgehogs.")
print(response)

posthog.shutdown()
