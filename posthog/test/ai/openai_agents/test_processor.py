import logging
from unittest.mock import MagicMock, patch

import pytest

try:
    from agents.tracing.span_data import (
        AgentSpanData,
        CustomSpanData,
        FunctionSpanData,
        GenerationSpanData,
        GuardrailSpanData,
        HandoffSpanData,
        ResponseSpanData,
        SpeechSpanData,
        TranscriptionSpanData,
    )

    from posthog.ai.openai_agents import PostHogTracingProcessor, instrument

    OPENAI_AGENTS_AVAILABLE = True
except ImportError:
    OPENAI_AGENTS_AVAILABLE = False


# Skip all tests if OpenAI Agents SDK is not available
pytestmark = pytest.mark.skipif(
    not OPENAI_AGENTS_AVAILABLE, reason="OpenAI Agents SDK is not available"
)


@pytest.fixture(scope="function")
def mock_client():
    client = MagicMock()
    client.privacy_mode = False
    logging.getLogger("posthog").setLevel(logging.DEBUG)
    return client


@pytest.fixture(scope="function")
def processor(mock_client):
    return PostHogTracingProcessor(
        client=mock_client,
        distinct_id="test-user",
        privacy_mode=False,
    )


@pytest.fixture
def mock_trace():
    trace = MagicMock()
    trace.trace_id = "trace_123456789"
    trace.name = "Test Workflow"
    trace.group_id = "group_123"
    trace.metadata = {"key": "value"}
    return trace


@pytest.fixture
def mock_span():
    span = MagicMock()
    span.trace_id = "trace_123456789"
    span.span_id = "span_987654321"
    span.parent_id = None
    span.started_at = "2024-01-01T00:00:00Z"
    span.ended_at = "2024-01-01T00:00:01Z"
    span.error = None
    return span


