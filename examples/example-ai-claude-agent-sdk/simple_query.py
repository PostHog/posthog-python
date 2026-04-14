"""Claude Agent SDK simple query, tracked by PostHog."""

import asyncio
import os

from claude_agent_sdk import ClaudeAgentOptions
from posthog import Posthog
from posthog.ai.claude_agent_sdk import query

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)


async def main():
    options = ClaudeAgentOptions(
        max_turns=2,
        permission_mode="plan",
    )

    async for message in query(
        prompt="What is 2 + 2? Reply in one sentence.",
        options=options,
        posthog_client=posthog,
        posthog_distinct_id="example-user",
        posthog_properties={"example": "simple_query"},
    ):
        print(f"[{type(message).__name__}]")


asyncio.run(main())
posthog.shutdown()
