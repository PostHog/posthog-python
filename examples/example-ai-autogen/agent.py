"""AutoGen with PostHog tracking via OpenAI wrapper."""

import os
import asyncio
from posthog import Posthog
from posthog.ai.openai import OpenAI
from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)

model_client = OpenAIChatCompletionClient(
    model="gpt-4o-mini",
    openai_client=openai_client,
)

agent = AssistantAgent("assistant", model_client=model_client)


async def main():
    result = await agent.run(task="Tell me a fun fact about hedgehogs.")
    print(result)
    await model_client.close()


asyncio.run(main())
posthog.shutdown()
