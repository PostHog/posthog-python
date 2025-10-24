import os
from unittest.mock import patch

import pytest

try:
    from anthropic.types import Message, Usage

    from posthog.ai.anthropic import Anthropic, AsyncAnthropic

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# Skip all tests if Anthropic is not available
pytestmark = pytest.mark.skipif(
    not ANTHROPIC_AVAILABLE, reason="Anthropic package is not available"
)


# =======================
# Reusable Mock Helpers
# =======================


class MockContent:
    """Reusable mock content class for Anthropic responses."""

    def __init__(self, text="Bar", content_type="text"):
        self.type = content_type
        self.text = text


class MockUsage:
    """Reusable mock usage class for Anthropic responses."""

    def __init__(
        self,
        input_tokens=18,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class MockResponse:
    """Reusable mock response class for Anthropic messages."""

    def __init__(
        self,
        content_text="Bar",
        model="claude-3-opus-20240229",
        input_tokens=18,
        output_tokens=1,
        cache_read=0,
        cache_creation=0,
    ):
        self.content = [MockContent(text=content_text)]
        self.model = model
        self.usage = MockUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )


def create_mock_response(**kwargs):
    """Factory function to create mock responses with custom parameters."""
    return MockResponse(**kwargs)


# Streaming mock helpers
class MockStreamEvent:
    """Reusable mock event class for streaming responses."""

    def __init__(self, event_type=None, **kwargs):
        self.type = event_type
        for key, value in kwargs.items():
            setattr(self, key, value)


class MockContentBlock:
    """Reusable mock content block for streaming."""

    def __init__(self, block_type, **kwargs):
        self.type = block_type
        for key, value in kwargs.items():
            setattr(self, key, value)


