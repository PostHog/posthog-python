"""Semantic Kernel with PostHog tracking via AsyncOpenAI wrapper."""

import os
import asyncio
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion
from posthog import Posthog
from posthog.ai.openai import AsyncOpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
openai_client = AsyncOpenAI(
    api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog
)

kernel = Kernel()
kernel.add_service(
    OpenAIChatCompletion(ai_model_id="gpt-4o-mini", async_client=openai_client)
)


async def main():
    result = await kernel.invoke_prompt("Tell me a fun fact about hedgehogs.")
    print(result)


asyncio.run(main())
posthog.shutdown()
