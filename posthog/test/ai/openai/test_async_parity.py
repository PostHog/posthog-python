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

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from posthog.ai.openai import AsyncOpenAI, OpenAI
from posthog.test.ai.utils import make_response_usage

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
    assert sync_props["$ai_tokens_source"] == "sdk"
    assert async_props["$ai_tokens_source"] == sync_props["$ai_tokens_source"]
    assert async_props["$ai_cache_read_input_tokens"] == 0
    assert async_props["$ai_reasoning_tokens"] == 0

    missing = sorted(set(sync_props) - set(async_props))
    assert missing == [], (
        f"the async openai streaming path drops {missing} that the sync path sends"
    )


@pytest.mark.asyncio
async def test_responses_streaming_properties_have_sync_async_parity(mock_client):
    response = SimpleNamespace(
        model="gpt-4o-response",
        status="completed",
        usage=make_response_usage(11, 7, 18, cached_tokens=3),
        output=[
            SimpleNamespace(
                type="message",
                role="assistant",
                content=[SimpleNamespace(type="output_text", text="hello")],
            )
        ],
    )
    chunk = SimpleNamespace(type="response.completed", response=response)
    request = {
        "input": [{"role": "user", "content": "Hi"}],
        "stream": True,
        "posthog_distinct_id": "test-id",
        "posthog_trace_id": "shared-trace",
        "posthog_provider_override": "groq",
    }

    with patch(
        "openai.resources.responses.Responses.create", return_value=iter([chunk])
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        list(client.responses.create(**request))
    sync_props = mock_client.capture.call_args.kwargs["properties"]

    async def create(self, **kwargs):
        async def chunks():
            yield chunk

        return chunks()

    mock_client.capture.reset_mock()
    with patch("openai.resources.responses.AsyncResponses.create", new=create):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)
        stream = await client.responses.create(**request)
        async for _ in stream:
            pass
    async_props = mock_client.capture.call_args.kwargs["properties"]

    sync_without_latency = {k: v for k, v in sync_props.items() if k != "$ai_latency"}
    async_without_latency = {k: v for k, v in async_props.items() if k != "$ai_latency"}
    assert async_without_latency == sync_without_latency
    assert async_props["$ai_model"] == "gpt-4o-response"
    assert async_props["$ai_stop_reason"] == "completed"
    assert async_props["$ai_provider"] == "groq"


def test_sync_stream_close_after_early_exit_captures_partial_state(
    mock_client, streaming_tool_call_chunks
):
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=iter(streaming_tool_call_chunks),
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        stream = client.chat.completions.create(
            model="gpt-4",
            messages=MESSAGES,
            stream=True,
            posthog_distinct_id="test-id",
        )
        assert next(stream) == streaming_tool_call_chunks[0]
        stream.close()

    assert mock_client.capture.call_count == 1
    assert mock_client.capture.call_args.kwargs["properties"]["$ai_model"] == "gpt-4"
