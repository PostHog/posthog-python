"""Stateful Claude client delivery, turn boundaries, and teardown contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk.types import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from posthog.ai.claude_agent_sdk import PostHogClaudeSDKClient  # noqa: E402
from posthog.ai.claude_agent_sdk.processor import (  # noqa: E402
    PostHogClaudeAgentProcessor,
)


def stream(kind, **data):
    return StreamEvent(uuid="evt", session_id="session", event={"type": kind, **data})


def result(**overrides):
    return ResultMessage(
        **{
            "subtype": "success",
            "duration_ms": 2000,
            "duration_api_ms": 1500,
            "is_error": False,
            "num_turns": 1,
            "session_id": "session",
            "usage": {"input_tokens": 10, "output_tokens": 0},
            "total_cost_usd": 0.01,
            **overrides,
        }
    )


@pytest.fixture
def sdk(monkeypatch):
    client = SimpleNamespace(
        connect=AsyncMock(),
        query=AsyncMock(),
        disconnect=AsyncMock(),
        interrupt=AsyncMock(),
        set_permission_mode=AsyncMock(),
        set_model=AsyncMock(),
    )
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(
        "posthog.ai.claude_agent_sdk.client.ClaudeSDKClient", constructor
    )
    client.constructor = constructor
    return client


def respond(sdk, messages):
    async def receive():
        for message in messages:
            yield message

    sdk.receive_response = receive


def analytics():
    return MagicMock(privacy_mode=False, enable_full_ai_capture=False)


def events(ph, name):
    return [
        call.kwargs
        for call in ph.capture.call_args_list
        if call.kwargs["event"] == name
    ]


@pytest.mark.asyncio
async def test_multiturn_stream_tool_parentage_input_promotion_and_final_trace(sdk):
    ph = analytics()
    tool = ToolUseBlock(id="tool-1", name="lookup", input={"query": "stock"})
    first = [
        stream(
            "message_start", message={"model": "claude", "usage": {"input_tokens": 12}}
        ),
        AssistantMessage(model="claude", content=[TextBlock(text="checking"), tool]),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tool-1",
                    content=[
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "A" * 400,
                            },
                        }
                    ],
                ),
                TextBlock(text="tool done"),
            ]
        ),
        # Empty/unrecognized content must not overwrite already queued tool results.
        UserMessage(content=[]),
        stream(
            "message_delta",
            usage={"output_tokens": 4},
            delta={"stop_reason": "tool_use"},
        ),
        stream("message_stop"),
        stream(
            "message_start", message={"model": "claude", "usage": {"input_tokens": 20}}
        ),
        AssistantMessage(model="claude", content=[TextBlock(text="answer")]),
        UserMessage(content="next input"),
        stream("message_delta", usage={"output_tokens": 2}),
        stream("message_stop"),
        result(),
    ]
    async with PostHogClaudeSDKClient(
        posthog_client=ph,
        posthog_distinct_id="user",
        posthog_trace_id="trace",
        posthog_properties={"environment": "test"},
        posthog_groups={"team": "sdk"},
    ) as client:
        await client.query("question", session_id="session")
        respond(sdk, first)
        assert [msg async for msg in client.receive_response()] == first
        assert not events(ph, "$ai_trace")
        generations = events(ph, "$ai_generation")
        assert len(generations) == 2
        one, two = [event["properties"] for event in generations]
        assert one["$ai_input"] == [{"role": "user", "content": "question"}]
        assert one["$ai_output_choices"][0]["content"][0] == {
            "type": "text",
            "text": "checking",
        }
        assert one["$ai_input_tokens"] == 12
        assert one["$ai_output_tokens"] == 4
        assert one["$ai_stop_reason"] == "tool_use"
        assert two["$ai_input"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "[base64 image redacted]",
                                },
                            },
                        ],
                    },
                    {"type": "text", "text": "tool done"},
                ],
            }
        ]
        span = events(ph, "$ai_span")[0]["properties"]
        assert span["$ai_parent_id"] == one["$ai_span_id"]
        assert span["$ai_input_state"] == {"query": "stock"}
        assert span["$ai_span_name"] == "lookup"

        await client.query("follow up")
        second = [
            stream("message_start", message={"model": "claude", "usage": {}}),
            AssistantMessage(model="claude", content=[]),
            stream("message_stop"),
            result(),
        ]
        respond(sdk, second)
        assert [msg async for msg in client.receive_response()] == second
        third = events(ph, "$ai_generation")[2]["properties"]
        assert third["$ai_span_name"] == "generation_3"
        assert third["$ai_input"] == [{"role": "user", "content": "follow up"}]
        assert "$ai_output_choices" not in third
        assert "$ai_input_tokens" not in third
        assert not events(ph, "$ai_trace")
    traces = events(ph, "$ai_trace")
    assert len(traces) == 1
    assert traces[0]["properties"]["$ai_trace_name"] == "claude_agent_sdk_session"
    assert {
        event["properties"]["$ai_trace_id"] for event in events(ph, "$ai_generation")
    } == {"trace"}
    for event in ph.capture.call_args_list:
        assert event.kwargs["distinct_id"] == "user"
        assert event.kwargs["groups"] == {"team": "sdk"}
        assert event.kwargs["properties"]["environment"] == "test"
    ph.flush.assert_called_once_with()
    sdk.connect.assert_awaited_once_with(None)
    assert sdk.query.await_args_list[0].args == ("question", "session")
    sdk.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_result_fallback_and_privacy_without_person_identity(sdk, private):
    ph = analytics()
    client = PostHogClaudeSDKClient(
        posthog_client=ph, posthog_trace_id="trace", posthog_privacy_mode=private
    )
    await client.query("secret")
    messages = [
        AssistantMessage(model="claude", content=[TextBlock(text="response")]),
        result(),
    ]
    respond(sdk, messages)
    assert [msg async for msg in client.receive_response()] == messages
    generation = events(ph, "$ai_generation")[0]
    props = generation["properties"]
    assert generation["distinct_id"] == "trace"
    assert props["$process_person_profile"] is False
    assert props["$ai_input"] == (
        None if private else [{"role": "user", "content": "secret"}]
    )
    assert props["$ai_output_choices"] == (
        None
        if private
        else [{"role": "assistant", "content": [{"type": "text", "text": "response"}]}]
    )
    assert props["$ai_input_tokens"] == 10
    assert props["$ai_output_tokens"] == 0
    assert props["$ai_latency"] == 1.5
    assert props["$ai_total_cost_usd"] == 0.01
    assert not events(ph, "$ai_trace")
    await client.disconnect()
    assert events(ph, "$ai_trace")[0]["properties"]["$process_person_profile"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [None, False, True])
async def test_options_are_copied_and_delegation_preserves_arguments(sdk, partial):
    options = (
        None
        if partial is None
        else ClaudeAgentOptions(
            include_partial_messages=partial, system_prompt="system"
        )
    )
    transport = object()
    client = PostHogClaudeSDKClient(options, transport, posthog_client=analytics())
    actual, actual_transport = sdk.constructor.call_args.args
    assert actual.include_partial_messages is True
    assert actual_transport is transport
    if options is not None:
        assert options.include_partial_messages is partial
        assert actual.system_prompt == "system"
        assert (actual is options) is partial
    await client.connect("initial")
    await client.interrupt()
    await client.set_permission_mode("acceptEdits")
    await client.set_model("claude")
    await client.set_model()
    sdk.connect.assert_awaited_once_with("initial")
    sdk.interrupt.assert_awaited_once_with()
    sdk.set_permission_mode.assert_awaited_once_with("acceptEdits")
    assert [call.args for call in sdk.set_model.await_args_list] == [
        ("claude",),
        (None,),
    ]


@pytest.mark.asyncio
async def test_instrumentation_failures_never_interrupt_delivery_or_disconnect(sdk):
    ph = analytics()
    ph.capture.side_effect = RuntimeError("capture failure")
    ph.flush.side_effect = RuntimeError("flush failure")
    client = PostHogClaudeSDKClient(posthog_client=ph)
    await client.query("prompt")
    messages = [stream("message_start", message=None), result()]
    respond(sdk, messages)
    assert [msg async for msg in client.receive_response()] == messages
    await client.disconnect()
    sdk.disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_context_manager_propagates_application_exception_and_disconnects(sdk):
    ph = analytics()
    with pytest.raises(ValueError, match="application failure"):
        async with PostHogClaudeSDKClient(posthog_client=ph):
            raise ValueError("application failure")
    sdk.disconnect.assert_awaited_once_with()
    assert len(events(ph, "$ai_trace")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, "person", 123])
async def test_processor_callable_identity_receives_result_only(
    sdk, monkeypatch, resolved
):
    ph = analytics()
    resolve = MagicMock(return_value=resolved)
    processor = PostHogClaudeAgentProcessor(client=ph, distinct_id=resolve)
    message = result()

    async def query(**kwargs):
        yield message

    monkeypatch.setattr("posthog.ai.claude_agent_sdk.processor.original_query", query)
    assert [msg async for msg in processor.query(prompt="question")] == [message]
    resolve.assert_called_once_with(message)
    generation = events(ph, "$ai_generation")[0]
    trace = events(ph, "$ai_trace")[0]
    assert generation["distinct_id"] == generation["properties"]["$ai_trace_id"]
    assert generation["properties"]["$process_person_profile"] is False
    assert trace["distinct_id"] == (
        str(resolved) if resolved else trace["properties"]["$ai_trace_id"]
    )
    assert ("$process_person_profile" in trace["properties"]) is (not bool(resolved))


@pytest.mark.parametrize("resolved", [None, "person", 123, ""])
def test_processor_identity_fallback_defers_callable_until_result(resolved):
    """Generation events have no result; only the final trace may resolve a callable ID."""
    resolve = MagicMock(return_value=resolved)
    processor = PostHogClaudeAgentProcessor(client=analytics(), distinct_id=resolve)
    assert processor._resolve_distinct_id(None) is None
    resolve.assert_not_called()
    message = result()
    assert processor._resolve_distinct_id(None, message) == (
        str(resolved) if resolved else None
    )
    resolve.assert_called_once_with(message)


@pytest.mark.asyncio
async def test_processor_cancellation_propagates_without_synthetic_generation_or_trace(
    monkeypatch,
):
    import asyncio

    entered = asyncio.Event()
    released = asyncio.Event()
    finalized = asyncio.Event()

    async def query(**kwargs):
        try:
            yield stream("message_start", message={"model": "claude", "usage": {}})
            entered.set()
            await released.wait()
        finally:
            finalized.set()

    monkeypatch.setattr("posthog.ai.claude_agent_sdk.processor.original_query", query)
    ph = analytics()
    processor = PostHogClaudeAgentProcessor(client=ph)

    async def consume():
        return [message async for message in processor.query(prompt="question")]

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized.is_set()
    ph.capture.assert_not_called()
    ph.flush.assert_not_called()
