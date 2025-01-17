import time
from unittest.mock import patch

import pytest
from anthropic.types import Message, Usage

from posthog.ai.anthropic import Anthropic


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


@pytest.fixture
def mock_anthropic_response():
    return Message(
        id="msg_123",
        type="message",
        role="assistant",
        content=[{"type": "text", "text": "Test response"}],
        model="claude-3-opus-20240229",
        usage=Usage(
            input_tokens=20,
            output_tokens=10,
        ),
        stop_reason="end_turn",
        stop_sequence=None,
    )


@pytest.fixture
def mock_anthropic_stream():
    class MockStreamEvent:
        def __init__(self, content, usage=None):
            self.content = content
            self.usage = usage

    def stream_generator():
        yield MockStreamEvent("A")
        yield MockStreamEvent("B")
        yield MockStreamEvent(
            "C",
            usage=Usage(
                input_tokens=20,
                output_tokens=10,
            ),
        )

    return stream_generator()


def test_basic_completion(mock_client, mock_anthropic_response):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_response):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_anthropic_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-opus-20240229"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Test response"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_streaming(mock_client, mock_anthropic_stream):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_stream):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        # Consume the stream
        chunks = list(response)
        assert len(chunks) == 3
        assert chunks[0].content == "A"
        assert chunks[1].content == "B"
        assert chunks[2].content == "C"

        # Wait a bit to ensure the capture is called
        time.sleep(0.1)
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-opus-20240229"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "ABC"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert isinstance(props["$ai_latency"], float)
        assert props["foo"] == "bar"


def test_streaming_with_stream_endpoint(mock_client, mock_anthropic_stream):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_stream):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.stream(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        # Consume the stream
        chunks = list(response)
        assert len(chunks) == 3
        assert chunks[0].content == "A"
        assert chunks[1].content == "B"
        assert chunks[2].content == "C"

        # Wait a bit to ensure the capture is called
        time.sleep(0.1)
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-opus-20240229"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "ABC"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert isinstance(props["$ai_latency"], float)
        assert props["foo"] == "bar"


def test_groups(mock_client, mock_anthropic_response):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_response):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_groups={"company": "test_company"},
        )

        assert response == mock_anthropic_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        assert call_args["groups"] == {"company": "test_company"}


def test_privacy_mode_local(mock_client, mock_anthropic_response):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_response):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_privacy_mode=True,
        )

        assert response == mock_anthropic_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_input"] is None
        assert props["$ai_output_choices"] is None


def test_privacy_mode_global(mock_client, mock_anthropic_response):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_response):
        mock_client.privacy_mode = True
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_privacy_mode=False,
        )

        assert response == mock_anthropic_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_input"] is None
        assert props["$ai_output_choices"] is None
