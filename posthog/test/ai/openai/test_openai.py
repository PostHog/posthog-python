import time
from unittest.mock import AsyncMock, patch

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
    from openai.types.audio import Transcription
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
    from posthog.ai.openai.openai_async import AsyncOpenAI

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
def streaming_tool_call_chunks():
    return [
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


def test_streaming_with_tool_calls(mock_client, streaming_tool_call_chunks):
    # Mock the create method to return our chunks
    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        # Set up the mock to return our chunks when iterated
        mock_create.return_value = streaming_tool_call_chunks

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
        assert chunks == streaming_tool_call_chunks

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


@pytest.mark.asyncio
async def test_async_chat_streaming_with_tool_calls(
    mock_client, streaming_tool_call_chunks
):
    captured_kwargs = {}

    async def mock_create(self, **kwargs):
        captured_kwargs["kwargs"] = kwargs

        async def chunk_iterable():
            for chunk in streaming_tool_call_chunks:
                yield chunk

        return chunk_iterable()

    with patch(
        "openai.resources.chat.completions.AsyncCompletions.create", new=mock_create
    ):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response_stream = await client.chat.completions.create(
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

        chunks = []
        async for chunk in response_stream:
            chunks.append(chunk)

    kwargs = captured_kwargs["kwargs"]
    assert kwargs["stream_options"]["include_usage"] is True

    assert len(chunks) == len(streaming_tool_call_chunks)
    assert chunks == streaming_tool_call_chunks

    assert mock_client.capture.call_count == 1
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "openai"
    assert props["$ai_model"] == "gpt-4"
    assert props["$ai_output_tokens"] == 15
    assert props["$ai_input_tokens"] == 20
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.asyncio
async def test_async_responses_streaming_with_tokens(mock_client):
    from openai.types.responses import ResponseUsage
    from unittest.mock import MagicMock

    chunks = []

    chunk1 = MagicMock()
    chunk1.type = "response.text.delta"
    chunk1.text = "Test "
    chunks.append(chunk1)

    chunk2 = MagicMock()
    chunk2.type = "response.text.delta"
    chunk2.text = "response"
    chunks.append(chunk2)

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

    async def mock_create(self, **kwargs):
        captured_kwargs["kwargs"] = kwargs

        async def chunk_iterable():
            for chunk in chunks:
                yield chunk

        return chunk_iterable()

    with patch("openai.resources.responses.AsyncResponses.create", new=mock_create):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response_stream = await client.responses.create(
            model="gpt-4o-mini",
            input=[{"role": "user", "content": "Test message"}],
            stream=True,
            posthog_distinct_id="test-id",
            posthog_properties={"test": "streaming"},
        )

        async for _ in response_stream:
            pass

    kwargs = captured_kwargs["kwargs"]
    assert "stream_options" not in kwargs

    assert mock_client.capture.call_count == 1
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "openai"
    assert props["$ai_model"] == "gpt-4o-mini"
    assert props["$ai_input_tokens"] == 25
    assert props["$ai_output_tokens"] == 30
    assert props["test"] == "streaming"
    assert isinstance(props["$ai_latency"], float)


@pytest.mark.asyncio
async def test_async_embeddings_create(mock_client, mock_embedding_response):
    mock_create = AsyncMock(return_value=mock_embedding_response)

    with patch("openai.resources.embeddings.AsyncEmbeddings.create", new=mock_create):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response = await client.embeddings.create(
            model="text-embedding-3-small",
            input="Hello world",
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

    assert response == mock_embedding_response
    assert mock_create.await_count == 1
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_embedding"
    assert props["$ai_provider"] == "openai"
    assert props["$ai_model"] == "text-embedding-3-small"
    assert props["foo"] == "bar"
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


def test_web_search_perplexity_style(mock_client):
    """Test web search detection via annotations (Perplexity-style)."""

    class MockAnnotation:
        def __init__(self):
            self.type = "url_citation"

    class MockMessage:
        def __init__(self):
            self.role = "assistant"
            self.content = "Based on recent search results..."
            self.annotations = [MockAnnotation(), MockAnnotation()]

    class MockChoice:
        def __init__(self):
            self.message = MockMessage()

    class MockUsage:
        def __init__(self):
            self.prompt_tokens = 50
            self.completion_tokens = 30

    class MockResponseWithAnnotations:
        def __init__(self):
            self.choices = [MockChoice()]
            self.usage = MockUsage()
            self.model = "gpt-4-turbo"

    mock_response = MockResponseWithAnnotations()

    with patch("openai.resources.chat.Completions.create", return_value=mock_response):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": "What's happening in tech?"}],
            posthog_distinct_id="test-id",
        )

        assert response == mock_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Verify web search count is detected (binary detection)
        assert props["$ai_web_search_count"] == 1


def test_web_search_responses_api(mock_client):
    """Test exact web search count from Responses API."""

    class MockWebSearchItem:
        def __init__(self):
            self.type = "web_search_call"

    class MockMessageItem:
        def __init__(self):
            self.type = "message"
            self.role = "assistant"
            self.content = "Here are the results..."

    class MockUsage:
        def __init__(self):
            self.input_tokens = 100
            self.output_tokens = 75

    class MockResponsesAPIResponse:
        def __init__(self):
            self.output = [MockWebSearchItem(), MockWebSearchItem(), MockMessageItem()]
            self.usage = MockUsage()
            self.model = "gpt-4o"

    mock_response = MockResponsesAPIResponse()

    with patch(
        "openai.resources.responses.Responses.create", return_value=mock_response
    ):
        # Manually call the tracking since we're testing the converter logic
        from posthog.ai.utils import call_llm_and_track_usage

        def mock_create_call(**kwargs):
            return mock_response

        result = call_llm_and_track_usage(
            posthog_distinct_id="test-id",
            ph_client=mock_client,
            provider="openai",
            posthog_trace_id=None,
            posthog_properties=None,
            posthog_privacy_mode=False,
            posthog_groups=None,
            base_url="https://api.openai.com/v1",
            call_method=mock_create_call,
            model="gpt-4o",
            messages=[{"role": "user", "content": "Search query"}],
        )

        assert result == mock_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Verify exact web search count
        assert props["$ai_web_search_count"] == 2


@pytest.fixture
def streaming_web_search_chunks():
    """Streaming chunks with web search indicators (Perplexity-style)."""
    return [
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
                        content="Based on my search, ",
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
                        content="here are the latest news...",
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
                    delta=ChoiceDelta(),
                    finish_reason="stop",
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=20,
                completion_tokens=15,
                total_tokens=35,
            ),
        ),
    ]