class TestPostHogTracingProcessor:
    """Tests for the PostHogTracingProcessor class."""

    def test_initialization(self, mock_client):
        """Test processor initializes correctly."""
        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id="user@example.com",
            privacy_mode=True,
            groups={"company": "acme"},
            properties={"env": "test"},
        )

        assert processor._client == mock_client
        assert processor._distinct_id == "user@example.com"
        assert processor._privacy_mode is True
        assert processor._groups == {"company": "acme"}
        assert processor._properties == {"env": "test"}

    def test_initialization_with_callable_distinct_id(self, mock_client, mock_trace):
        """Test processor with callable distinct_id resolver."""

        def resolver(trace):
            return trace.metadata.get("user_id", "default")

        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id=resolver,
        )

        mock_trace.metadata = {"user_id": "resolved-user"}
        distinct_id = processor._get_distinct_id(mock_trace)
        assert distinct_id == "resolved-user"

    def test_on_trace_start_stores_metadata(self, processor, mock_client, mock_trace):
        """Test that on_trace_start stores metadata but does not capture an event."""
        processor.on_trace_start(mock_trace)

        mock_client.capture.assert_not_called()
        assert mock_trace.trace_id in processor._trace_metadata

    def test_on_trace_end_captures_ai_trace(self, processor, mock_client, mock_trace):
        """Test that on_trace_end captures $ai_trace event."""
        processor.on_trace_start(mock_trace)
        processor.on_trace_end(mock_trace)

        mock_client.capture.assert_called_once()
        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_trace"
        assert call_kwargs["distinct_id"] == "test-user"
        assert call_kwargs["properties"]["$ai_trace_id"] == "trace_123456789"
        assert call_kwargs["properties"]["$ai_trace_name"] == "Test Workflow"
        assert call_kwargs["properties"]["$ai_provider"] == "openai"
        assert call_kwargs["properties"]["$ai_framework"] == "openai-agents"
        assert "$ai_latency" in call_kwargs["properties"]

    def test_personless_mode_when_no_distinct_id(self, mock_client, mock_trace):
        """Test that trace events use personless mode when no distinct_id is provided."""
        processor = PostHogTracingProcessor(
            client=mock_client,
        )

        processor.on_trace_start(mock_trace)
        processor.on_trace_end(mock_trace)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$process_person_profile"] is False
        # Should fallback to trace_id as the distinct_id
        assert call_kwargs["distinct_id"] == mock_trace.trace_id

    def test_personless_mode_for_spans_when_no_distinct_id(
        self, mock_client, mock_trace, mock_span
    ):
        """Test that span events use personless mode when no distinct_id is provided."""
        processor = PostHogTracingProcessor(
            client=mock_client,
        )

        processor.on_trace_start(mock_trace)
        mock_client.capture.reset_mock()

        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$process_person_profile"] is False
        assert call_kwargs["distinct_id"] == mock_span.trace_id

    def test_personless_mode_when_callable_returns_none(
        self, mock_client, mock_trace, mock_span
    ):
        """Test personless mode when callable distinct_id returns None."""

        def resolver(trace):
            return None  # Simulate no user ID available

        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id=resolver,
        )

        processor.on_trace_start(mock_trace)
        mock_client.capture.reset_mock()

        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$process_person_profile"] is False
        assert call_kwargs["distinct_id"] == mock_span.trace_id

    def test_person_profile_when_distinct_id_provided(self, mock_client, mock_trace):
        """Test that events create person profiles when distinct_id is provided."""
        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id="real-user",
        )

        processor.on_trace_start(mock_trace)
        processor.on_trace_end(mock_trace)

        call_kwargs = mock_client.capture.call_args[1]
        assert "$process_person_profile" not in call_kwargs["properties"]

    def test_on_trace_end_clears_metadata(self, processor, mock_client, mock_trace):
        """Test that on_trace_end clears stored trace metadata."""
        processor.on_trace_start(mock_trace)
        assert mock_trace.trace_id in processor._trace_metadata

        processor.on_trace_end(mock_trace)
        assert mock_trace.trace_id not in processor._trace_metadata
        # Also verify it captured the event
        mock_client.capture.assert_called_once()

    def test_on_span_start_tracks_time(self, processor, mock_span):
        """Test that on_span_start records start time."""
        processor.on_span_start(mock_span)
        assert mock_span.span_id in processor._span_start_times

    def test_generation_span_mapping(self, processor, mock_client, mock_span):
        """Test GenerationSpanData maps to $ai_generation event."""
        span_data = GenerationSpanData(
            input=[{"role": "user", "content": "Hello"}],
            output=[{"role": "assistant", "content": "Hi there!"}],
            model="gpt-4o",
            model_config={"temperature": 0.7, "max_tokens": 100},
            usage={"input_tokens": 10, "output_tokens": 20},
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        mock_client.capture.assert_called_once()
        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_generation"
        assert call_kwargs["properties"]["$ai_trace_id"] == "trace_123456789"
        assert call_kwargs["properties"]["$ai_span_id"] == "span_987654321"
        assert call_kwargs["properties"]["$ai_provider"] == "openai"
        assert call_kwargs["properties"]["$ai_framework"] == "openai-agents"
        assert call_kwargs["properties"]["$ai_model"] == "gpt-4o"
        assert call_kwargs["properties"]["$ai_input_tokens"] == 10
        assert call_kwargs["properties"]["$ai_output_tokens"] == 20
        assert call_kwargs["properties"]["$ai_input"] == [
            {"role": "user", "content": "Hello"}
        ]
        assert call_kwargs["properties"]["$ai_output_choices"] == [
            {"role": "assistant", "content": "Hi there!"}
        ]

    def test_generation_span_with_reasoning_tokens(
        self, processor, mock_client, mock_span
    ):
        """Test GenerationSpanData includes reasoning tokens when present."""
        span_data = GenerationSpanData(
            model="o1-preview",
            usage={
                "input_tokens": 100,
                "output_tokens": 500,
                "reasoning_tokens": 400,
            },
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_reasoning_tokens"] == 400

    def test_function_span_mapping(self, processor, mock_client, mock_span):
        """Test FunctionSpanData maps to $ai_span event with type=tool."""
        span_data = FunctionSpanData(
            name="get_weather",
            input='{"city": "San Francisco"}',
            output="Sunny, 72F",
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_name"] == "get_weather"
        assert call_kwargs["properties"]["$ai_span_type"] == "tool"
        assert (
            call_kwargs["properties"]["$ai_input_state"] == '{"city": "San Francisco"}'
        )
        assert call_kwargs["properties"]["$ai_output_state"] == "Sunny, 72F"

    def test_agent_span_mapping(self, processor, mock_client, mock_span):
        """Test AgentSpanData maps to $ai_span event with type=agent."""
        span_data = AgentSpanData(
            name="CustomerServiceAgent",
            handoffs=["TechnicalAgent", "BillingAgent"],
            tools=["search", "get_order"],
            output_type="str",
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_name"] == "CustomerServiceAgent"
        assert call_kwargs["properties"]["$ai_span_type"] == "agent"
        assert call_kwargs["properties"]["$ai_agent_handoffs"] == [
            "TechnicalAgent",
            "BillingAgent",
        ]
        assert call_kwargs["properties"]["$ai_agent_tools"] == ["search", "get_order"]

    def test_handoff_span_mapping(self, processor, mock_client, mock_span):
        """Test HandoffSpanData maps to $ai_span event with type=handoff."""
        span_data = HandoffSpanData(
            from_agent="TriageAgent",
            to_agent="TechnicalAgent",
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_type"] == "handoff"
        assert call_kwargs["properties"]["$ai_handoff_from_agent"] == "TriageAgent"
        assert call_kwargs["properties"]["$ai_handoff_to_agent"] == "TechnicalAgent"
        assert (
            call_kwargs["properties"]["$ai_span_name"]
            == "TriageAgent -> TechnicalAgent"
        )

    def test_guardrail_span_mapping(self, processor, mock_client, mock_span):
        """Test GuardrailSpanData maps to $ai_span event with type=guardrail."""
        span_data = GuardrailSpanData(
            name="ContentFilter",
            triggered=True,
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_name"] == "ContentFilter"
        assert call_kwargs["properties"]["$ai_span_type"] == "guardrail"
        assert call_kwargs["properties"]["$ai_guardrail_triggered"] is True

    def test_custom_span_mapping(self, processor, mock_client, mock_span):
        """Test CustomSpanData maps to $ai_span event with type=custom."""
        span_data = CustomSpanData(
            name="database_query",
            data={"query": "SELECT * FROM users", "rows": 100},
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_name"] == "database_query"
        assert call_kwargs["properties"]["$ai_span_type"] == "custom"
        assert call_kwargs["properties"]["$ai_custom_data"] == {
            "query": "SELECT * FROM users",
            "rows": 100,
        }

    def test_privacy_mode_redacts_content(self, mock_client, mock_span):
        """Test that privacy_mode redacts input/output content."""
        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id="test-user",
            privacy_mode=True,
        )

        span_data = GenerationSpanData(
            input=[{"role": "user", "content": "Secret message"}],
            output=[{"role": "assistant", "content": "Secret response"}],
            model="gpt-4o",
            usage={"input_tokens": 10, "output_tokens": 20},
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        # Content should be redacted
        assert call_kwargs["properties"]["$ai_input"] is None
        assert call_kwargs["properties"]["$ai_output_choices"] is None
        # Token counts should still be present
        assert call_kwargs["properties"]["$ai_input_tokens"] == 10
        assert call_kwargs["properties"]["$ai_output_tokens"] == 20

    def test_error_handling_in_span(self, processor, mock_client, mock_span):
        """Test that span errors are captured correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {"message": "Rate limit exceeded", "data": {"code": 429}}

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["properties"]["$ai_is_error"] is True
        assert call_kwargs["properties"]["$ai_error"] == "Rate limit exceeded"

    def test_generation_span_includes_total_tokens(
        self, processor, mock_client, mock_span
    ):
        """Test that $ai_total_tokens is calculated and included."""
        span_data = GenerationSpanData(
            model="gpt-4o",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_total_tokens"] == 150

    def test_error_type_categorization_model_behavior(
        self, processor, mock_client, mock_span
    ):
        """Test that ModelBehaviorError is categorized correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {
            "message": "ModelBehaviorError: Invalid JSON output",
            "type": "ModelBehaviorError",
        }

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_error_type"] == "model_behavior_error"

    def test_error_type_categorization_user_error(
        self, processor, mock_client, mock_span
    ):
        """Test that UserError is categorized correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {"message": "UserError: Tool failed", "type": "UserError"}

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_error_type"] == "user_error"

    def test_error_type_categorization_input_guardrail(
        self, processor, mock_client, mock_span
    ):
        """Test that InputGuardrailTripwireTriggered is categorized correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {
            "message": "InputGuardrailTripwireTriggered: Content blocked"
        }

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert (
            call_kwargs["properties"]["$ai_error_type"] == "input_guardrail_triggered"
        )

    def test_error_type_categorization_output_guardrail(
        self, processor, mock_client, mock_span
    ):
        """Test that OutputGuardrailTripwireTriggered is categorized correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {
            "message": "OutputGuardrailTripwireTriggered: Response blocked"
        }

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert (
            call_kwargs["properties"]["$ai_error_type"] == "output_guardrail_triggered"
        )

    def test_error_type_categorization_max_turns(
        self, processor, mock_client, mock_span
    ):
        """Test that MaxTurnsExceeded is categorized correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {"message": "MaxTurnsExceeded: Agent exceeded maximum turns"}

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_error_type"] == "max_turns_exceeded"

    def test_error_type_categorization_unknown(self, processor, mock_client, mock_span):
        """Test that unknown errors are categorized as unknown."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {"message": "Some random error occurred"}

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_error_type"] == "unknown"

    def test_response_span_with_output_and_total_tokens(
        self, processor, mock_client, mock_span
    ):
        """Test ResponseSpanData includes output choices and total tokens."""
        # Create a mock response object
        mock_response = MagicMock()
        mock_response.id = "resp_123"
        mock_response.model = "gpt-4o"
        mock_response.output = [{"type": "message", "content": "Hello!"}]
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 25
        mock_response.usage.output_tokens = 10

        span_data = ResponseSpanData(
            response=mock_response,
            input="Hello, world!",
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_generation"
        assert call_kwargs["properties"]["$ai_total_tokens"] == 35
        assert call_kwargs["properties"]["$ai_output_choices"] == [
            {"type": "message", "content": "Hello!"}
        ]
        assert call_kwargs["properties"]["$ai_response_id"] == "resp_123"

    def test_speech_span_with_pass_through_properties(
        self, processor, mock_client, mock_span
    ):
        """Test SpeechSpanData includes pass-through properties."""
        span_data = SpeechSpanData(
            input="Hello, how can I help you?",
            output="base64_audio_data",
            output_format="pcm",
            model="tts-1",
            model_config={"voice": "alloy", "speed": 1.0},
            first_content_at="2024-01-01T00:00:00.500Z",
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_type"] == "speech"
        assert call_kwargs["properties"]["$ai_model"] == "tts-1"
        # Pass-through properties (no $ai_ prefix)
        assert (
            call_kwargs["properties"]["first_content_at"] == "2024-01-01T00:00:00.500Z"
        )
        assert call_kwargs["properties"]["audio_output_format"] == "pcm"
        assert call_kwargs["properties"]["model_config"] == {
            "voice": "alloy",
            "speed": 1.0,
        }
        # Text input should be captured
        assert call_kwargs["properties"]["$ai_input"] == "Hello, how can I help you?"

    def test_transcription_span_with_pass_through_properties(
        self, processor, mock_client, mock_span
    ):
        """Test TranscriptionSpanData includes pass-through properties."""
        span_data = TranscriptionSpanData(
            input="base64_audio_data",
            input_format="pcm",
            output="This is the transcribed text.",
            model="whisper-1",
            model_config={"language": "en"},
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]

        assert call_kwargs["event"] == "$ai_span"
        assert call_kwargs["properties"]["$ai_span_type"] == "transcription"
        assert call_kwargs["properties"]["$ai_model"] == "whisper-1"
        # Pass-through properties (no $ai_ prefix)
        assert call_kwargs["properties"]["audio_input_format"] == "pcm"
        assert call_kwargs["properties"]["model_config"] == {"language": "en"}
        # Transcription output should be captured
        assert (
            call_kwargs["properties"]["$ai_output_state"]
            == "This is the transcribed text."
        )

    def test_latency_calculation(self, processor, mock_client, mock_span):
        """Test that latency is calculated correctly."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data

        with patch("time.time") as mock_time:
            mock_time.return_value = 1000.0
            processor.on_span_start(mock_span)

            mock_time.return_value = 1001.5  # 1.5 seconds later
            processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_latency"] == pytest.approx(1.5, rel=0.01)

    def test_groups_included_in_events(self, mock_client, mock_trace, mock_span):
        """Test that groups are included in captured events."""
        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id="test-user",
            groups={"company": "acme", "team": "engineering"},
        )

        processor.on_trace_start(mock_trace)
        processor.on_trace_end(mock_trace)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["groups"] == {"company": "acme", "team": "engineering"}

    def test_additional_properties_included(self, mock_client, mock_trace):
        """Test that additional properties are included in events."""
        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id="test-user",
            properties={"environment": "production", "version": "1.0"},
        )

        processor.on_trace_start(mock_trace)
        processor.on_trace_end(mock_trace)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["environment"] == "production"
        assert call_kwargs["properties"]["version"] == "1.0"

    def test_shutdown_clears_state(self, processor):
        """Test that shutdown clears internal state."""
        processor._span_start_times["span_1"] = 1000.0
        processor._trace_metadata["trace_1"] = {"name": "test"}

        processor.shutdown()

        assert len(processor._span_start_times) == 0
        assert len(processor._trace_metadata) == 0

    def test_force_flush_calls_client_flush(self, processor, mock_client):
        """Test that force_flush calls client.flush()."""
        processor.force_flush()
        mock_client.flush.assert_called_once()

    def test_generation_span_with_no_usage(self, processor, mock_client, mock_span):
        """Test GenerationSpanData with no usage data defaults to zero tokens."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_input_tokens"] == 0
        assert call_kwargs["properties"]["$ai_output_tokens"] == 0
        assert call_kwargs["properties"]["$ai_total_tokens"] == 0

    def test_generation_span_with_partial_usage(
        self, processor, mock_client, mock_span
    ):
        """Test GenerationSpanData with only input_tokens present."""
        span_data = GenerationSpanData(
            model="gpt-4o",
            usage={"input_tokens": 42},
        )
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_input_tokens"] == 42
        assert call_kwargs["properties"]["$ai_output_tokens"] == 0
        assert call_kwargs["properties"]["$ai_total_tokens"] == 42

    def test_error_type_categorization_by_type_field_only(
        self, processor, mock_client, mock_span
    ):
        """Test error categorization works when only the type field matches."""
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data
        mock_span.error = {
            "message": "Something went wrong",
            "type": "ModelBehaviorError",
        }

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["properties"]["$ai_error_type"] == "model_behavior_error"

    def test_distinct_id_resolved_from_trace_for_spans(
        self, mock_client, mock_trace, mock_span
    ):
        """Test that spans use the distinct_id resolved at trace start."""

        def resolver(trace):
            return f"user-{trace.name}"

        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id=resolver,
        )

        # Start trace - this resolves and stores distinct_id
        processor.on_trace_start(mock_trace)
        mock_client.capture.reset_mock()

        # End a span - should use the stored distinct_id from trace
        span_data = GenerationSpanData(model="gpt-4o")
        mock_span.span_data = span_data

        processor.on_span_start(mock_span)
        processor.on_span_end(mock_span)

        call_kwargs = mock_client.capture.call_args[1]
        assert call_kwargs["distinct_id"] == "user-Test Workflow"

    def test_eviction_of_stale_entries(self, mock_client):
        """Test that stale entries are evicted when max is exceeded."""
        processor = PostHogTracingProcessor(
            client=mock_client,
            distinct_id="test-user",
        )
        processor._max_tracked_entries = 10

        # Fill beyond max
        for i in range(15):
            processor._span_start_times[f"span_{i}"] = float(i)
            processor._trace_metadata[f"trace_{i}"] = {"name": f"trace_{i}"}

        processor._evict_stale_entries()

        # Should have evicted half
        assert len(processor._span_start_times) <= 10
        assert len(processor._trace_metadata) <= 10


class TestInstrumentHelper:
    """Tests for the instrument() convenience function."""

    def test_instrument_registers_processor(self, mock_client):
        """Test that instrument() registers a processor."""
        with patch("agents.tracing.add_trace_processor") as mock_add:
            processor = instrument(
                client=mock_client,
                distinct_id="test-user",
            )

            mock_add.assert_called_once_with(processor)
            assert isinstance(processor, PostHogTracingProcessor)

    def test_instrument_with_privacy_mode(self, mock_client):
        """Test instrument() respects privacy_mode."""
        with patch("agents.tracing.add_trace_processor"):
            processor = instrument(
                client=mock_client,
                privacy_mode=True,
            )

            assert processor._privacy_mode is True

    def test_instrument_with_groups_and_properties(self, mock_client):
        """Test instrument() accepts groups and properties."""
        with patch("agents.tracing.add_trace_processor"):
            processor = instrument(
                client=mock_client,
                groups={"company": "acme"},
                properties={"env": "test"},
            )

            assert processor._groups == {"company": "acme"}
            assert processor._properties == {"env": "test"}
