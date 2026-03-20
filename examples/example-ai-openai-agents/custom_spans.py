"""OpenAI Agents SDK with custom spans for tracking custom operations, traced by PostHog."""

import asyncio
import os
from agents import Agent, Runner, trace
from agents.tracing import custom_span
from posthog import Posthog
from posthog.ai.openai_agents import instrument

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
instrument(posthog, distinct_id="example-user")

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
    model="gpt-4o-mini",
)


async def main():
    user_input = "Summarize the benefits of product analytics"

    # Wrap the workflow in a trace with custom spans for each stage
    with trace("processing_pipeline"):
        with custom_span(name="preprocess", data={"input_length": len(user_input)}):
            processed = user_input.strip().lower()

        with custom_span(name="validate", data={"input": processed}):
            is_valid = 0 < len(processed) < 1000

        if is_valid:
            with custom_span(name="llm_call"):
                result = await Runner.run(agent, user_input)
                print(result.final_output)


asyncio.run(main())
posthog.shutdown()