def test_streaming_with_web_search(mock_client, streaming_web_search_chunks):
    """Test that web search count is properly captured in streaming mode."""

    # Add citations attribute to the last chunk to indicate web search was used
    streaming_web_search_chunks[-1].citations = ["https://example.com/news"]

    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        mock_create.return_value = streaming_web_search_chunks

        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response_generator = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Search for recent news"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        # Consume the generator to trigger the event capture
        chunks = list(response_generator)

        # Verify the chunks were returned correctly
        assert len(chunks) == 3
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Verify web search count is captured (binary detection = 1)
        assert props["$ai_web_search_count"] == 1
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 15


def test_streaming_with_web_search_on_non_usage_chunk(
    mock_client, streaming_web_search_chunks
):
    """Test that web search count is captured even when citations appear on chunks without usage data."""

    # Add citations attribute to the FIRST chunk (which has no usage data)
    # This tests the fix for the bug where web search indicators on non-usage chunks were ignored
    streaming_web_search_chunks[0].citations = ["https://example.com/news"]

    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        mock_create.return_value = streaming_web_search_chunks

        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response_generator = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Search for recent news"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        # Consume the generator to trigger the event capture
        chunks = list(response_generator)

        # Verify the chunks were returned correctly
        assert len(chunks) == 3
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Verify web search count is captured even though citations were on first chunk
        assert props["$ai_web_search_count"] == 1
        assert props["$ai_input_tokens"] == 20
        assert props["$ai_output_tokens"] == 15


