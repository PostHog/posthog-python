"""
Tests for PostHogSpanExporter - the generic OpenTelemetry span exporter for PostHog.
"""

import json
from unittest.mock import MagicMock

import pytest

try:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.trace import StatusCode

    from posthog.ai.otel import PostHogSpanExporter

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
    status_description: str = None,
):
    """Create a mock ReadableSpan for testing."""
    span = MagicMock(spec=ReadableSpan)
    span.name = name
    span.attributes = attributes or {}

    # Set up span context
    span.context = MagicMock()
    span.context.trace_id = trace_id
    span.context.span_id = span_id

    # Set up parent context
    if parent_span_id:
        span.parent = MagicMock()
        span.parent.span_id = parent_span_id
    else:
        span.parent = None

    span.start_time = start_time
    span.end_time = end_time

    # Set up status
    span.status = MagicMock()
    span.status.status_code = status_code
    span.status.description = status_description

    return span


class TestTraceIdFormatting:
    """Tests for trace ID formatting to UUID format."""

    def test_format_trace_id_as_uuid(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        trace_id = 0x418BB9C71D1C0591CD2AD7F97B58B9EB
        result = exporter._format_trace_id_as_uuid(trace_id)
        assert result == "418bb9c7-1d1c-0591-cd2a-d7f97b58b9eb"

    def test_format_trace_id_preserves_leading_zeros(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        trace_id = 0x00000000000000000000000000000001
        result = exporter._format_trace_id_as_uuid(trace_id)
        assert result == "00000000-0000-0000-0000-000000000001"


class TestSpanClassification:
    """Tests for span type classification logic."""

    def test_is_generation_span_chat_prefix(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        assert exporter._is_generation_span("chat openai", {}) is True
        assert exporter._is_generation_span("chat gpt-4", {}) is True

    def test_is_generation_span_with_operation_name(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        attrs = {"gen_ai.operation.name": "chat"}
        assert exporter._is_generation_span("some_span", attrs) is True

    def test_is_generation_span_with_request_model(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        attrs = {"gen_ai.request.model": "gpt-4"}
        assert exporter._is_generation_span("some_span", attrs) is True

    def test_is_generation_span_negative(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        assert exporter._is_generation_span("tool call", {}) is False

    def test_is_agent_span(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        assert exporter._is_agent_span("agent run", {}) is True
        assert exporter._is_agent_span("invoke_agent", {}) is True
        assert exporter._is_agent_span(
            "some_span", {"gen_ai.agent.name": "test"}
        )  # truthy
        assert not exporter._is_agent_span("some_span", {})

    def test_is_tool_span(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        assert exporter._is_tool_span("execute_tool get_weather", {}) is True
        assert exporter._is_tool_span("running tools", {}) is True
        assert (
            exporter._is_tool_span("some_span", {"gen_ai.tool.name": "get_weather"})
            is True
        )
        assert exporter._is_tool_span("model call", {}) is False


class TestGenerationEventCreation:
    """Tests for $ai_generation event creation from model request spans."""

    def test_basic_generation_event(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.system": "openai",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 50,
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "content": "Hello"}]
                ),
                "gen_ai.output.messages": json.dumps(
                    [{"role": "assistant", "content": "Hi!"}]
                ),
            },
        )

        exporter.export([span])

        mock_client.capture.assert_called_once()
        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_generation"
        assert call_kwargs["distinct_id"] == "user_123"

        props = call_kwargs["properties"]
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_input_tokens"] == 100
        assert props["$ai_output_tokens"] == 50
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Hi!"}]
        assert props["$ai_is_error"] is False
        assert props["$ai_http_status"] == 200
        assert "$ai_trace_id" in props
        assert "-" in props["$ai_trace_id"]  # UUID format

    def test_generation_event_with_error(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            attributes={"gen_ai.request.model": "gpt-4"},
            status_code=StatusCode.ERROR,
            status_description="Rate limit exceeded",
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_http_status"] == 500
        assert props["$ai_error"] == "Rate limit exceeded"

    def test_generation_event_privacy_mode(self, mock_client):
        exporter = PostHogSpanExporter(
            mock_client, distinct_id="user_123", privacy_mode=True
        )

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "content": "Secret data"}]
                ),
                "gen_ai.output.messages": json.dumps(
                    [{"role": "assistant", "content": "Response"}]
                ),
            },
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert "$ai_input" not in props
        assert "$ai_output_choices" not in props

    def test_generation_event_with_model_parameters(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "gen_ai.request.temperature": 0.7,
                "gen_ai.request.max_tokens": 1000,
                "gen_ai.request.top_p": 0.9,
            },
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$ai_model_parameters"] == {
            "temperature": 0.7,
            "max_tokens": 1000,
            "top_p": 0.9,
        }


