import time
from unittest.mock import patch

import pytest

try:
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import Choice as ChoiceChunk
    from openai.types.chat.chat_completion_chunk import (
        ChoiceDelta,
        ChoiceDeltaToolCall,
        ChoiceDeltaToolCallFunction,
    )
    from openai.types.chat.chat_completion_message_tool_call import (
        ChatCompletionMessageToolCall,
        Function,
    )
    from openai.types.completion_usage import CompletionUsage
    from openai.types.create_embedding_response import CreateEmbeddingResponse, Usage
    from openai.types.embedding import Embedding
    from openai.types.responses import (
        Response,
        ResponseOutputMessage,
        ResponseOutputText,
        ResponseUsage,
        ResponseFunctionToolCall,
        ParsedResponse,
    )
    from openai.types.responses.parsed_response import (
        ParsedResponseOutputMessage,
        ParsedResponseOutputText,
    )

    from posthog.ai.openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Skip all tests if OpenAI is not available
pytestmark = pytest.mark.skipif(
    not OPENAI_AVAILABLE, reason="OpenAI package is not available"
)


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
def mock_parsed_response():
    return ParsedResponse(
        id="test",
        model="gpt-4o-2024-08-06",
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
            ParsedResponseOutputMessage(
                id="msg_123",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ParsedResponseOutputText(
                        type="output_text",
                        text='{"name": "Science Fair", "date": "Friday", "participants": ["Alice", "Bob"]}',
                        annotations=[],
                        parsed={
                            "name": "Science Fair",
                            "date": "Friday",
                            "participants": ["Alice", "Bob"],
                        },
                    )
                ],
            )
        ],
        output_parsed={
            "name": "Science Fair",
            "date": "Friday",
            "participants": ["Alice", "Bob"],
        },
        parallel_tool_calls=True,
        previous_response_id=None,
        usage=ResponseUsage(
            input_tokens=15,
            output_tokens=20,
            input_tokens_details={"prompt_tokens": 15, "cached_tokens": 0},
            output_tokens_details={"reasoning_tokens": 5},
            total_tokens=35,
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
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    content="I'll check the weather for you.",
                    role="assistant",
                ),
            ),
            Choice(
                finish_reason="stop",
                index=1,
                message=ChatCompletionMessage(
                    content=" Let me look that up.",
                    role="assistant",
                ),
            ),
            Choice(
                finish_reason="tool_calls",
                index=2,
                message=ChatCompletionMessage(
                    content=None,
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
            ),
        ],
        usage=CompletionUsage(
            completion_tokens=15,
            prompt_tokens=20,
            total_tokens=35,
        ),
    )


@pytest.fixture
def mock_openai_response_tool_calls_only():
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
                    content=None,
                    role="assistant",
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id="call_def456",
                            type="function",
                            function=Function(
                                name="get_weather",
                                arguments='{"location": "New York"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=CompletionUsage(
            completion_tokens=10,
            prompt_tokens=25,
            total_tokens=35,
        ),
    )


@pytest.fixture
def mock_responses_api_with_tool_calls():
    return Response(
        id="resp_123",
        object="response",
        created_at=int(time.time()),
        model="gpt-4o-mini",
        status="completed",
        error=None,
        incomplete_details=None,
        instructions=None,
        max_output_tokens=None,
        tools=[],
        tool_choice="auto",
        parallel_tool_calls=True,
        output=[
            ResponseOutputMessage(
                id="msg_456",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text",
                        text="I'll help you with the weather.",
                        annotations=[],
                    ),
                    ResponseOutputText(
                        type="output_text",
                        text=" Let me check that for you.",
                        annotations=[],
                    ),
                ],
            ),
            ResponseFunctionToolCall(
                id="fc_789",
                type="function_call",
                name="get_weather",
                call_id="call_xyz789",
                arguments='{"location": "Chicago"}',
                status="completed",
            ),
        ],
        usage=ResponseUsage(
            input_tokens=30,
            output_tokens=20,
            input_tokens_details={"prompt_tokens": 30, "cached_tokens": 0},
            output_tokens_details={"reasoning_tokens": 0},
            total_tokens=50,
        ),
        previous_response_id=None,
        user=None,
        metadata={},
    )


def test_basic_completion(mock_client, mock_openai_response):
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response,
    ):
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