@pytest.mark.asyncio
async def test_async_chat_with_web_search(mock_client):
    """Test that web search count is properly tracked in async non-streaming mode."""

    # Create mock response with citations (Perplexity-style)
    mock_response = ChatCompletion(
        id="chatcmpl-test",
        model="gpt-4",
        object="chat.completion",
        created=1234567890,
        choices=[
            Choice(
                index=0,
                message=ChatCompletionMessage(
                    role="assistant",
                    content="Here are the search results...",
                ),
                finish_reason="stop",
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=20,
            completion_tokens=15,
            total_tokens=35,
        ),
    )

    # Add citations attribute to indicate web search
    mock_response.citations = ["https://example.com/result1"]

    async def mock_create(self, **kwargs):
        return mock_response

    with patch(
        "openai.resources.chat.completions.AsyncCompletions.create", new=mock_create
    ):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Search for recent news"}],
            posthog_distinct_id="test-id",
        )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Verify web search count is captured (binary detection = 1)
    assert props["$ai_web_search_count"] == 1
    assert props["$ai_input_tokens"] == 20
    assert props["$ai_output_tokens"] == 15


@pytest.mark.asyncio
async def test_async_chat_streaming_with_web_search(
    mock_client, streaming_web_search_chunks
):
    """Test that web search count is properly captured in async streaming mode."""

    # Add citations attribute to the last chunk to indicate web search was used
    streaming_web_search_chunks[-1].citations = ["https://example.com/news"]

    captured_kwargs = {}

    async def mock_create(self, **kwargs):
        captured_kwargs["kwargs"] = kwargs

        async def chunk_iterable():
            for chunk in streaming_web_search_chunks:
                yield chunk

        return chunk_iterable()

    with patch(
        "openai.resources.chat.completions.AsyncCompletions.create", new=mock_create
    ):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response_stream = await client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Search for recent news"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        chunks = []
        async for chunk in response_stream:
            chunks.append(chunk)

    # Verify the chunks were returned correctly
    assert len(chunks) == 3
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Verify web search count is captured (binary detection = 1)
    assert props["$ai_web_search_count"] == 1
    assert props["$ai_input_tokens"] == 20
    assert props["$ai_output_tokens"] == 15


# Tests for model extraction fallback (stored prompts support)


def test_streaming_chat_extracts_model_from_chunk_when_not_in_kwargs(mock_client):
    """Test that model is extracted from streaming chunks when not provided in kwargs (stored prompts)."""

    # Create streaming chunks with model field but we won't pass model in kwargs
    chunks = [
        ChatCompletionChunk(
            id="chunk1",
            model="gpt-4o-stored-prompt",  # Model comes from response, not request
            object="chat.completion.chunk",
            created=1234567890,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(role="assistant", content="Hello"),
                    finish_reason=None,
                )
            ],
        ),
        ChatCompletionChunk(
            id="chunk2",
            model="gpt-4o-stored-prompt",
            object="chat.completion.chunk",
            created=1234567891,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(content=" world"),
                    finish_reason="stop",
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        ),
    ]

    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        mock_create.return_value = chunks

        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Note: NOT passing model in kwargs - simulates stored prompt usage
        response_generator = client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        # Consume the generator
        list(response_generator)

        assert mock_client.capture.call_count == 1
        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Model should be extracted from chunk, not kwargs
        assert props["$ai_model"] == "gpt-4o-stored-prompt"