class TestAgentSpanHandling:
    """Tests for agent span handling ($ai_trace events)."""

    def test_agent_span_creates_trace_event(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="agent run", attributes={"gen_ai.agent.name": "TestAgent"}
        )

        exporter.export([span])

        mock_client.capture.assert_called_once()
        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["event"] == "$ai_trace"
        assert call_kwargs["properties"]["$ai_span_name"] == "TestAgent"

    def test_invoke_agent_span_creates_trace_event(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(name="invoke_agent", attributes={})

        exporter.export([span])

        mock_client.capture.assert_called_once()
        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["event"] == "$ai_trace"


class TestToolSpanEventCreation:
    """Tests for $ai_span event creation from tool execution spans."""

    def test_basic_tool_span_event(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="execute_tool get_weather",
            attributes={
                "gen_ai.tool.name": "get_weather",
                "gen_ai.tool.call.arguments": json.dumps(
                    {"latitude": 37.7749, "longitude": -122.4194}
                ),
                "gen_ai.tool.call.result": "Sunny, 72°F",
            },
            parent_span_id=0xABCDEF1234567890,
        )

        exporter.export([span])

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["event"] == "$ai_span"

        props = call_kwargs["properties"]
        assert props["$ai_span_name"] == "get_weather"
        assert "$ai_trace_id" in props
        assert "$ai_span_id" in props
        assert "$ai_parent_id" in props
        assert props["$ai_tool_arguments"] == {
            "latitude": 37.7749,
            "longitude": -122.4194,
        }
        assert props["$ai_tool_result"] == "Sunny, 72°F"

    def test_tool_span_privacy_mode(self, mock_client):
        exporter = PostHogSpanExporter(
            mock_client, distinct_id="user_123", privacy_mode=True
        )

        span = create_mock_span(
            name="execute_tool get_weather",
            attributes={
                "gen_ai.tool.name": "get_weather",
                "gen_ai.tool.call.arguments": json.dumps({"secret": "value"}),
                "gen_ai.tool.call.result": "Secret result",
            },
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert "$ai_tool_arguments" not in props
        assert "$ai_tool_result" not in props


class TestDistinctIdHandling:
    """Tests for distinct_id resolution."""

    def test_distinct_id_from_constructor(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="configured_user")

        span = create_mock_span(
            name="chat openai", attributes={"gen_ai.request.model": "gpt-4"}
        )

        exporter.export([span])

        assert mock_client.capture.call_args[1]["distinct_id"] == "configured_user"

    def test_distinct_id_from_span_attribute(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="default_user")

        span = create_mock_span(
            name="chat openai",
            attributes={
                "gen_ai.request.model": "gpt-4",
                "posthog.distinct_id": "span_user",
            },
        )

        exporter.export([span])

        assert mock_client.capture.call_args[1]["distinct_id"] == "span_user"

    def test_distinct_id_fallback_to_trace_id(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)

        span = create_mock_span(
            name="chat openai",
            attributes={"gen_ai.request.model": "gpt-4"},
            trace_id=0xABCDEF1234567890ABCDEF1234567890,
        )

        exporter.export([span])

        assert (
            mock_client.capture.call_args[1]["distinct_id"]
            == "abcdef1234567890abcdef1234567890"
        )

    def test_process_person_profile_false_when_no_distinct_id(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)

        span = create_mock_span(
            name="chat openai", attributes={"gen_ai.request.model": "gpt-4"}
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$process_person_profile"] is False


class TestAdditionalProperties:
    """Tests for additional properties and groups."""

    def test_additional_properties_included(self, mock_client):
        exporter = PostHogSpanExporter(
            mock_client,
            distinct_id="user_123",
            properties={"$ai_session_id": "session_abc", "custom_prop": "value"},
        )

        span = create_mock_span(
            name="chat openai", attributes={"gen_ai.request.model": "gpt-4"}
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$ai_session_id"] == "session_abc"
        assert props["custom_prop"] == "value"

    def test_groups_included(self, mock_client):
        exporter = PostHogSpanExporter(
            mock_client,
            distinct_id="user_123",
            groups={"company": "posthog", "team": "product"},
        )

        span = create_mock_span(
            name="chat openai", attributes={"gen_ai.request.model": "gpt-4"}
        )

        exporter.export([span])

        assert mock_client.capture.call_args[1]["groups"] == {
            "company": "posthog",
            "team": "product",
        }


class TestExportResult:
    """Tests for export method return values."""

    def test_export_returns_success(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai", attributes={"gen_ai.request.model": "gpt-4"}
        )

        result = exporter.export([span])

        assert result == SpanExportResult.SUCCESS

    def test_export_handles_exceptions_gracefully(self, mock_client):
        mock_client.capture.side_effect = Exception("Network error")
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai", attributes={"gen_ai.request.model": "gpt-4"}
        )

        result = exporter.export([span])

        assert result == SpanExportResult.SUCCESS


class TestLatencyCalculation:
    """Tests for latency calculation from span times."""

    def test_latency_calculated_correctly(self, mock_client):
        exporter = PostHogSpanExporter(mock_client, distinct_id="user_123")

        span = create_mock_span(
            name="chat openai",
            attributes={"gen_ai.request.model": "gpt-4"},
            start_time=1_000_000_000,  # 1 second in nanoseconds
            end_time=2_500_000_000,  # 2.5 seconds in nanoseconds
        )

        exporter.export([span])

        props = mock_client.capture.call_args[1]["properties"]
        assert props["$ai_latency"] == 1.5


class TestJsonParsing:
    """Tests for JSON attribute parsing."""

    def test_parse_json_string(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        result = exporter._parse_json_attr('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_json_already_parsed(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        result = exporter._parse_json_attr({"key": "value"})
        assert result == {"key": "value"}

    def test_parse_json_invalid_returns_original(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        result = exporter._parse_json_attr("not json")
        assert result == "not json"

    def test_parse_json_none(self, mock_client):
        exporter = PostHogSpanExporter(mock_client)
        result = exporter._parse_json_attr(None)
        assert result is None