def test_embeddings(mock_client, mock_embedding_response):
    with patch(
        "openai.resources.embeddings.Embeddings.create",
        return_value=mock_embedding_response,
    ):
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
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response,
    ):
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
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response,
    ):
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
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response,
    ):
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
    with patch(
        "openai.resources.chat.completions.Completions.create",
        side_effect=Exception("Test error"),
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        with pytest.raises(Exception):
            client.chat.completions.create(
                model="gpt-4", messages=[{"role": "user", "content": "Hello"}]
            )

        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]
        assert props["$ai_is_error"] is True
        assert props["$ai_error"] == "Test error"


def test_cached_tokens(mock_client, mock_openai_response_with_cached_tokens):
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response_with_cached_tokens,
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
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Test response"}],
            }
        ]
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_cache_read_input_tokens"] == 15
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_tool_calls(mock_client, mock_openai_response_with_tool_calls):
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response_with_tool_calls,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "user", "content": "What's the weather in San Francisco?"}
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {},
                    },
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
        assert props["$ai_input"] == [
            {"role": "user", "content": "What's the weather in San Francisco?"}
        ]
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll check the weather for you."},
                    {"type": "text", "text": " Let me look that up."},
                    {
                        "type": "function",
                        "id": "call_abc123",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "San Francisco", "unit": "celsius"}',
                        },
                    },
                ],
            }
        ]

        # Check that defined tools are properly captured in $ai_tools
        assert "$ai_tools" in props
        defined_tools = props["$ai_tools"]
        assert len(defined_tools) == 1

        # Verify the defined tool details
        defined_tool = defined_tools[0]
        assert defined_tool["type"] == "function"
        assert defined_tool["function"]["name"] == "get_weather"
        assert defined_tool["function"]["description"] == "Get weather"
        assert defined_tool["function"]["parameters"] == {}

        # Check token usage
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 15
        assert props["$ai_http_status"] == 200


def test_tool_calls_only_no_content(mock_client, mock_openai_response_tool_calls_only):
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response_tool_calls_only,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Get weather for New York"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {},
                    },
                }
            ],
            posthog_distinct_id="test-id",
        )

        assert response == mock_openai_response_tool_calls_only
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4"
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "function",
                        "id": "call_def456",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "New York"}',
                        },
                    }
                ],
            }
        ]

        # Check token usage
        assert props["$ai_input_tokens"] == 25
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_http_status"] == 200


def test_responses_api_tool_calls(mock_client, mock_responses_api_with_tool_calls):
    with patch(
        "openai.resources.responses.Responses.create",
        return_value=mock_responses_api_with_tool_calls,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "What's the weather in Chicago?"}],
            tools=[
                {
                    "name": "get_weather",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {},
                    },
                }
            ],
            posthog_distinct_id="test-id",
        )

        assert response == mock_responses_api_with_tool_calls
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4o-mini"
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll help you with the weather."},
                    {"type": "text", "text": " Let me check that for you."},
                    {
                        "type": "function",
                        "id": "call_xyz789",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"location": "Chicago"}',
                        },
                    },
                ],
            }
        ]

        # Check token usage
        assert props["$ai_input_tokens"] == 30
        assert props["$ai_output_tokens"] == 20
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
            messages=[
                {"role": "user", "content": "What's the weather in San Francisco?"}
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {},
                    },
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

        # Check that defined tools are properly captured in $ai_tools
        assert "$ai_tools" in props
        defined_tools = props["$ai_tools"]
        assert len(defined_tools) == 1

        # Verify the defined tool details
        defined_tool = defined_tools[0]
        assert defined_tool["type"] == "function"
        assert defined_tool["function"]["name"] == "get_weather"
        assert defined_tool["function"]["description"] == "Get weather"
        assert defined_tool["function"]["parameters"] == {}

        # Check that both text content and tool calls were accumulated
        output_content = props["$ai_output_choices"][0]["content"]

        # Find text content and tool call in the output
        text_content = None
        tool_call_content = None
        for item in output_content:
            if item["type"] == "text":
                text_content = item
            elif item["type"] == "function":
                tool_call_content = item

        # Verify text content
        assert text_content is not None
        assert text_content["text"] == "The weather in San Francisco is 15°C."

        # Verify tool call was captured
        assert tool_call_content is not None
        assert tool_call_content["id"] == "call_abc123"
        assert tool_call_content["function"]["name"] == "get_weather"
        assert (
            tool_call_content["function"]["arguments"]
            == '{"location": "San Francisco", "unit": "celsius"}'
        )

        # Check token usage
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 15


