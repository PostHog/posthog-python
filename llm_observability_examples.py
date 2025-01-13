import os
import uuid

import posthog
from posthog.ai.openai import AsyncOpenAI, OpenAI

# Example credentials - replace these with your own or use environment variables
posthog.project_api_key = os.getenv("POSTHOG_PROJECT_API_KEY", "your-project-api-key")
posthog.personal_api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "your-personal-api-key")
posthog.host = os.getenv("POSTHOG_HOST", "http://localhost:8000")  # Or https://app.posthog.com
posthog.debug = True

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
    distinct_id = "test_distinct_id"
    properties = {"test_property": "test_value"}

    try:
        basic_openai_call(distinct_id, trace_id, properties)
        streaming_openai_call(distinct_id, trace_id, properties)
        non_instrumented_openai_call()
    except Exception as e:
        print("Error during OpenAI call:", str(e))


async def main_async():
    trace_id = str(uuid.uuid4())
    print("Trace ID:", trace_id)
    distinct_id = "test_distinct_id"
    properties = {"test_property": "test_value"}

    try:
        await basic_async_openai_call(distinct_id, trace_id, properties)
        await streaming_async_openai_call(distinct_id, trace_id, properties)
    except Exception as e:
        print("Error during OpenAI call:", str(e))


def basic_openai_call(distinct_id, trace_id, properties):
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
    )
    print(response)
    if response and response.choices:
        print("OpenAI response:", response.choices[0].message.content)
    else:
        print("No response or unexpected format returned.")
    return response


async def basic_async_openai_call(distinct_id, trace_id, properties):
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
    )
    if response and hasattr(response, "choices"):
        print("OpenAI response:", response.choices[0].message.content)
    else:
        print("No response or unexpected format returned.")
    return response


def streaming_openai_call(distinct_id, trace_id, properties):

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
    )

    for chunk in response:
        if hasattr(chunk, "choices") and chunk.choices and len(chunk.choices) > 0:
            print(chunk.choices[0].delta.content or "", end="")

    return response


async def streaming_async_openai_call(distinct_id, trace_id, properties):
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
    )

    async for chunk in response:
        if hasattr(chunk, "choices") and chunk.choices and len(chunk.choices) > 0:
            print(chunk.choices[0].delta.content or "", end="")

    return response


def non_instrumented_openai_call():
    response = openai_client.images.generate(model="dall-e-3", prompt="A cute baby hedgehog", n=1, size="1024x1024")
    print(response)
    return response


# HOW TO RUN:
# comment out one of these to run the other

if __name__ == "__main__":
    main_sync()

# asyncio.run(main_async())
