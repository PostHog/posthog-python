import os
import uuid

from pydantic import BaseModel

import posthog
from posthog.ai.openai import AsyncOpenAI, OpenAI

# Example credentials - replace these with your own or use environment variables
posthog.project_api_key = os.getenv("POSTHOG_PROJECT_API_KEY", "your-project-api-key")
posthog.host = os.getenv("POSTHOG_HOST", "http://localhost:8000")  # Or https://app.posthog.com
posthog.debug = True
# change this to False to see usage events
# posthog.privacy_mode = True

openai_client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "your-openai-api-key"),
    posthog_client=posthog,
)

async_openai_client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "your-openai-api-key"),
    posthog_client=posthog,
)


def main_sync():
    trace_id = str(uuid.uuid4())
    print("Trace ID:", trace_id)
    distinct_id = "test2_distinct_id"
    properties = {"test_property": "test_value"}
    groups = {"company": "test_company"}

    try:
        # basic_openai_call(distinct_id, trace_id, properties, groups)
        # streaming_openai_call(distinct_id, trace_id, properties, groups)
        # embedding_openai_call(distinct_id, trace_id, properties, groups)
        # image_openai_call()
        beta_openai_call(distinct_id, trace_id, properties, groups)
    except Exception as e:
        print("Error during OpenAI call:", str(e))


async def main_async():
    trace_id = str(uuid.uuid4())
    print("Trace ID:", trace_id)
    distinct_id = "test_distinct_id"
    properties = {"test_property": "test_value"}
    groups = {"company": "test_company"}

    try:
        await basic_async_openai_call(distinct_id, trace_id, properties, groups)
        await streaming_async_openai_call(distinct_id, trace_id, properties, groups)
        await embedding_async_openai_call(distinct_id, trace_id, properties, groups)
        await image_async_openai_call()
    except Exception as e:
        print("Error during OpenAI call:", str(e))


def basic_openai_call(distinct_id, trace_id, properties, groups):
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a complex problem solver."},
            {"role": "user", "content": "Explain quantum computing in simple terms."},
        ],
        max_tokens=100,
        temperature=0.7,
        posthog_distinct_id=distinct_id,
        posthog_trace_id=trace_id,
        posthog_properties=properties,
        posthog_groups=groups,
    )
    print(response)
    if response and response.choices:
        print("OpenAI response:", response.choices[0].message.content)
    else:
        print("No response or unexpected format returned.")
    return response


async def basic_async_openai_call(distinct_id, trace_id, properties, groups):
    response = await async_openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a complex problem solver."},
            {"role": "user", "content": "Explain quantum computing in simple terms."},
        ],
        max_tokens=100,
        temperature=0.7,
        posthog_distinct_id=distinct_id,
        posthog_trace_id=trace_id,
        posthog_properties=properties,
        posthog_groups=groups,
    )
    if response and hasattr(response, "choices"):
        print("OpenAI response:", response.choices[0].message.content)
    else:
        print("No response or unexpected format returned.")
    return response


def streaming_openai_call(distinct_id, trace_id, properties, groups):

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a complex problem solver."},
            {"role": "user", "content": "Explain quantum computing in simple terms."},
        ],
        max_tokens=100,
        temperature=0.7,
        stream=True,
        posthog_distinct_id=distinct_id,
        posthog_trace_id=trace_id,
        posthog_properties=properties,
        posthog_groups=groups,
    )

    for chunk in response:
        if hasattr(chunk, "choices") and chunk.choices and len(chunk.choices) > 0:
            print(chunk.choices[0].delta.content or "", end="")

    return response


async def streaming_async_openai_call(distinct_id, trace_id, properties, groups):
    response = await async_openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a complex problem solver."},
            {"role": "user", "content": "Explain quantum computing in simple terms."},
        ],
        max_tokens=100,
        temperature=0.7,
        stream=True,
        posthog_distinct_id=distinct_id,
        posthog_trace_id=trace_id,
        posthog_properties=properties,
        posthog_groups=groups,
    )

    async for chunk in response:
        if hasattr(chunk, "choices") and chunk.choices and len(chunk.choices) > 0:
            print(chunk.choices[0].delta.content or "", end="")

    return response


# none instrumented
def image_openai_call():
    response = openai_client.images.generate(model="dall-e-3", prompt="A cute baby hedgehog", n=1, size="1024x1024")
    print(response)
    return response


# none instrumented
async def image_async_openai_call():
    response = await async_openai_client.images.generate(
        model="dall-e-3", prompt="A cute baby hedgehog", n=1, size="1024x1024"
    )
    print(response)
    return response


def embedding_openai_call(posthog_distinct_id, posthog_trace_id, posthog_properties, posthog_groups):
    response = openai_client.embeddings.create(
        input="The hedgehog is cute",
        model="text-embedding-3-small",
        posthog_distinct_id=posthog_distinct_id,
        posthog_trace_id=posthog_trace_id,
        posthog_properties=posthog_properties,
        posthog_groups=posthog_groups,
    )
    print(response)
    return response


async def embedding_async_openai_call(posthog_distinct_id, posthog_trace_id, posthog_properties, posthog_groups):
    response = await async_openai_client.embeddings.create(
        input="The hedgehog is cute",
        model="text-embedding-3-small",
        posthog_distinct_id=posthog_distinct_id,
        posthog_trace_id=posthog_trace_id,
        posthog_properties=posthog_properties,
        posthog_groups=posthog_groups,
    )
    print(response)
    return response


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]


def beta_openai_call(distinct_id, trace_id, properties, groups):
    response = openai_client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Extract the event information."},
            {"role": "user", "content": "Alice and Bob are going to a science fair on Friday."},
        ],
        response_format=CalendarEvent,
        posthog_distinct_id=distinct_id,
        posthog_trace_id=trace_id,
        posthog_properties=properties,
        posthog_groups=groups,
    )
    print(response)
    return response


# HOW TO RUN:
# comment out one of these to run the other

if __name__ == "__main__":
    main_sync()
# asyncio.run(main_async())
