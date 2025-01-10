import os
import uuid
import asyncio

import posthog
from posthog.ai import OpenAI, AsyncOpenAI

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

    try:
        basic_openai_call()
        streaming_openai_call()
    except Exception as e:
        print("Error during OpenAI call:", str(e))

async def main_async():
    try:
        await basic_async_openai_call()
        await streaming_async_openai_call()
    except Exception as e:
        print("Error during OpenAI call:", str(e))


def basic_openai_call():
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a complex problem solver."}, {"role": "user", "content": "Explain quantum computing in simple terms."}],
        max_tokens=100,
        temperature=0.7,
    )
    if response and response.choices:
        print("OpenAI response:", response.choices[0].message.content)
    else:
        print("No response or unexpected format returned.")
    return response

async def basic_async_openai_call():
    response = await async_openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a complex problem solver."}, {"role": "user", "content": "Explain quantum computing in simple terms."}],
        max_tokens=100,
        temperature=0.7,
    )
    if response and hasattr(response, "choices"):
        print("OpenAI response:", response.choices[0].message.content)
    else:
        print("No response or unexpected format returned.")
    return response

def streaming_openai_call():
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a complex problem solver."}, {"role": "user", "content": "Explain quantum computing in simple terms."}],
        max_tokens=100,
        temperature=0.7,
        stream=True,
    )

    for chunk in response:
        print(chunk.choices[0].delta.content or "", end="")

    return response

async def streaming_async_openai_call():
    response = await async_openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a complex problem solver."}, {"role": "user", "content": "Explain quantum computing in simple terms."}],
        max_tokens=100,
        temperature=0.7,
        stream=True,
    )

    async for chunk in response:
        print(chunk.choices[0].delta.content or "", end="")

    return response

# HOW TO RUN:
# comment out one of these to run the other

if __name__ == "__main__":
    main_sync()

# asyncio.run(main_async())