class MockDelta:
    """Reusable mock delta for streaming events."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


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
def mock_anthropic_stream_with_tools():
    """Mock stream events for tool calls."""

    class MockMessage:
        def __init__(self):
            self.usage = MockUsage(
                input_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=5,
            )

    def stream_generator():
        # Message start with usage
        event = MockStreamEvent("message_start")
        event.message = MockMessage()
        yield event

        # Text block start
        event = MockStreamEvent("content_block_start")
        event.content_block = MockContentBlock("text")
        event.index = 0
        yield event

        # Text delta
        event = MockStreamEvent("content_block_delta")
        event.delta = MockDelta(text="I'll check the weather for you.")
        event.index = 0
        yield event

        # Text block stop
        event = MockStreamEvent("content_block_stop")
        event.index = 0
        yield event

        # Tool use block start
        event = MockStreamEvent("content_block_start")
        event.content_block = MockContentBlock(
            "tool_use", id="toolu_stream123", name="get_weather"
        )
        event.index = 1
        yield event

        # Tool input delta 1
        event = MockStreamEvent("content_block_delta")
        event.delta = MockDelta(
            type="input_json_delta", partial_json='{"location": "San'
        )
        event.index = 1
        yield event

        # Tool input delta 2
        event = MockStreamEvent("content_block_delta")
        event.delta = MockDelta(
            type="input_json_delta", partial_json=' Francisco", "unit": "celsius"}'
        )
        event.index = 1
        yield event

        # Tool block stop
        event = MockStreamEvent("content_block_stop")
        event.index = 1
        yield event

        # Message delta with final usage
        event = MockStreamEvent("message_delta")
        event.usage = MockUsage(output_tokens=25)
        yield event

        # Message stop
        event = MockStreamEvent("message_stop")
        yield event

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


@pytest.fixture
def mock_anthropic_response_with_tool_calls():
    return Message(
        id="msg_456",
        type="message",
        role="assistant",
        content=[
            {"type": "text", "text": "I'll help you check the weather."},
            {"type": "text", "text": " Let me look that up."},
            {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": "get_weather",
                "input": {"location": "San Francisco"},
            },
        ],
        model="claude-3-5-sonnet-20241022",
        usage=Usage(
            input_tokens=25,
            output_tokens=15,
        ),
        stop_reason="tool_use",
        stop_sequence=None,
    )


@pytest.fixture
def mock_anthropic_response_tool_calls_only():
    return Message(
        id="msg_789",
        type="message",
        role="assistant",
        content=[
            {
                "type": "tool_use",
                "id": "toolu_def456",
                "name": "get_weather",
                "input": {"location": "New York", "unit": "fahrenheit"},
            }
        ],
        model="claude-3-5-sonnet-20241022",
        usage=Usage(
            input_tokens=30,
            output_tokens=12,
        ),
        stop_reason="tool_use",
        stop_sequence=None,
    )


def test_basic_completion(mock_client, mock_anthropic_response):
    with patch(
        "anthropic.resources.Messages.create", return_value=mock_anthropic_response
    ):
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
        assert props["$ai_lib_metadata"] == {
            "schema": "v1",
            "frameworks": [{"name": "anthropic"}],
        }
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-opus-20240229"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Test response"}],
            }
        ]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_groups(mock_client, mock_anthropic_response):
    with patch(
        "anthropic.resources.Messages.create", return_value=mock_anthropic_response
    ):
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
    with patch(
        "anthropic.resources.Messages.create", return_value=mock_anthropic_response
    ):
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
    with patch(
        "anthropic.resources.Messages.create", return_value=mock_anthropic_response
    ):
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


def test_basic_integration(mock_client):
    """Test basic non-streaming integration."""

    with patch(
        "anthropic.resources.Messages.create",
        return_value=create_mock_response(),
    ):
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
    assert props["$ai_output_choices"][0]["content"] == [
        {"type": "text", "text": "Bar"}
    ]
    assert props["$ai_input_tokens"] == 18
    assert props["$ai_output_tokens"] == 1
    assert props["$ai_http_status"] == 200
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)


async def test_basic_async_integration(mock_client):
    """Test async non-streaming integration."""

    # Make the mock async
    async def mock_async_create(**kwargs):
        return create_mock_response(input_tokens=16)

    with patch(
        "anthropic.resources.messages.AsyncMessages.create",
        side_effect=mock_async_create,
    ):
        client = AsyncAnthropic(posthog_client=mock_client)
        await client.messages.create(
            model="claude-3-opus-20240229",
            messages=[
                {"role": "user", "content": "You must always answer with 'Bar'."}
            ],
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
    assert props["$ai_input"] == [
        {"role": "user", "content": "You must always answer with 'Bar'."}
    ]
    assert props["$ai_output_choices"][0]["role"] == "assistant"
    assert props["$ai_input_tokens"] == 16
    assert props["$ai_output_tokens"] == 1
    assert props["$ai_http_status"] == 200
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)


async def test_async_streaming_system_prompt(mock_client):
    """Test async streaming with system prompt."""

    # Create a simple mock async stream using reusable helpers
    async def mock_async_stream():
        # Yield some events
        yield MockStreamEvent(type="message_start")
        yield MockStreamEvent(type="content_block_start")
        yield MockStreamEvent(type="content_block_delta", text="Bar")

        # Final message with usage
        final_msg = MockStreamEvent(type="message_delta")
        final_msg.usage = MockUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        yield final_msg

    # Mock create to return a coroutine that yields the async generator
    # This matches the actual behavior when stream=True with await
    async def async_create_wrapper(**kwargs):
        return mock_async_stream()

    with patch(
        "anthropic.resources.messages.AsyncMessages.create",
        side_effect=async_create_wrapper,
    ):
        client = AsyncAnthropic(posthog_client=mock_client)
        response = await client.messages.create(
            model="claude-3-opus-20240229",
            system="You must always answer with 'Bar'.",
            messages=[{"role": "user", "content": "Foo"}],
            stream=True,
            max_tokens=1,
        )

        # Consume the stream - async finally block completes before this returns
        [c async for c in response]

        # Capture happens in the async finally block before generator completes
        assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert props["$ai_input"] == [
        {"role": "system", "content": "You must always answer with 'Bar'."},
        {"role": "user", "content": "Foo"},
    ]


def test_error(mock_client, mock_anthropic_response):
    with patch(
        "anthropic.resources.Messages.create", side_effect=Exception("Test error")
    ):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        with pytest.raises(Exception):
            client.messages.create(
                model="claude-3-opus-20240229",
                messages=[{"role": "user", "content": "Hello"}],
            )

        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_error"] == "Test error"


def test_cached_tokens(mock_client, mock_anthropic_response_with_cached_tokens):
    with patch(
        "anthropic.resources.Messages.create",
        return_value=mock_anthropic_response_with_cached_tokens,
    ):
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
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Test response"}],
            }
        ]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_cache_read_input_tokens"] == 15
        assert props["$ai_cache_creation_input_tokens"] == 2
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_tool_definition(mock_client, mock_anthropic_response):
    with patch(
        "anthropic.resources.Messages.create",
        return_value=mock_anthropic_response,
    ):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)

        tools = [
            {
                "name": "get_weather",
                "description": "Get the current weather for a specific location",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city or location name to get weather for",
                        }
                    },
                    "required": ["location"],
                },
            }
        ]

        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=200,
            temperature=0.7,
            tools=tools,
            messages=[{"role": "user", "content": "hey"}],
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
        assert props["$ai_model"] == "claude-3-5-sonnet-20241022"
        assert props["$ai_input"] == [{"role": "user", "content": "hey"}]
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Test response"}],
            }
        ]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)
        # Verify that tools are captured in the $ai_tools property
        assert props["$ai_tools"] == tools


def test_tool_calls_in_output_choices(
    mock_client, mock_anthropic_response_with_tool_calls
):
    with patch(
        "anthropic.resources.Messages.create",
        return_value=mock_anthropic_response_with_tool_calls,
    ):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=200,
            messages=[
                {"role": "user", "content": "What's the weather in San Francisco?"}
            ],
            tools=[
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                }
            ],
            posthog_distinct_id="test-id",
        )

        assert response == mock_anthropic_response_with_tool_calls
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-5-sonnet-20241022"
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll help you check the weather."},
                    {"type": "text", "text": " Let me look that up."},
                    {
                        "type": "function",
                        "id": "toolu_abc123",
                        "function": {
                            "name": "get_weather",
                            "arguments": {"location": "San Francisco"},
                        },
                    },
                ],
            }
        ]

        # Check token usage
        assert props["$ai_input_tokens"] == 25
        assert props["$ai_output_tokens"] == 15
        assert props["$ai_http_status"] == 200


def test_tool_calls_only_no_content(
    mock_client, mock_anthropic_response_tool_calls_only
):
    with patch(
        "anthropic.resources.Messages.create",
        return_value=mock_anthropic_response_tool_calls_only,
    ):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=200,
            messages=[{"role": "user", "content": "Get weather for New York"}],
            tools=[
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                            "unit": {"type": "string"},
                        },
                        "required": ["location"],
                    },
                }
            ],
            posthog_distinct_id="test-id",
        )

        assert response == mock_anthropic_response_tool_calls_only
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-5-sonnet-20241022"
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "function",
                        "id": "toolu_def456",
                        "function": {
                            "name": "get_weather",
                            "arguments": {"location": "New York", "unit": "fahrenheit"},
                        },
                    }
                ],
            }
        ]

        # Check token usage
        assert props["$ai_input_tokens"] == 30
        assert props["$ai_output_tokens"] == 12
        assert props["$ai_http_status"] == 200


def test_async_tool_calls_in_output_choices(
    mock_client, mock_anthropic_response_with_tool_calls
):
    import asyncio

    async def mock_async_create(**kwargs):
        return mock_anthropic_response_with_tool_calls

    with patch(
        "anthropic.resources.AsyncMessages.create",
        side_effect=mock_async_create,
    ):
        async_client = AsyncAnthropic(api_key="test-key", posthog_client=mock_client)

        async def run_test():
            return await async_client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=200,
                messages=[
                    {"role": "user", "content": "What's the weather in San Francisco?"}
                ],
                tools=[
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "input_schema": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"],
                        },
                    }
                ],
                posthog_distinct_id="test-id",
            )

        response = asyncio.run(run_test())

        assert response == mock_anthropic_response_with_tool_calls
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-5-sonnet-20241022"
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll help you check the weather."},
                    {"type": "text", "text": " Let me look that up."},
                    {
                        "type": "function",
                        "id": "toolu_abc123",
                        "function": {
                            "name": "get_weather",
                            "arguments": {"location": "San Francisco"},
                        },
                    },
                ],
            }
        ]

        # Check token usage
        assert props["$ai_input_tokens"] == 25
        assert props["$ai_output_tokens"] == 15
        assert props["$ai_http_status"] == 200


def test_streaming_with_tool_calls(mock_client, mock_anthropic_stream_with_tools):
    """Test that tool calls are properly captured in streaming mode."""
    with patch(
        "anthropic.resources.Messages.create",
        return_value=mock_anthropic_stream_with_tools,
    ):
        client = Anthropic(api_key="test-key", posthog_client=mock_client)
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            system="You are a helpful weather assistant.",
            messages=[
                {"role": "user", "content": "What's the weather in San Francisco?"}
            ],
            tools=[
                {
                    "name": "get_weather",
                    "description": "Get weather information",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                            "unit": {"type": "string"},
                        },
                        "required": ["location"],
                    },
                }
            ],
            stream=True,
            posthog_distinct_id="test-id",
        )

        # Consume the stream - this triggers the finally block synchronously
        list(response)

        # Capture happens synchronously when generator is exhausted
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-5-sonnet-20241022"

        # Verify system prompt is included in input
        assert props["$ai_input"] == [
            {"role": "system", "content": "You are a helpful weather assistant."},
            {"role": "user", "content": "What's the weather in San Francisco?"},
        ]

        # Verify that tools are captured in the properties
        assert props["$ai_tools"] == [
            {
                "name": "get_weather",
                "description": "Get weather information",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "unit": {"type": "string"},
                    },
                    "required": ["location"],
                },
            }
        ]

        # Verify output contains both text and tool call
        output_choices = props["$ai_output_choices"]
        assert len(output_choices) == 1

        assistant_message = output_choices[0]
        assert assistant_message["role"] == "assistant"

        content = assistant_message["content"]
        assert isinstance(content, list)
        assert len(content) == 2

        # Verify text block
        text_block = content[0]
        assert text_block["type"] == "text"
        assert text_block["text"] == "I'll check the weather for you."

        # Verify tool call block
        tool_block = content[1]
        assert tool_block["type"] == "function"
        assert tool_block["id"] == "toolu_stream123"
        assert tool_block["function"]["name"] == "get_weather"
        assert tool_block["function"]["arguments"] == {
            "location": "San Francisco",
            "unit": "celsius",
        }

        # Check token usage
        assert props["$ai_input_tokens"] == 50
        assert props["$ai_output_tokens"] == 25
        assert props["$ai_cache_read_input_tokens"] == 5
        assert props["$ai_cache_creation_input_tokens"] == 0


def test_async_streaming_with_tool_calls(mock_client, mock_anthropic_stream_with_tools):
    """Test that tool calls are properly captured in async streaming mode."""
    import asyncio

    async def mock_async_generator():
        # Convert regular generator to async generator
        for event in mock_anthropic_stream_with_tools:
            yield event

    async def mock_async_create(**kwargs):
        # Return the async generator (to be awaited by the implementation)
        return mock_async_generator()

    with patch(
        "anthropic.resources.AsyncMessages.create",
        side_effect=mock_async_create,
    ):
        async_client = AsyncAnthropic(api_key="test-key", posthog_client=mock_client)

        async def run_test():
            response = await async_client.messages.create(
                model="claude-3-5-sonnet-20241022",
                system="You are a helpful weather assistant.",
                messages=[
                    {"role": "user", "content": "What's the weather in San Francisco?"}
                ],
                tools=[
                    {
                        "name": "get_weather",
                        "description": "Get weather information",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "location": {"type": "string"},
                                "unit": {"type": "string"},
                            },
                            "required": ["location"],
                        },
                    }
                ],
                stream=True,
                posthog_distinct_id="test-id",
            )

            # Consume the async stream
            [event async for event in response]

        # asyncio.run() waits for all async operations to complete
        asyncio.run(run_test())

        # Capture completes before asyncio.run() returns
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "anthropic"
        assert props["$ai_model"] == "claude-3-5-sonnet-20241022"

        # Verify system prompt is included in input
        assert props["$ai_input"] == [
            {"role": "system", "content": "You are a helpful weather assistant."},
            {"role": "user", "content": "What's the weather in San Francisco?"},
        ]

        # Verify that tools are captured in the properties
        assert props["$ai_tools"] == [
            {
                "name": "get_weather",
                "description": "Get weather information",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "unit": {"type": "string"},
                    },
                    "required": ["location"],
                },
            }
        ]

        # Verify output contains both text and tool call
        output_choices = props["$ai_output_choices"]
        assert len(output_choices) == 1

        assistant_message = output_choices[0]
        assert assistant_message["role"] == "assistant"

        content = assistant_message["content"]
        assert isinstance(content, list)
        assert len(content) == 2

        # Verify text block
        text_block = content[0]
        assert text_block["type"] == "text"
        assert text_block["text"] == "I'll check the weather for you."

        # Verify tool call block
        tool_block = content[1]
        assert tool_block["type"] == "function"
        assert tool_block["id"] == "toolu_stream123"
        assert tool_block["function"]["name"] == "get_weather"
        assert tool_block["function"]["arguments"] == {
            "location": "San Francisco",
            "unit": "celsius",
        }

        # Check token usage
        assert props["$ai_input_tokens"] == 50
        assert props["$ai_output_tokens"] == 25
        assert props["$ai_cache_read_input_tokens"] == 5
        assert props["$ai_cache_creation_input_tokens"] == 0
