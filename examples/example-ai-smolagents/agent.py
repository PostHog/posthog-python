"""smolagents with PostHog tracking via OpenAI wrapper."""

import os
from smolagents import CodeAgent, OpenAIServerModel
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

model = OpenAIServerModel(model_id="gpt-4o-mini", client=openai_client)

agent = CodeAgent(tools=[], model=model)
result = agent.run("What is a fun fact about hedgehogs?")
print(result)

posthog.shutdown()
