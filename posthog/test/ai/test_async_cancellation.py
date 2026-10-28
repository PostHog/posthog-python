import asyncio

import pytest

from posthog import Posthog
from posthog.ai.utils import call_llm_and_track_usage_async


@pytest.fixture
def captured_generations():
    events = []

    def before_send(event):
        if event["event"] == "$ai_generation":
            events.append(event)
        return None

    client = Posthog("phc_test", before_send=before_send, sync_mode=True)
    try:
        yield client, events
    finally:
        client.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "openai", "anthropic"])
async def test_cancelled_call_captures_error_and_preserves_cancellation(
    captured_generations, provider
):
    client, events = captured_generations
    started = asyncio.Event()

    async def slow_call(**kwargs):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        call_llm_and_track_usage_async(
            "user-1",
            client,
            provider,
            "trace-1",
            None,
            False,
            None,
            "",
            slow_call,
            model="test-model",
        )
    )
    await started.wait()
    task.cancel("request cancelled")

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$ai_http_status"] == 0
    assert props["$ai_is_error"] is True
    assert props["$ai_error"] == "cancelled"
    assert props["$ai_provider"] == provider
    assert props["$ai_trace_id"] == "trace-1"
    assert "$ai_input_tokens" not in props
    assert "$ai_output_tokens" not in props


@pytest.mark.asyncio
async def test_provider_error_is_captured_and_reraised(captured_generations):
    client, events = captured_generations
    error = RuntimeError("provider error")

    async def failing_call(**kwargs):
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        await call_llm_and_track_usage_async(
            None, client, "gemini", None, None, False, None, "", failing_call
        )

    assert exc_info.value is error
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$ai_http_status"] == 0
    assert props["$ai_is_error"] is True
    assert props["$ai_error"] == "provider error"