def test_streaming_chat_prefers_kwargs_model_over_chunk_model(mock_client):
    """Test that model from kwargs takes precedence over model from chunk."""
    chunks = [
        ChatCompletionChunk(
            id="chunk1",
            model="gpt-4o-from-response",
            object="chat.completion.chunk",
            created=1234567890,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(role="assistant", content="Hello"),
                    finish_reason="stop",
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        ),
    ]

    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        mock_create.return_value = chunks

        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        response_generator = client.chat.completions.create(
            model="gpt-4o-from-kwargs",  # Explicitly passed model
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        list(response_generator)

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # kwargs model should take precedence
        assert props["$ai_model"] == "gpt-4o-from-kwargs"


def test_streaming_responses_api_extracts_model_from_response_object(mock_client):
    """Test that Responses API streaming extracts model from chunk.response.model (stored prompts)."""
    from unittest.mock import MagicMock
    from openai.types.responses import ResponseUsage

    chunks = []

    # Content chunk
    chunk1 = MagicMock()
    chunk1.type = "response.text.delta"
    chunk1.text = "Test response"
    # No response attribute on content chunks
    del chunk1.response
    chunks.append(chunk1)

    # Final chunk with response object containing model
    chunk2 = MagicMock()
    chunk2.type = "response.completed"
    chunk2.response = MagicMock()
    chunk2.response.model = "gpt-4o-mini-stored"  # Model from stored prompt
    chunk2.response.usage = ResponseUsage(
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        input_tokens_details={"prompt_tokens": 20, "cached_tokens": 0},
        output_tokens_details={"reasoning_tokens": 0},
    )
    chunk2.response.output = ["Test response"]
    chunks.append(chunk2)

    with patch("openai.resources.responses.Responses.create") as mock_create:
        mock_create.return_value = iter(chunks)

        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Note: NOT passing model - simulates stored prompt
        response_generator = client.responses.create(
            input=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        list(response_generator)

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Model should be extracted from chunk.response.model
        assert props["$ai_model"] == "gpt-4o-mini-stored"


def test_non_streaming_extracts_model_from_response(mock_client):
    """Test that non-streaming calls extract model from response when not in kwargs."""
    # Create a response with model but we won't pass model in kwargs
    mock_response = ChatCompletion(
        id="test",
        model="gpt-4o-stored-prompt",
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

    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Note: NOT passing model in kwargs
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
        )

        assert response == mock_response
        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Model should be extracted from response.model
        assert props["$ai_model"] == "gpt-4o-stored-prompt"


def test_non_streaming_responses_api_extracts_model_from_response(mock_client):
    """Test that non-streaming Responses API extracts model from response when not in kwargs."""
    mock_response = Response(
        id="test",
        model="gpt-4o-mini-stored",
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
            output_tokens_details={"reasoning_tokens": 0},
            total_tokens=20,
        ),
        user=None,
        metadata={},
    )

    with patch(
        "openai.resources.responses.Responses.create",
        return_value=mock_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Note: NOT passing model in kwargs
        response = client.responses.create(
            input="Hello",
            posthog_distinct_id="test-id",
        )

        assert response == mock_response
        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Model should be extracted from response.model
        assert props["$ai_model"] == "gpt-4o-mini-stored"


def test_non_streaming_returns_none_when_no_model(mock_client):
    """Test that non-streaming returns None (not 'unknown') when model is not available anywhere."""
    # Create a response without model attribute using real OpenAI types
    mock_response = ChatCompletion(
        id="test",
        model="",  # Will be removed below
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
            completion_tokens=5,
            prompt_tokens=10,
            total_tokens=15,
        ),
    )
    # Remove model attribute to simulate missing model
    object.__delattr__(mock_response, "model")

    with patch(
        "openai.resources.chat.completions.Completions.create",
        return_value=mock_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        # Note: NOT passing model in kwargs and response has no model
        client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello"}],
            posthog_distinct_id="test-id",
        )

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Should be None, NOT "unknown" (to avoid incorrect cost matching)
        assert props["$ai_model"] is None


def test_streaming_falls_back_to_unknown_when_no_model(mock_client):
    """Test that streaming falls back to 'unknown' when model is not available anywhere."""
    from unittest.mock import MagicMock

    # Create a chunk without model attribute
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = "Hello"
    chunk.choices[0].delta.role = "assistant"
    chunk.choices[0].delta.tool_calls = None
    chunk.usage = CompletionUsage(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    # Explicitly remove model attribute
    del chunk.model

    with patch("openai.resources.chat.completions.Completions.create") as mock_create:
        mock_create.return_value = [chunk]

        client = OpenAI(api_key="test-key", posthog_client=mock_client)

        response_generator = client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        list(response_generator)

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Should fall back to "unknown"
        assert props["$ai_model"] == "unknown"


@pytest.mark.asyncio
async def test_async_streaming_chat_extracts_model_from_chunk(mock_client):
    """Test async streaming extracts model from chunk when not in kwargs."""
    chunks = [
        ChatCompletionChunk(
            id="chunk1",
            model="gpt-4o-async-stored",
            object="chat.completion.chunk",
            created=1234567890,
            choices=[
                ChoiceChunk(
                    index=0,
                    delta=ChoiceDelta(role="assistant", content="Hello"),
                    finish_reason="stop",
                )
            ],
            usage=CompletionUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        ),
    ]

    async def mock_create(self, **kwargs):
        async def chunk_iterable():
            for chunk in chunks:
                yield chunk

        return chunk_iterable()

    with patch(
        "openai.resources.chat.completions.AsyncCompletions.create", new=mock_create
    ):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        # Note: NOT passing model
        response_stream = await client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        async for _ in response_stream:
            pass

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert props["$ai_model"] == "gpt-4o-async-stored"


@pytest.mark.asyncio
async def test_async_streaming_responses_extracts_model_from_response(mock_client):
    """Test async Responses API streaming extracts model from chunk.response.model."""
    from unittest.mock import MagicMock
    from openai.types.responses import ResponseUsage

    chunks = []

    chunk1 = MagicMock()
    chunk1.type = "response.text.delta"
    chunk1.text = "Test"
    del chunk1.response
    chunks.append(chunk1)

    chunk2 = MagicMock()
    chunk2.type = "response.completed"
    chunk2.response = MagicMock()
    chunk2.response.model = "gpt-4o-mini-async-stored"
    chunk2.response.usage = ResponseUsage(
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        input_tokens_details={"prompt_tokens": 20, "cached_tokens": 0},
        output_tokens_details={"reasoning_tokens": 0},
    )
    chunk2.response.output = ["Test"]
    chunks.append(chunk2)

    async def mock_create(self, **kwargs):
        async def chunk_iterable():
            for chunk in chunks:
                yield chunk

        return chunk_iterable()

    with patch("openai.resources.responses.AsyncResponses.create", new=mock_create):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response_stream = await client.responses.create(
            input=[{"role": "user", "content": "Hello"}],
            stream=True,
            posthog_distinct_id="test-id",
        )

        async for _ in response_stream:
            pass

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert props["$ai_model"] == "gpt-4o-mini-async-stored"


# Tests for audio transcriptions


@pytest.fixture
def mock_transcription_response():
    return Transcription(text="Hello world, this is a test transcription.")


@pytest.fixture
def mock_transcription_response_with_duration():
    return Transcription(
        text="Hello world, this is a test transcription.",
        duration=12.5,
    )


def test_transcription(mock_client, mock_transcription_response):
    """Test basic transcription tracking."""
    from io import BytesIO

    mock_file = BytesIO(b"fake audio data")
    mock_file.name = "test_audio.mp3"

    with patch(
        "openai.resources.audio.transcriptions.Transcriptions.create",
        return_value=mock_transcription_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=mock_file,
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

        assert response == mock_transcription_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert call_args["distinct_id"] == "test-id"
        assert call_args["event"] == "$ai_transcription"
        assert props["$ai_provider"] == "openai"
        assert props["$ai_model"] == "whisper-1"
        assert props["$ai_input"] == "test_audio.mp3"
        assert props["$ai_output_text"] == "Hello world, this is a test transcription."
        assert props["$ai_http_status"] == 200
        assert props["foo"] == "bar"
        assert isinstance(props["$ai_latency"], float)


def test_transcription_with_duration(
    mock_client, mock_transcription_response_with_duration
):
    """Test transcription tracking with audio duration."""
    from io import BytesIO

    mock_file = BytesIO(b"fake audio data")
    mock_file.name = "test_audio.mp3"

    with patch(
        "openai.resources.audio.transcriptions.Transcriptions.create",
        return_value=mock_transcription_response_with_duration,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=mock_file,
            posthog_distinct_id="test-id",
        )

        assert response == mock_transcription_response_with_duration
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert props["$ai_audio_duration"] == 12.5


def test_transcription_with_language(mock_client, mock_transcription_response):
    """Test transcription tracking with language parameter."""
    from io import BytesIO

    mock_file = BytesIO(b"fake audio data")
    mock_file.name = "test_audio.mp3"

    with patch(
        "openai.resources.audio.transcriptions.Transcriptions.create",
        return_value=mock_transcription_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=mock_file,
            language="en",
            posthog_distinct_id="test-id",
        )

        assert response == mock_transcription_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        assert props["$ai_language"] == "en"


def test_transcription_groups(mock_client, mock_transcription_response):
    """Test transcription tracking with groups."""
    from io import BytesIO

    mock_file = BytesIO(b"fake audio data")
    mock_file.name = "test_audio.mp3"

    with patch(
        "openai.resources.audio.transcriptions.Transcriptions.create",
        return_value=mock_transcription_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=mock_file,
            posthog_distinct_id="test-id",
            posthog_groups={"company": "test_company"},
        )

        assert response == mock_transcription_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]

        assert call_args["groups"] == {"company": "test_company"}


def test_transcription_privacy_mode(mock_client, mock_transcription_response):
    """Test transcription tracking with privacy mode enabled."""
    from io import BytesIO

    mock_file = BytesIO(b"fake audio data")
    mock_file.name = "test_audio.mp3"

    with patch(
        "openai.resources.audio.transcriptions.Transcriptions.create",
        return_value=mock_transcription_response,
    ):
        client = OpenAI(api_key="test-key", posthog_client=mock_client)
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=mock_file,
            posthog_distinct_id="test-id",
            posthog_privacy_mode=True,
        )

        assert response == mock_transcription_response
        assert mock_client.capture.call_count == 1

        call_args = mock_client.capture.call_args[1]
        props = call_args["properties"]

        # Input and output should be redacted
        assert props["$ai_input"] is None
        assert props["$ai_output_text"] is None


@pytest.mark.asyncio
async def test_async_transcription(mock_client, mock_transcription_response):
    """Test async transcription tracking."""
    from io import BytesIO

    mock_file = BytesIO(b"fake audio data")
    mock_file.name = "test_audio.mp3"

    mock_create = AsyncMock(return_value=mock_transcription_response)

    with patch(
        "openai.resources.audio.transcriptions.AsyncTranscriptions.create",
        new=mock_create,
    ):
        client = AsyncOpenAI(api_key="test-key", posthog_client=mock_client)

        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=mock_file,
            posthog_distinct_id="test-id",
            posthog_properties={"foo": "bar"},
        )

    assert response == mock_transcription_response
    assert mock_create.await_count == 1
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_transcription"
    assert props["$ai_provider"] == "openai"
    assert props["$ai_model"] == "whisper-1"
    assert props["$ai_input"] == "test_audio.mp3"
    assert props["$ai_output_text"] == "Hello world, this is a test transcription."
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)
