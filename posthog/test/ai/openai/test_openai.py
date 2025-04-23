import json
import time
from unittest.mock import patch

import pytest

try:
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import Choice as ChoiceChunk
    from openai.types.chat.chat_completion_chunk import ChoiceDelta, ChoiceDeltaToolCall, ChoiceDeltaToolCallFunction
    from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function
    from openai.types.completion_usage import CompletionUsage
    from openai.types.create_embedding_response import CreateEmbeddingResponse, Usage
    from openai.types.embedding import Embedding
    from openai.types.responses import Response, ResponseOutputMessage, ResponseOutputText, ResponseUsage

    from posthog.ai.openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Skip all tests if OpenAI is not available
pytestmark = pytest.mark.skipif(not OPENAI_AVAILABLE, reason="OpenAI package is not available")


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


@pytest.fixture
def mock_openai_response():
    return ChatCompletion(
        id="test",
        model="gpt-4",
        object="chat.completion",
        created=int(time.time()),
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    content="Test response",
                    role="assistant",
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=20,
            total_tokens=30,
        ),
    )


@pytest.fixture
def mock_openai_response_with_responses_api():
    return Response(
        id="test",
        model="gpt-4o-mini",
        object="response",
        created_at=1741476542,
        status="completed",
        error=None,
        incomplete_details=None,
        instructions=None,
        max_output_tokens=None,
        tools=[],
        tool_choice="auto",
        output=[
            ResponseOutputMessage(
                id="msg_123",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text",
                        text="Test response",
                        annotations=[],
                    )
                ],
            )
        ],
        parallel_tool_calls=True,
        previous_response_id=None,
        usage=ResponseUsage(
            input_tokens=10,
            output_tokens=10,
            input_tokens_details={"prompt_tokens": 10, "cached_tokens": 0},
            output_tokens_details={"reasoning_tokens": 15},
            total_tokens=20,
        ),
        user=None,
        metadata={},
    )


@pytest.fixture
def mock_embedding_response():
    return CreateEmbeddingResponse(
        data=[
            Embedding(
                embedding=[0.1, 0.2, 0.3],
                index=0,
                object="embedding",
            )
        ],
        model="text-embedding-3-small",
        object="list",
        usage=Usage(
            prompt_tokens=10,
            total_tokens=10,
        ),
    )


@pytest.fixture
def mock_openai_response_with_cached_tokens():
    return ChatCompletion(
        id="test",
        model="gpt-4",
        object="chat.completion",
        created=int(time.time()),
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    content="Test response",
                    role="assistant",
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=20,
            total_tokens=30,
            prompt_tokens_details={"cached_tokens": 15},
        ),
    )


@pytest.fixture
def mock_openai_response_with_tool_calls():
    return ChatCompletion(
        id="test",
        model="gpt-4",
        object="chat.completion",
        created=int(time.time()),
        choices=[
            Choice(
                finish_reason="tool_calls",
                index=0,
                message=ChatCompletionMessage(
                    content="I'll check the weather for you.",
                    role="assistant",
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id="call_abc123",
                            type="function",
                            function=Function(
                                name="get_weather",
                                arguments='{"location": "San Francisco", "unit": "celsius"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=15,
            prompt_tokens=20,
            total_tokens=35,
        ),
    )


def test_basic_completion(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Test response"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_embeddings(mock_client, mock_embedding_response):
    with patch("openai.resources.embeddings.Embeddings.create", return_value=mock_embedding_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input="Hello world",
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_embedding_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_embedding"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "text-embedding-3-small"
        assert props["$ai_input"] == "Hello world"
        assert props["$ai_input_tokens"] == 10
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_groups(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_groups={"company": "test_company"},
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]

        assert call_args["groups"] == {"company": "test_company"}


def test_privacy_mode_local(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_privacy_mode=True,
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_input"] is None
        assert props["$ai_output_choices"] is None


def test_privacy_mode_global(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", return_value=mock_openai_response):
        mock_client.privacy_mode = True
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_privacy_mode=False,
        )

        assert response == mock_openai_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_input"] is None
        assert props["$ai_output_choices"] is None


def test_error(mock_client, mock_openai_response):
    with patch("openai.resources.chat.completions.Completions.create", side_effect=Exception("Test error")):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        with pytest.raises(Exception):
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "Hello"}])

        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_error"] == "Test error"


def test_cached_tokens(mock_client, mock_openai_response_with_cached_tokens):
    with patch(
        "openai.resources.chat.completions.Completions.create", return_value=mock_openai_response_with_cached_tokens
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_openai_response_with_cached_tokens
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Test response"}]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_cache_read_input_tokens"] == 15
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_tool_calls(mock_client, mock_openai_response_with_tool_calls):
    with patch(
        "openai.resources.chat.completions.Completions.create", return_value=mock_openai_response_with_tool_calls
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "What's the weather in San Francisco?"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "get_weather", "description": "Get weather", "parameters": {}},
                }
            ],
            posthog_distinct_id="test-id",
        )

        assert response == mock_openai_response_with_tool_calls
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_input"] == [{"role": "user", "content": "What's the weather in San Francisco?"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "I'll check the weather for you."}]

        # Check that tool calls are properly captured
        assert "$ai_tools" in props
        tool_calls = props["$ai_tools"]
        assert len(tool_calls) == 1

        # Verify the tool call details
        tool_call = tool_calls[0]
        assert tool_call.id == "call_abc123"
        assert tool_call.type == "function"
        assert tool_call.function.name == "get_weather"

        # Verify the arguments
        arguments = tool_call.function.arguments
        parsed_args = json.loads(arguments)
        assert parsed_args == {"location": "San Francisco", "unit": "celsius"}

        # Check token usage
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 15
        assert props["$ai_http_status"] == 200


