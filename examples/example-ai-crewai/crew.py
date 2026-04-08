"""CrewAI with PostHog tracking via LiteLLM callbacks."""

import os
import litellm
from crewai import Agent, Task, Crew

os.environ["POSTHOG_API_KEY"] = os.environ.get("POSTHOG_API_KEY", "")
os.environ["POSTHOG_API_URL"] = os.environ.get(
    "POSTHOG_HOST", "https://us.i.posthog.com"
)
litellm.success_callback = ["posthog"]
litellm.failure_callback = ["posthog"]

researcher = Agent(
    role="Researcher",
    goal="Find interesting facts about hedgehogs",
    backstory="You are an expert wildlife researcher.",
)

task = Task(
    description="Research three fun facts about hedgehogs.",
    expected_output="A list of three fun facts.",
    agent=researcher,
)

crew = Crew(
    agents=[researcher],
    tasks=[task],
)

result = crew.kickoff()
print(result)
