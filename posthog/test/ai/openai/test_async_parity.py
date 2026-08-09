"""
End-to-end regression test for the openai async streaming property gap.

Drives the real wrapped clients through a mocked OpenAI stream, sync and async, and asserts
the async twin emits the same $ai_generation properties the sync twin does. Reuses posthog's
own fixtures so the stream shape is theirs, not mine.

FAILS on main. Would pass if the async twin called posthog.ai.utils.capture_streaming_event
the way the sync twin, anthropic and gemini all do.

  cp test_async_parity.py repo-posthog/posthog/test/ai/openai/test_async_parity.py
  .venv-posthog/bin/python -m pytest repo-posthog/posthog/test/ai/openai/test_async_parity.py -v
"""

from unittest.mock import patch

import pytest

from posthog.ai.openai import OpenAI, AsyncOpenAI

# fixtures mock_client and streaming_tool_call_chunks come from test_openai.py in this dir
from posthog.test.ai.openai.test_openai import (  # noqa: F401
    mock_client,
    streaming_tool_call_chunks,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {},
        },
    }
]
MESSAGES = [{"role": "user", "content": "What's the weather in San Francisco?"}]


def _sync_props(mock_client, chunks):
    with patch("openai.resources.chat.completions.Completions.create") as create:
        create.return_value = chunks
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        list(
            client.chat.completions.create(
                model="gpt-4",
                messages=MESSAGES,
                tools=TOOLS,
                stream=True,
                posthog_distinct_id="test-id",
            )
        )
    return mock_client.capture.call_args[1]["properties"]


async def _async_props(mock_client, chunks):
    async def create(self, **kwargs):
        async def it():
            for chunk in chunks:
                yield chunk

        return it()

    with patch("openai.resources.chat.completions.AsyncCompletions.create", new=create):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)
        stream = await client.chat.completions.create(
            model="gpt-4",
            messages=MESSAGES,
            tools=TOOLS,
            stream=True,
            posthog_distinct_id="test-id",
        )
        async for _ in stream:
            pass
    return mock_client.capture.call_args[1]["properties"]


@pytest.mark.asyncio
async def test_async_streaming_emits_the_same_properties_as_sync(
    mock_client, streaming_tool_call_chunks
):
    sync_props = _sync_props(mock_client, streaming_tool_call_chunks)
    mock_client.capture.reset_mock()
    async_props = await _async_props(mock_client, streaming_tool_call_chunks)

    # Guard: if the sync side stopped emitting these, the comparison below is vacuous.
    assert "$ai_usage" in sync_props
    assert "$ai_tokens_source" in sync_props

    missing = sorted(set(sync_props) - set(async_props))
    assert missing == [], (
        f"the async openai streaming path drops {missing} that the sync path sends"
    )
