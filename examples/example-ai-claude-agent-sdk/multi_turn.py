"""Claude Agent SDK multi-turn conversation with history, tracked by PostHog."""

import asyncio
import os

from claude_agent_sdk import AssistantMessage, ResultMessage
from claude_agent_sdk.types import ClaudeAgentOptions, TextBlock
from posthog import Posthog
from posthog.ai.claude_agent_sdk import PostHogClaudeSDKClient

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)


async def main():
    options = ClaudeAgentOptions(
        max_turns=5,
        permission_mode="plan",
    )

    async with PostHogClaudeSDKClient(
        options,
        posthog_client=posthog,
        posthog_distinct_id="example-user",
        posthog_properties={"example": "multi_turn"},
    ) as client:
        # Turn 1
        print("> What is the capital of France?")
        await client.query("What is the capital of France? Reply in one sentence.")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"  {block.text}")
            elif isinstance(message, ResultMessage):
                print(f"  [{message.num_turns} turns, ${message.total_cost_usd:.4f}]")

        # Turn 2 — has full conversation history
        print("\n> And what language do they speak there?")
        await client.query(
            "And what language do they speak there? Reply in one sentence."
        )
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"  {block.text}")
            elif isinstance(message, ResultMessage):
                print(f"  [{message.num_turns} turns, ${message.total_cost_usd:.4f}]")

        # Turn 3 — still has context from both previous turns
        print("\n> How do you say 'hello' in that language?")
        await client.query(
            "How do you say 'hello' in that language? Reply in one sentence."
        )
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"  {block.text}")
            elif isinstance(message, ResultMessage):
                print(f"  [{message.num_turns} turns, ${message.total_cost_usd:.4f}]")


asyncio.run(main())
posthog.shutdown()
