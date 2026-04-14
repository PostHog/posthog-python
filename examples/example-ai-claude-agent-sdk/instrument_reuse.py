"""Claude Agent SDK with instrument() for reusable config, tracked by PostHog."""

import asyncio
import os

from claude_agent_sdk import ClaudeAgentOptions, AssistantMessage, TextBlock
from posthog import Posthog
from posthog.ai.claude_agent_sdk import instrument

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)

# Configure once, reuse for multiple queries
ph = instrument(
    client=posthog,
    distinct_id="example-user",
    properties={"app": "demo", "environment": "development"},
)


async def ask(prompt: str) -> None:
    print(f"\n> {prompt}")
    options = ClaudeAgentOptions(
        max_turns=2,
        permission_mode="plan",
    )

    async for message in ph.query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"  {block.text}")


async def main():
    await ask("What is the capital of France? Reply in one sentence.")
    await ask("What is 15% of 280? Reply in one sentence.")


asyncio.run(main())
posthog.shutdown()
