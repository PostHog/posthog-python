import os
import time
from unittest.mock import patch

import pytest

try:
    from anthropic.types import Message, Usage

    from posthog.ai.anthropic import Anthropic, AsyncAnthropic

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Skip all tests if Anthropic is not available
pytestmark = pytest.mark.skipif(not ANTHROPIC_AVAILABLE, reason="Anthropic package is not available")


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


@pytest.fixture
def mock_anthropic_response_with_cached_tokens():
    # Create a mock Usage object with cached_tokens in input_tokens_details
    usage = Usage(
        input_tokens=20,
        output_tokens=10,
        cache_read_input_tokens=15,
        cache_creation_input_tokens=2,
    )

    return Message(
        id="msg_123",
        type="message",
        role="assistant",
        content=[{"type": "text", "text": "Test response"}],
        model="claude-3-opus-20240229",
        usage=usage,
        stop_reason="end_turn",
        stop_sequence=None,
    )


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


@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY is not set")
def test_basic_integration(mock_client):
    client = Anthropic(posthog_client=mock_client)
    client.messages.create(
        model="claude-3-opus-20240229",
        messages=[{"role": "user", "content": "Foo"}],
        max_tokens=1,
        temperature=0,
        posthog_distinct_id="test-id",
        posthog_properties={"foo": "bar"},
        system="You must always answer with 'Bar'.",
    )

    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "anthropic"
    assert props["$ai_model"] == "claude-3-opus-20240229"
    assert props["$ai_input"] == [
        {"role": "system", "content": "You must always answer with 'Bar'."},
        {"role": "user", "content": "Foo"},
    ]
    assert props["$ai_output_choices"][0]["role"] == "assistant"
    assert props["$ai_output_choices"][0]["content"] == "Bar"
    assert props["$ai_input_tokens"] == 18
    assert props["$ai_output_tokens"] == 1
    assert props["$ai_http_status"] == 200
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY is not set")
async def test_basic_async_integration(mock_client):
    client = AsyncAnthropic(posthog_client=mock_client)
    await client.messages.create(
        model="claude-3-opus-20240229",
        messages=[{"role": "user", "content": "You must always answer with 'Bar'."}],
        max_tokens=1,
        temperature=0,
        posthog_distinct_id="test-id",
        posthog_properties={"foo": "bar"},
    )

    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "anthropic"
    assert props["$ai_model"] == "claude-3-opus-20240229"
    assert props["$ai_input"] == [{"role": "user", "content": "You must always answer with 'Bar'."}]
    assert props["$ai_output_choices"][0]["role"] == "assistant"
    assert props["$ai_input_tokens"] == 16
    assert props["$ai_output_tokens"] == 1
    assert props["$ai_http_status"] == 200
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)


def test_streaming_system_prompt(mock_client, mock_anthropic_stream):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_stream):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            system="Foo",
            messages=[{"role": "user", "content": "Bar"}],
            stream=True,
        )

        # Consume the stream
        list(response)

        # Wait a bit to ensure the capture is called
        time.sleep(0.1)
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert props["$ai_input"] == [{"role": "system", "content": "Foo"}, {"role": "user", "content": "Bar"}]


@pytest.mark.skipif(not ANTHROPIC_API_KEY, reason="ANTHROPIC_API_KEY is not set")
async def test_async_streaming_system_prompt(mock_client, mock_anthropic_stream):
    client = AsyncAnthropic(posthog_client=mock_client)
    response = await client.messages.create(
        model="claude-3-opus-20240229",
        system="You must always answer with 'Bar'.",
        messages=[{"role": "user", "content": "Foo"}],
        stream=True,
        max_tokens=1,
    )

    # Consume the stream
    [c async for c in response]

    # Wait a bit to ensure the capture is called
    time.sleep(0.1)
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert props["$ai_input"] == [
        {"role": "system", "content": "You must always answer with 'Bar'."},
        {"role": "user", "content": "Foo"},
    ]


def test_error(mock_client, mock_anthropic_response):
    with patch("anthropic.resources.Messages.create", side_effect=Exception("Test error")):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        with pytest.raises(Exception):
            client.messages.create(model="claude-3-opus-20240229", messages=[{"role": "user", "content": "Hello"}])

        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_error"] == "Test error"


def test_cached_tokens(mock_client, mock_anthropic_response_with_cached_tokens):
    with patch("anthropic.resources.Messages.create", return_value=mock_anthropic_response_with_cached_tokens):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-opus-20240229",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_anthropic_response_with_cached_tokens
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
        assert props["$ai_cache_read_input_tokens"] == 15
        assert props["$ai_cache_creation_input_tokens"] == 2
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)