def test_streaming_with_tool_calls(mock_client):
    # Create mock tool call chunks that will be returned in sequence
    tool_call_chunks = [
        ChatCompletionChunk(
            id="chunk1",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567890,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_abc123",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    name="get_weather",
                                    arguments='{"location": "',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk2",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567891,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_abc123",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    arguments='San Francisco"',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk3",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567892,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        tool_calls=[
                            ChoiceDeltaToolCall(
                                index=0,
                                id="call_abc123",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    arguments=', "unit": "celsius"}',
                                ),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk4",
            model="gpt-4",
            object="chat.completion.chunk",
            created=1234567893,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(
                        content="The weather in San Francisco is 15°C.",
                    ),
                    finish_reason=None,
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=20,
                completion_tokens=15,
                total_tokens=35,
            ),
        ),
    ]

    # Mock the create method to return our chunks
    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        # Set up the mock to return our chunks when iterated
        mock_create.return_value = tool_call_chunks

        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Call the streaming method
        response_generator = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "What's the weather in San Francisco?"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "get_weather", "description": "Get weather", "parameters": {}},
                }
            ],
            stream=True,
            posthog_distinct_id="test-id",
        )

        # Consume the generator to trigger the event capture
        chunks = list(response_generator)

        # Verify the chunks were returned correctly
        assert len(chunks) == 4
        assert chunks == tool_call_chunks

        # Verify the capture was called with the right arguments
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"

        # Check that the tool calls were properly accumulated
        assert "$ai_tools" in props
        tool_calls = props["$ai_tools"]
        assert len(tool_calls) == 1

        # Verify the complete tool call was properly assembled
        tool_call = tool_calls[0]
        assert tool_call.id == "call_abc123"
        assert tool_call.type == "function"
        assert tool_call.function.name == "get_weather"

        # Verify the arguments were concatenated correctly
        arguments = tool_call.function.arguments
        parsed_args = json.loads(arguments)
        assert parsed_args == {"location": "San Francisco", "unit": "celsius"}

        # Check that the content was also accumulated
        assert props["$ai_output_choices"][0]["content"] == "The weather in San Francisco is 15°C."

        # Check token usage
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 15


# test responses api
def test_responses_api(mock_client, mock_openai_response_with_responses_api):
    with patch("openai.resources.responses.Responses.create", return_value=mock_openai_response_with_responses_api):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.responses.create(
            model="gpt-4o-mini",
            input="Hello",
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )
        assert response == mock_openai_response_with_responses_api
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4o-mini"
        assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
        assert props["$ai_output_choices"] == [{"role": "assistant", "content": "Test response"}]
        assert props["$ai_input_tokens"] == 10
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_reasoning_tokens"] == 15
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)
