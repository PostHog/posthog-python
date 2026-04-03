from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

try:
    from claude_agent_sdk.types import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        StreamEvent,
        TextBlock,
        ToolUseBlock,
    )

    from posthog.ai.claude_agent_sdk import (
        PostHogClaudeAgentProcessor,
        instrument,
    )

    CLAUDE_AGENT_SDK_AVAILABLE = True
except ImportError:
    CLAUDE_AGENT_SDK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CLAUDE_AGENT_SDK_AVAILABLE, reason="Claude Agent SDK is not available"
)


# ── Helpers ──────────────────────────────────────────────────────


def _make_stream_event(
    event_type: str, data: Optional[Dict[str, Any]] = None, session_id: str = "sess_123"
) -> StreamEvent:
    event = {"type": event_type, **(data or {})}
    return StreamEvent(uuid="evt_1", session_id=session_id, event=event)


def _make_message_start(
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 100,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> StreamEvent:
    return _make_stream_event(
        "message_start",
        {
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                },
            },
        },
    )


def _make_message_delta(output_tokens: int = 50) -> StreamEvent:
    return _make_stream_event(
        "message_delta",
        {
            "usage": {"output_tokens": output_tokens},
        },
    )


def _make_message_stop() -> StreamEvent:
    return _make_stream_event("message_stop")


def _make_assistant_message(
    model: str = "claude-sonnet-4-6",
    text: str = "Hello!",
    tool_uses: Optional[List[Dict[str, Any]]] = None,
) -> AssistantMessage:
    content = [TextBlock(text=text)]
    if tool_uses:
        for tu in tool_uses:
            content.append(
                ToolUseBlock(id=tu["id"], name=tu["name"], input=tu.get("input", {}))
            )
    return AssistantMessage(content=content, model=model)


def _make_result_message(
    total_cost_usd: float = 0.01,
    input_tokens: int = 100,
    output_tokens: int = 50,
    duration_ms: int = 2000,
    duration_api_ms: int = 1500,
    num_turns: int = 3,
    is_error: bool = False,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> ResultMessage:
    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=duration_ms,
        duration_api_ms=duration_api_ms,
        is_error=is_error,
        num_turns=num_turns,
        session_id="sess_123",
        total_cost_usd=total_cost_usd,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    )


async def _fake_query(messages):
    """Create a fake query function that yields pre-defined messages."""
    for msg in messages:
        yield msg


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.privacy_mode = False
    logging.getLogger("posthog").setLevel(logging.DEBUG)
    return client


@pytest.fixture
def processor(mock_client):
    return PostHogClaudeAgentProcessor(
        client=mock_client,
        distinct_id="test-user",
        privacy_mode=False,
    )


# ── Tests ────────────────────────────────────────────────────────


class TestPostHogClaudeAgentProcessor:
    def test_initialization(self, mock_client):
        proc = PostHogClaudeAgentProcessor(
            client=mock_client,
            distinct_id="user@example.com",
            privacy_mode=True,
            groups={"company": "acme"},
            properties={"env": "test"},
        )
        assert proc._client == mock_client
        assert proc._distinct_id == "user@example.com"
        assert proc._privacy_mode is True
        assert proc._groups == {"company": "acme"}
        assert proc._properties == {"env": "test"}

    def test_initialization_defaults(self):
        with patch("posthog.ai.claude_agent_sdk.processor.setup") as mock_setup:
            mock_setup.return_value = MagicMock()
            proc = PostHogClaudeAgentProcessor()
            assert proc._distinct_id is None
            assert proc._privacy_mode is False
            assert proc._groups == {}
            assert proc._properties == {}


