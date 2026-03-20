"""OpenAI Agents SDK single agent with tools, tracked by PostHog."""

import asyncio
import os
from typing import Annotated
from agents import Agent, Runner, function_tool
from posthog import Posthog
from posthog.ai.openai_agents import instrument

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
instrument(posthog, distinct_id="example-user")


@function_tool
def get_weather(city: Annotated[str, "The city to get weather for"]) -> str:
    """Get current weather for a city."""
    return f"Weather in {city}: 22°C, clear skies, humidity 45%"


@function_tool
def calculate(expression: Annotated[str, "A math expression to evaluate"]) -> str:
    """Evaluate a mathematical expression."""
    allowed = set("0123456789+-*/().^ ")
    if not all(c in allowed for c in expression):
        return "Error: invalid characters"
    return f"Result: {eval(expression.replace('^', '**'))}"


agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant with weather and math tools.",
    model="gpt-4o-mini",
    tools=[get_weather, calculate],
)


async def main():
    result = await Runner.run(agent, "What's 15% of 280?")
    print(result.final_output)


asyncio.run(main())
posthog.shutdown()
