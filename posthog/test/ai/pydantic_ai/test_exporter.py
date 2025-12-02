"""
Tests for PydanticAISpanExporter - handles Pydantic AI message format normalization.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

try:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

    from posthog.ai.pydantic_ai.exporter import PydanticAISpanExporter

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not OTEL_AVAILABLE, reason="OpenTelemetry SDK is not available"
)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.privacy_mode = False
    return client


def create_mock_span(
    name: str,
    attributes: dict = None,
    trace_id: int = 0x418BB9C71D1C0591CD2AD7F97B58B9EB,
    span_id: int = 0x1234567890ABCDEF,
    parent_span_id: int = None,
    start_time: int = 1000000000,
    end_time: int = 2000000000,
    status_code: StatusCode = StatusCode.OK,
):
    """Create a mock ReadableSpan for testing."""
    span = MagicMock(spec=ReadableSpan)
    span.name = name
    span.attributes = attributes or {}
    span.context = MagicMock()
    span.context.trace_id = trace_id
    span.context.span_id = span_id
    if parent_span_id:
        span.parent = MagicMock()
        span.parent.span_id = parent_span_id
    else:
        span.parent = None
    span.start_time = start_time
    span.end_time = end_time
    span.status = MagicMock()
    span.status.status_code = status_code
    span.status.description = None
    return span


class TestMessageNormalization:
    """Tests for normalizing Pydantic AI message format to OpenAI format."""

    def test_normalize_simple_text_message(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_format = [
            {"parts": [{"content": "Hello, how are you?", "type": "text"}], "role": "user"}
        ]
        result = exporter._normalize_messages(pydantic_format)

        assert result == [{"content": "Hello, how are you?", "role": "user"}]

    def test_normalize_multiple_text_parts(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_format = [
            {
                "parts": [
                    {"content": "First part.", "type": "text"},
                    {"content": "Second part.", "type": "text"},
                ],
                "role": "user",
            }
        ]
        result = exporter._normalize_messages(pydantic_format)

        assert result == [{"content": "First part.\nSecond part.", "role": "user"}]

    def test_normalize_message_with_tool_call(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_format = [
            {
                "parts": [
                    {
                        "type": "tool_call",
                        "id": "call_123",
                        "name": "get_weather",
                        "arguments": '{"latitude": 37.7749}',
                    }
                ],
                "role": "assistant",
            }
        ]
        result = exporter._normalize_messages(pydantic_format)

        assert result == [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"latitude": 37.7749}',
                        },
                    }
                ],
            }
        ]

    def test_normalize_message_with_text_and_tool_call(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_format = [
            {
                "parts": [
                    {"content": "Let me get the weather.", "type": "text"},
                    {
                        "type": "tool_call",
                        "id": "call_123",
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                ],
                "role": "assistant",
            }
        ]
        result = exporter._normalize_messages(pydantic_format)

        assert result == [
            {
                "role": "assistant",
                "content": "Let me get the weather.",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            }
        ]

    def test_normalize_preserves_finish_reason(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_format = [
            {
                "parts": [{"content": "Done!", "type": "text"}],
                "role": "assistant",
                "finish_reason": "stop",
            }
        ]
        result = exporter._normalize_messages(pydantic_format)

        assert result == [{"content": "Done!", "role": "assistant", "finish_reason": "stop"}]

    def test_normalize_already_openai_format(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        openai_format = [
            {"content": "Hello!", "role": "user"},
            {"content": "Hi there!", "role": "assistant"},
        ]
        result = exporter._normalize_messages(openai_format)

        assert result == openai_format

    def test_normalize_json_string_input(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        json_string = json.dumps(
            [{"parts": [{"content": "Hello", "type": "text"}], "role": "user"}]
        )
        result = exporter._normalize_messages(json_string)

        assert result == [{"content": "Hello", "role": "user"}]

    def test_normalize_invalid_json_returns_original(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        invalid_json = "not valid json"
        result = exporter._normalize_messages(invalid_json)

        assert result == "not valid json"

    def test_normalize_empty_parts(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_format = [{"parts": [], "role": "user"}]
        result = exporter._normalize_messages(pydantic_format)

        assert result == [{"role": "user", "content": ""}]


class TestSpanTransformation:
    """Tests for span attribute transformation."""

    def test_transform_span_normalizes_input_messages(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_messages = json.dumps(
            [{"parts": [{"content": "Hello", "type": "text"}], "role": "user"}]
        )

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.input.messages": pydantic_messages,
            },
        )

        transformed = exporter._transform_span(span)

        # Compare as parsed JSON to avoid key ordering issues
        result = json.loads(transformed.attributes["gen_ai.input.messages"])
        assert result == [{"content": "Hello", "role": "user"}]

    def test_transform_span_normalizes_output_messages(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_messages = json.dumps(
            [{"parts": [{"content": "Hi!", "type": "text"}], "role": "assistant"}]
        )

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.output.messages": pydantic_messages,
            },
        )

        transformed = exporter._transform_span(span)

        # Compare as parsed JSON to avoid key ordering issues
        result = json.loads(transformed.attributes["gen_ai.output.messages"])
        assert result == [{"content": "Hi!", "role": "assistant"}]

    def test_transform_span_preserves_other_attributes(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
                "gen_ai.input.messages": json.dumps(
                    [{"parts": [{"content": "Hi", "type": "text"}], "role": "user"}]
                ),
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.attributes["gen_ai.request.model"] == "gpt-4"
        assert transformed.attributes["gen_ai.usage.input_tokens"] == 100
        assert transformed.attributes["gen_ai.usage.output_tokens"] == 50

    def test_transform_span_no_modification_preserves_content(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        openai_messages = json.dumps([{"content": "Hi", "role": "user"}])

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.input.messages": openai_messages,
            },
        )

        transformed = exporter._transform_span(span)

        # Content should be equivalent even if span wrapper is different
        result = json.loads(transformed.attributes["gen_ai.input.messages"])
        assert result == [{"content": "Hi", "role": "user"}]


class TestEndToEndExport:
    """Tests for full export flow with message normalization."""

    def test_export_normalizes_and_captures(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_input = json.dumps(
            [{"parts": [{"content": "What's the weather?", "type": "text"}], "role": "user"}]
        )
        pydantic_output = json.dumps(
            [
                {
                    "parts": [{"content": "It's sunny!", "type": "text"}],
                    "role": "assistant",
                    "finish_reason": "stop",
                }
            ]
        )

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.system": "openai",
                "gen_ai.input.messages": pydantic_input,
                "gen_ai.output.messages": pydantic_output,
            },
        )

        result = exporter.export([span])

        assert result == SpanExportResult.SUCCESS
        mock_client.capture.assert_called_once()

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$ai_input"] == [{"content": "What's the weather?", "role": "user"}]
        assert props["$ai_output_choices"] == [
            {"content": "It's sunny!", "role": "assistant", "finish_reason": "stop"}
        ]

    def test_export_with_tool_calls_normalized(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        pydantic_output = json.dumps(
            [
                {
                    "parts": [
                        {
                            "type": "tool_call",
                            "id": "call_abc",
                            "name": "get_weather",
                            "arguments": '{"city": "SF"}',
                        }
                    ],
                    "role": "assistant",
                }
            ]
        )

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.output.messages": pydantic_output,
            },
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        expected_tool_call = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        }
        assert props["$ai_output_choices"] == [expected_tool_call]


class TestToolAttributeMapping:
    """Tests for mapping Pydantic AI tool attributes to GenAI standard names."""

    def test_maps_tool_arguments(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="running tool get_weather",
            attributes={
                "tool_arguments": '{"city": "SF"}',
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.attributes["gen_ai.tool.call.arguments"] == '{"city": "SF"}'
        assert transformed.attributes["tool_arguments"] == '{"city": "SF"}'

    def test_maps_tool_response(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="running tool get_weather",
            attributes={
                "tool_response": "Sunny, 72°F",
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.attributes["gen_ai.tool.call.result"] == "Sunny, 72°F"
        assert transformed.attributes["tool_response"] == "Sunny, 72°F"

    def test_maps_both_tool_attributes(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="running tool get_weather",
            attributes={
                "tool_arguments": '{"city": "SF"}',
                "tool_response": "Sunny, 72°F",
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.attributes["gen_ai.tool.call.arguments"] == '{"city": "SF"}'
        assert transformed.attributes["gen_ai.tool.call.result"] == "Sunny, 72°F"

    def test_does_not_overwrite_existing_genai_attributes(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="running tool get_weather",
            attributes={
                "tool_arguments": '{"city": "SF"}',
                "gen_ai.tool.call.arguments": '{"existing": "value"}',
            },
        )

        transformed = exporter._transform_span(span)

        # Should preserve existing GenAI attribute, not overwrite
        assert transformed.attributes["gen_ai.tool.call.arguments"] == '{"existing": "value"}'

    def test_tool_span_export_with_mapped_attributes(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="running tool get_weather",
            attributes={
                "gen_ai.tool.name": "get_weather",
                "tool_arguments": '{"city": "SF"}',
                "tool_response": "Sunny, 72°F",
            },
            parent_span_id=0xABCDEF1234567890,
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$ai_tool_arguments"] == {"city": "SF"}
        assert props["$ai_tool_result"] == "Sunny, 72°F"


class TestSpanWrapperProperties:
    """Tests for the _SpanWithModifiedAttributes wrapper."""

    def test_wrapper_preserves_span_name(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.input.messages": json.dumps(
                    [{"parts": [{"content": "Hi", "type": "text"}], "role": "user"}]
                )
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.name == "chat openai"

    def test_wrapper_preserves_context(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            trace_id=0x12345,
            span_id=0xABCDE,
            attributes={
                "gen_ai.input.messages": json.dumps(
                    [{"parts": [{"content": "Hi", "type": "text"}], "role": "user"}]
                )
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.context.trace_id == 0x12345
        assert transformed.context.span_id == 0xABCDE

    def test_wrapper_preserves_timing(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            start_time=1000,
            end_time=2000,
            attributes={
                "gen_ai.input.messages": json.dumps(
                    [{"parts": [{"content": "Hi", "type": "text"}], "role": "user"}]
                )
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.start_time == 1000
        assert transformed.end_time == 2000

    def test_wrapper_preserves_status(self, mock_client):
        exporter = PydanticAISpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            status_code=StatusCode.ERROR,
            attributes={
                "gen_ai.input.messages": json.dumps(
                    [{"parts": [{"content": "Hi", "type": "text"}], "role": "user"}]
                )
            },
        )

        transformed = exporter._transform_span(span)

        assert transformed.status.status_code == StatusCode.ERROR