class TestGenerationEmission:
    @pytest.mark.asyncio
    async def test_emits_generation_from_stream_events(self, processor, mock_client):
        messages = [
            _make_message_start(input_tokens=100, cache_read=20),
            _make_message_delta(output_tokens=50),
            _make_message_stop(),
            _make_assistant_message(),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            collected = []
            async for msg in processor.query(prompt="Hi", options=ClaudeAgentOptions()):
                collected.append(msg)

        # Should have captured $ai_generation + $ai_trace
        calls = mock_client.capture.call_args_list
        events = [c.kwargs.get("event") or c[1].get("event") for c in calls]
        assert "$ai_generation" in events
        assert "$ai_trace" in events

        # Check generation properties
        gen_call = next(
            c
            for c in calls
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_generation"
        )
        props = gen_call.kwargs.get("properties") or gen_call[1].get("properties")
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_framework"] == "claude-agent-sdk"
        assert props["$ai_model"] == "claude-sonnet-4-6"
        assert props["$ai_input_tokens"] == 100
        assert props["$ai_output_tokens"] == 50
        assert props["$ai_cache_read_input_tokens"] == 20

    @pytest.mark.asyncio
    async def test_emits_multiple_generations_for_multi_turn(
        self, processor, mock_client
    ):
        messages = [
            # Turn 1
            _make_message_start(input_tokens=50),
            _make_message_delta(output_tokens=30),
            _make_message_stop(),
            _make_assistant_message(),
            # Turn 2
            _make_message_start(input_tokens=80),
            _make_message_delta(output_tokens=40),
            _make_message_stop(),
            _make_assistant_message(),
            _make_result_message(num_turns=2),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in processor.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        gen_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_generation"
        ]
        assert len(gen_calls) == 2

    @pytest.mark.asyncio
    async def test_fallback_generation_from_result_when_no_stream_events(
        self, processor, mock_client
    ):
        """When StreamEvents are not available, fall back to ResultMessage data."""
        messages = [
            _make_assistant_message(),
            _make_result_message(input_tokens=200, output_tokens=100),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in processor.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        gen_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_generation"
        ]
        assert len(gen_calls) == 1
        props = gen_calls[0].kwargs.get("properties") or gen_calls[0][1].get(
            "properties"
        )
        assert props["$ai_input_tokens"] == 200
        assert props["$ai_output_tokens"] == 100


class TestToolSpanEmission:
    @pytest.mark.asyncio
    async def test_emits_span_for_tool_use(self, processor, mock_client):
        messages = [
            _make_message_start(),
            _make_message_stop(),
            _make_assistant_message(
                tool_uses=[
                    {
                        "id": "tu_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ]
            ),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in processor.query(
                prompt="Read a file", options=ClaudeAgentOptions()
            ):
                pass

        span_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_span"
        ]
        assert len(span_calls) == 1
        props = span_calls[0].kwargs.get("properties") or span_calls[0][1].get(
            "properties"
        )
        assert props["$ai_span_name"] == "Read"
        assert props["$ai_span_type"] == "tool"
        assert props["$ai_input_state"] == {"file_path": "/tmp/test.py"}


class TestTraceEmission:
    @pytest.mark.asyncio
    async def test_emits_trace_on_result(self, processor, mock_client):
        messages = [
            _make_message_start(),
            _make_message_stop(),
            _make_assistant_message(),
            _make_result_message(total_cost_usd=0.05, duration_ms=3000, is_error=False),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in processor.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        trace_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_trace"
        ]
        assert len(trace_calls) == 1
        props = trace_calls[0].kwargs.get("properties") or trace_calls[0][1].get(
            "properties"
        )
        assert props["$ai_trace_name"] == "claude_agent_sdk_query"
        assert props["$ai_total_cost_usd"] == 0.05
        assert props["$ai_latency"] == 3.0
        assert props["$ai_is_error"] is False

    @pytest.mark.asyncio
    async def test_trace_emits_error_status(self, processor, mock_client):
        messages = [
            _make_result_message(is_error=True, total_cost_usd=0.0),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in processor.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        trace_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_trace"
        ]
        assert len(trace_calls) == 1
        props = trace_calls[0].kwargs.get("properties") or trace_calls[0][1].get(
            "properties"
        )
        assert props["$ai_is_error"] is True


class TestPrivacyMode:
    @pytest.mark.asyncio
    async def test_privacy_mode_redacts_tool_input(self, mock_client):
        proc = PostHogClaudeAgentProcessor(
            client=mock_client,
            distinct_id="user",
            privacy_mode=True,
        )
        messages = [
            _make_message_start(),
            _make_message_stop(),
            _make_assistant_message(
                tool_uses=[
                    {
                        "id": "tu_1",
                        "name": "Read",
                        "input": {"file_path": "/secret/file"},
                    },
                ]
            ),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in proc.query(
                prompt="Read secret", options=ClaudeAgentOptions()
            ):
                pass

        span_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_span"
        ]
        assert len(span_calls) == 1
        props = span_calls[0].kwargs.get("properties") or span_calls[0][1].get(
            "properties"
        )
        assert "$ai_input_state" not in props


class TestPersonlessMode:
    @pytest.mark.asyncio
    async def test_no_distinct_id_sets_process_person_profile_false(self, mock_client):
        proc = PostHogClaudeAgentProcessor(
            client=mock_client,
            distinct_id=None,
        )
        messages = [
            _make_message_start(),
            _make_message_stop(),
            _make_assistant_message(),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in proc.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        for call in mock_client.capture.call_args_list:
            props = call.kwargs.get("properties") or call[1].get("properties")
            assert props.get("$process_person_profile") is False


class TestCustomProperties:
    @pytest.mark.asyncio
    async def test_instance_properties_merged(self, mock_client):
        proc = PostHogClaudeAgentProcessor(
            client=mock_client,
            distinct_id="user",
            properties={"app": "stamphog", "version": "1.0"},
        )
        messages = [
            _make_message_start(),
            _make_message_stop(),
            _make_assistant_message(),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in proc.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        for call in mock_client.capture.call_args_list:
            props = call.kwargs.get("properties") or call[1].get("properties")
            assert props.get("app") == "stamphog"
            assert props.get("version") == "1.0"

    @pytest.mark.asyncio
    async def test_per_call_properties_merged(self, processor, mock_client):
        messages = [
            _make_message_start(),
            _make_message_stop(),
            _make_assistant_message(),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in processor.query(
                prompt="Hi",
                options=ClaudeAgentOptions(),
                posthog_properties={"pr_number": 42},
            ):
                pass

        for call in mock_client.capture.call_args_list:
            props = call.kwargs.get("properties") or call[1].get("properties")
            assert props.get("pr_number") == 42


class TestCallableDistinctId:
    @pytest.mark.asyncio
    async def test_callable_distinct_id_resolved_on_trace(self, mock_client):
        def resolver(result):
            return f"user-{result.session_id}"

        proc = PostHogClaudeAgentProcessor(
            client=mock_client,
            distinct_id=resolver,
        )
        messages = [
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(messages),
        ):
            async for _ in proc.query(prompt="Hi", options=ClaudeAgentOptions()):
                pass

        trace_calls = [
            c
            for c in mock_client.capture.call_args_list
            if (c.kwargs.get("event") or c[1].get("event")) == "$ai_trace"
        ]
        assert len(trace_calls) == 1
        did = trace_calls[0].kwargs.get("distinct_id") or trace_calls[0][1].get(
            "distinct_id"
        )
        assert did == "user-sess_123"


class TestMessagePassthrough:
    @pytest.mark.asyncio
    async def test_all_messages_yielded_unchanged(self, processor, mock_client):
        original_messages = [
            _make_message_start(),
            _make_message_delta(),
            _make_message_stop(),
            _make_assistant_message(text="Hello world"),
            _make_result_message(),
        ]

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=lambda **kw: _fake_query(original_messages),
        ):
            collected = []
            async for msg in processor.query(prompt="Hi", options=ClaudeAgentOptions()):
                collected.append(msg)

        assert len(collected) == len(original_messages)
        # Verify types match
        for orig, got in zip(original_messages, collected):
            assert type(orig) is type(got)


class TestInstrumentFunction:
    def test_instrument_returns_processor(self, mock_client):
        proc = instrument(client=mock_client, distinct_id="test")
        assert isinstance(proc, PostHogClaudeAgentProcessor)
        assert proc._distinct_id == "test"
        assert proc._client == mock_client


class TestEnsurePartialMessages:
    @pytest.mark.asyncio
    async def test_enables_partial_messages_on_options(self, processor, mock_client):
        """Verify that the processor enables include_partial_messages."""
        captured_options = {}

        async def fake_query_capture(**kwargs):
            captured_options.update(kwargs)
            return
            yield  # make it an async generator

        with patch(
            "posthog.ai.claude_agent_sdk.processor.original_query",
            side_effect=fake_query_capture,
        ):
            async for _ in processor.query(
                prompt="Hi",
                options=ClaudeAgentOptions(include_partial_messages=False),
            ):
                pass

        assert captured_options.get("options").include_partial_messages is True
