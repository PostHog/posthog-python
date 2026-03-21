"""OpenAI Agents SDK multi-agent with handoffs, tracked by PostHog."""

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
    return f"Weather in {city}: 18°C, partly cloudy, humidity 65%"


@function_tool
def calculate(expression: Annotated[str, "A math expression to evaluate"]) -> str:
    """Evaluate a mathematical expression."""
    allowed = set("0123456789+-*/().^ ")
    if not all(c in allowed for c in expression):
        return "Error: invalid characters"
    return f"Result: {eval(expression.replace('^', '**'))}"


weather_agent = Agent(
    name="WeatherAgent",
    instructions="You handle weather queries. Use the get_weather tool.",
    model="gpt-4o-mini",
    tools=[get_weather],
)

math_agent = Agent(
    name="MathAgent",
    instructions="You handle math problems. Use the calculate tool.",
    model="gpt-4o-mini",
    tools=[calculate],
)

general_agent = Agent(
    name="GeneralAgent",
    instructions="You handle general questions and conversation.",
    model="gpt-4o-mini",
)

triage_agent = Agent(
    name="TriageAgent",
    instructions="Route to WeatherAgent for weather, MathAgent for math, GeneralAgent for everything else.",
    model="gpt-4o-mini",
    handoffs=[weather_agent, math_agent, general_agent],
)


async def main():
    result = await Runner.run(triage_agent, "What's the weather in Tokyo?")
    print(result.final_output)


asyncio.run(main())
posthog.shutdown()
