"""Instructor structured extraction with PostHog tracking."""

import os
import instructor
from pydantic import BaseModel
from posthog import Posthog
from posthog.ai.openai import OpenAI

posthog = Posthog(
    os.environ["POSTHOG_API_KEY"],
    host=os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),
)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], posthog_client=posthog)
client = instructor.from_openai(openai_client)


class UserInfo(BaseModel):
    name: str
    age: int


user = client.chat.completions.create(
    model="gpt-4o-mini",
    response_model=UserInfo,
    messages=[{"role": "user", "content": "John Doe is 30 years old."}],
    posthog_distinct_id="example-user",
)

print(f"{user.name} is {user.age} years old")
posthog.shutdown()