# test responses api
def test_responses_api(mock_client, mock_openai_response_with_responses_api):
    with patch(
        "openai.resources.responses.Responses.create",
        return_value=mock_openai_response_with_responses_api,
    ):
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
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Test response"}],
            }
        ]
        assert props["$ai_input_tokens"] == 10
        assert props["$ai_output_tokens"] == 10
        assert props["$ai_reasoning_tokens"] == 15
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_responses_parse(mock_client, mock_parsed_response):
    with patch(
        "openai.resources.responses.Responses.parse",
        return_value=mock_parsed_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.responses.parse(
            model="gpt-4o-2024-08-06",
            input=[
                {"role": "system", "content": "Extract the event information."},
                {
                    "role": "user",
                    "content": "Alice and Bob are going to a science fair on Friday.",
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "event",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "date": {"type": "string"},
                                "participants": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["name", "date", "participants"],
                        },
                    },
                }
            },
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_parsed_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4o-2024-08-06"
        assert props["$ai_input"] == [
            {"role": "system", "content": "Extract the event information."},
            {
                "role": "user",
                "content": "Alice and Bob are going to a science fair on Friday.",
            },
        ]
        assert props["$ai_output_choices"] == [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": '{"name": "Science Fair", "date": "Friday", "participants": ["Alice", "Bob"]}',
                    }
                ],
            }
        ]
        assert props["$ai_input_tokens"] == 15
        assert props["$ai_output_tokens"] == 20
        assert props["$ai_reasoning_tokens"] == 5
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_responses_api_streaming_with_tokens(mock_client):
    """Test that Responses API streaming properly captures token usage from response.usage."""
    from openai.types.responses import ResponseUsage
    from unittest.mock import MagicMock

    # Create mock response chunks with usage data in the correct location
    chunks = []

    # First chunk - just content, no usage
    chunk1 = MagicMock()
    chunk1.type = "response.text.delta"
    chunk1.text = "Test "
    chunks.append(chunk1)

    # Second chunk - more content
    chunk2 = MagicMock()
    chunk2.type = "response.text.delta"
    chunk2.text = "response"
    chunks.append(chunk2)

    # Final chunk - completed event with usage in response.usage
    chunk3 = MagicMock()
    chunk3.type = "response.completed"
    chunk3.response = MagicMock()
    chunk3.response.usage = ResponseUsage(
        input_tokens=25,
        output_tokens=30,
        total_tokens=55,
        input_tokens_details={"prompt_tokens": 25, "cached_tokens": 0},
        output_tokens_details={"reasoning_tokens": 0},
    )
    chunk3.response.output = ["Test response"]
    chunks.append(chunk3)

    captured_kwargs = {}

    def mock_streaming_response(**kwargs):
        # Capture the kwargs to verify stream_options was NOT added
        captured_kwargs.update(kwargs)
        return iter(chunks)

    with patch(
        "openai.resources.responses.Responses.create",
        side_effect=mock_streaming_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Consume the streaming response
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "Test message"}],
            stream=True,
            posthog_distinct_id="test-id",
            posthog_properties={"test": "streaming"},
        )

        # Consume all chunks
        list(response)

        # Verify stream_options was NOT added (Responses API doesn't support it)
        assert "stream_options" not in captured_kwargs

        # Verify capture was called
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Verify tokens are captured correctly from response.usage (not 0)
        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_generation"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "gpt-4o-mini"
        assert props["$ai_input_tokens"] == 25  # Should not be 0
        assert props["$ai_output_tokens"] == 30  # Should not be 0
        assert props["test"] == "streaming"
        assert isinstance(props["$ai_latency"], float)


def test_tool_definition(mock_client, mock_openai_response):
    """Test that tools defined in the create function are captured in $ai_tools property"""
    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_openai_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Define tools to be passed to the create function
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a specific location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {
                                "type": "string",
                                "description": "The city or location name to get weather for",
                            }
                        },
                        "required": ["location"],
                    },
                },
            }
        ]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hey"}],
            tools=tools,
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
        assert props["$ai_model"] == "gpt-4o-mini"
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
