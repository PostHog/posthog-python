from unittest.mock import patch, MagicMock

import pytest

try:
    from posthog.ai.litellm import completion, acompletion, embedding

    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not LITELLM_AVAILABLE, reason="LiteLLM package is not available"
)


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.capture = MagicMock()
        mock_client.privacy_mode = False
        yield mock_client


@pytest.fixture
def mock_usage():
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 20
    usage.total_tokens = 30
    usage.prompt_tokens_details = MagicMock()
    usage.prompt_tokens_details.cached_tokens = 0
    usage.output_tokens_details = MagicMock()
    usage.output_tokens_details.reasoning_tokens = 0
    return usage


@pytest.fixture
def mock_usage_with_cached_tokens():
    usage = MagicMock()
    usage.prompt_tokens = 20
    usage.completion_tokens = 15
    usage.total_tokens = 35
    usage.prompt_tokens_details = MagicMock()
    usage.prompt_tokens_details.cached_tokens = 15
    usage.output_tokens_details = MagicMock()
    usage.output_tokens_details.reasoning_tokens = 5
    return usage


@pytest.fixture
def mock_response(mock_usage):
    response = MagicMock()
    response.usage = mock_usage
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.content = "Hello, world!"
    response.choices[0].message.role = "assistant"
    return response


@pytest.fixture
def mock_response_with_cached_tokens(mock_usage_with_cached_tokens):
    response = MagicMock()
    response.usage = mock_usage_with_cached_tokens
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.content = "Hello with cached tokens!"
    response.choices[0].message.role = "assistant"
    return response


@pytest.fixture
def mock_response_tool_calls_only():
    response = MagicMock()
    response.usage = MagicMock()
    response.usage.prompt_tokens = 25
    response.usage.completion_tokens = 10
    response.usage.total_tokens = 35
    response.usage.prompt_tokens_details = MagicMock()
    response.usage.prompt_tokens_details.cached_tokens = 0
    response.usage.output_tokens_details = MagicMock()
    response.usage.output_tokens_details.reasoning_tokens = 0
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.content = None
    response.choices[0].message.role = "assistant"
    response.choices[0].message.tool_calls = [MagicMock()]
    response.choices[0].message.tool_calls[0].id = "call_def456"
    response.choices[0].message.tool_calls[0].type = "function"
    response.choices[0].message.tool_calls[0].function = MagicMock()
    response.choices[0].message.tool_calls[0].function.name = "get_weather"
    response.choices[0].message.tool_calls[0].function.arguments = '{"location": "New York"}'
    return response


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_basic(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    response = completion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Hello"}],
        posthog_distinct_id="test-user",
    )

    assert response == mock_response

    mock_litellm_completion.assert_called_once()
    call_kwargs = mock_litellm_completion.call_args[1]
    assert call_kwargs["model"] == "openai/gpt-3.5-turbo"
    assert call_kwargs["messages"] == [{"role": "user", "content": "Hello"}]

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["event"] == "$ai_generation"
    assert call_args[1]["properties"]["$ai_provider"] == "litellm"
    assert call_args[1]["properties"]["$ai_model"] == "openai/gpt-3.5-turbo"
    assert call_args[1]["properties"]["$ai_input_tokens"] == 10
    assert call_args[1]["properties"]["$ai_output_tokens"] == 20


@patch("posthog.ai.litellm.litellm.litellm.acompletion")
@patch("posthog.ai.litellm.litellm.setup")
@pytest.mark.asyncio
async def test_acompletion_basic(
    mock_setup, mock_litellm_acompletion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_acompletion.return_value = mock_response

    response = await acompletion(
        model="anthropic/claude-3-sonnet-20240229",
        messages=[{"role": "user", "content": "Hello async"}],
        posthog_distinct_id="test-user-async",
    )

    assert response == mock_response

    mock_litellm_acompletion.assert_called_once()
    call_kwargs = mock_litellm_acompletion.call_args[1]
    assert call_kwargs["model"] == "anthropic/claude-3-sonnet-20240229"
    assert call_kwargs["messages"] == [{"role": "user", "content": "Hello async"}]

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["event"] == "$ai_generation"
    assert call_args[1]["properties"]["$ai_provider"] == "litellm"
    assert (
        call_args[1]["properties"]["$ai_model"] == "anthropic/claude-3-sonnet-20240229"
    )


@patch("posthog.ai.litellm.litellm.litellm.acompletion")
@patch("posthog.ai.litellm.litellm.setup")
@pytest.mark.asyncio
async def test_acompletion_with_base64_image_sanitization(
    mock_setup, mock_litellm_acompletion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_acompletion.return_value = mock_response

    base64_image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUl=="

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Analyze this image"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": base64_image_url,
                        "detail": "low"
                    }
                }
            ]
        }
    ]

    response = await acompletion(
        model="anthropic/claude-3-haiku-20240307",
        messages=messages,
        posthog_distinct_id="test-user-async",
    )

    assert response == mock_response

    call_args = mock_client.capture.call_args
    sanitized_input = call_args[1]["properties"]["$ai_input"]

    assert sanitized_input[0]["content"][0]["text"] == "Analyze this image"
    assert sanitized_input[0]["content"][1]["image_url"]["url"] == "[base64 image redacted]"
    assert sanitized_input[0]["content"][1]["image_url"]["detail"] == "low"


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_with_tools(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather information",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    _response = completion(
        model="openai/gpt-4",
        messages=[{"role": "user", "content": "What's the weather?"}],
        tools=tools,
        posthog_distinct_id="test-user",
    )

    call_kwargs = mock_litellm_completion.call_args[1]
    assert call_kwargs["tools"] == tools

    call_args = mock_client.capture.call_args
    assert call_args[1]["properties"]["$ai_tools"] == tools


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_privacy_mode(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    response = completion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Sensitive information"}],
        posthog_distinct_id="test-user",
        posthog_privacy_mode=True,
    )

    call_args = mock_client.capture.call_args
    assert call_args[1]["properties"]["$ai_input"] is None
    assert call_args[1]["properties"]["$ai_output_choices"] is None


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_with_base64_image_sanitization(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    base64_image_url = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": base64_image_url,
                        "detail": "high"
                    }
                }
            ]
        }
    ]

    response = completion(
        model="openai/gpt-4-vision-preview",
        messages=messages,
        posthog_distinct_id="test-user",
    )

    assert response == mock_response

    call_args = mock_client.capture.call_args
    sanitized_input = call_args[1]["properties"]["$ai_input"]

    assert sanitized_input[0]["content"][0]["text"] == "What is in this image?"
    assert sanitized_input[0]["content"][1]["image_url"]["url"] == "[base64 image redacted]"
    assert sanitized_input[0]["content"][1]["image_url"]["detail"] == "high"


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_streaming(mock_setup, mock_litellm_completion, mock_client):
    mock_setup.return_value = mock_client

    mock_chunk = MagicMock()
    mock_chunk.usage = MagicMock()
    mock_chunk.usage.prompt_tokens = 10
    mock_chunk.usage.completion_tokens = 5
    mock_chunk.usage.total_tokens = 15

    mock_chunk.choices = [MagicMock()]
    mock_chunk.choices[0].delta = MagicMock()
    mock_chunk.choices[0].delta.content = "Hello"

    mock_litellm_completion.return_value = [mock_chunk]

    generator = completion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Stream test"}],
        stream=True,
        posthog_distinct_id="test-user",
    )

    list(generator)

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["properties"]["$ai_provider"] == "litellm"
    assert call_args[1]["properties"]["$ai_input_tokens"] == 10
    assert call_args[1]["properties"]["$ai_output_tokens"] == 5


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_streaming_with_base64_image_sanitization(mock_setup, mock_litellm_completion, mock_client):
    mock_setup.return_value = mock_client

    mock_chunk = MagicMock()
    mock_chunk.usage = MagicMock()
    mock_chunk.usage.prompt_tokens = 15
    mock_chunk.usage.completion_tokens = 8
    mock_chunk.usage.total_tokens = 23

    mock_chunk.choices = [MagicMock()]
    mock_chunk.choices[0].delta = MagicMock()
    mock_chunk.choices[0].delta.content = "This is an image"

    mock_litellm_completion.return_value = [mock_chunk]

    base64_image_url = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": base64_image_url,
                        "detail": "auto"
                    }
                }
            ]
        }
    ]

    generator = completion(
        model="openai/gpt-4-vision-preview",
        messages=messages,
        stream=True,
        posthog_distinct_id="test-user-streaming",
    )

    list(generator)

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["properties"]["$ai_provider"] == "litellm"

    sanitized_input = call_args[1]["properties"]["$ai_input"]
    assert sanitized_input[0]["content"][0]["text"] == "Describe this image"
    assert sanitized_input[0]["content"][1]["image_url"]["url"] == "[base64 image redacted]"
    assert sanitized_input[0]["content"][1]["image_url"]["detail"] == "auto"


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_error_handling(mock_setup, mock_litellm_completion, mock_client):
    mock_setup.return_value = mock_client
    mock_litellm_completion.side_effect = Exception("API Error")

    with pytest.raises(Exception) as exc_info:
        completion(
            model="openai/gpt-3.5-turbo",
            messages=[{"role": "user", "content": "This will fail"}],
            posthog_distinct_id="test-user",
        )

    assert str(exc_info.value) == "API Error"

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["properties"]["$ai_is_error"] is True
    assert call_args[1]["properties"]["$ai_error"] == "API Error"


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_custom_properties(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    custom_props = {"custom_key": "custom_value", "environment": "test"}

    response = completion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Hello"}],
        posthog_distinct_id="test-user",
        posthog_properties=custom_props,
    )

    call_args = mock_client.capture.call_args
    properties = call_args[1]["properties"]
    assert properties["custom_key"] == "custom_value"
    assert properties["environment"] == "test"


@pytest.fixture
def mock_embedding_response():
    response = MagicMock()
    response.data = [
        MagicMock(
            embedding=[0.1, 0.2, 0.3],
            index=0,
            object="embedding",
        )
    ]
    response.model = "text-embedding-3-small"
    response.object = "list"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.total_tokens = 10
    response.usage.prompt_tokens_details = MagicMock()
    response.usage.prompt_tokens_details.cached_tokens = 0
    response.usage.output_tokens_details = MagicMock()
    response.usage.output_tokens_details.reasoning_tokens = 0
    return response


@patch("posthog.ai.litellm.litellm.litellm.embedding")
@patch("posthog.ai.litellm.litellm.setup")
def test_embedding_basic(
    mock_setup, mock_litellm_embedding, mock_client, mock_embedding_response
):
    mock_setup.return_value = mock_client
    mock_litellm_embedding.return_value = mock_embedding_response

    response = embedding(
        model="openai/text-embedding-3-small",
        input="Hello world",
        posthog_distinct_id="test-user",
        posthog_properties={"foo": "bar"},
    )

    assert response == mock_embedding_response

    mock_litellm_embedding.assert_called_once()
    call_kwargs = mock_litellm_embedding.call_args[1]
    assert call_kwargs["model"] == "openai/text-embedding-3-small"
    assert call_kwargs["input"] == "Hello world"

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["event"] == "$ai_embedding"
    assert call_args[1]["properties"]["$ai_provider"] == "litellm"
    assert call_args[1]["properties"]["$ai_model"] == "openai/text-embedding-3-small"
    assert call_args[1]["properties"]["$ai_input"] == "Hello world"
    assert call_args[1]["properties"]["$ai_input_tokens"] == 10
    assert call_args[1]["properties"]["$ai_http_status"] == 200
    assert call_args[1]["properties"]["foo"] == "bar"
    assert isinstance(call_args[1]["properties"]["$ai_latency"], float)


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_groups(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    response = completion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Hello"}],
        posthog_distinct_id="test-id",
        posthog_groups={"company": "test_company", "team": "engineering"},
    )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    assert call_args["groups"] == {"company": "test_company", "team": "engineering"}


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_cached_tokens(
    mock_setup, mock_litellm_completion, mock_client, mock_response_with_cached_tokens
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response_with_cached_tokens

    response = completion(
        model="openai/gpt-4",
        messages=[{"role": "user", "content": "Hello"}],
        posthog_distinct_id="test-id",
        posthog_properties={"foo": "bar"},
    )

    assert response == mock_response_with_cached_tokens
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "litellm"
    assert props["$ai_model"] == "openai/gpt-4"
    assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]
    assert props["$ai_output_choices"] == [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello with cached tokens!"}],
        }
    ]
    assert props["$ai_input_tokens"] == 20
    assert props["$ai_output_tokens"] == 15
    assert props["$ai_cache_read_input_tokens"] == 15
    assert props["$ai_reasoning_tokens"] == 5
    assert props["$ai_http_status"] == 200
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_tool_calls_only_no_content(
    mock_setup, mock_litellm_completion, mock_client, mock_response_tool_calls_only
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response_tool_calls_only

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {},
            },
        }
    ]

    response = completion(
        model="openai/gpt-4",
        messages=[{"role": "user", "content": "Get weather for New York"}],
        tools=tools,
        posthog_distinct_id="test-id",
    )

    assert response == mock_response_tool_calls_only
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "litellm"
    assert props["$ai_model"] == "openai/gpt-4"
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

    assert "$ai_tools" in props
    defined_tools = props["$ai_tools"]
    assert len(defined_tools) == 1

    defined_tool = defined_tools[0]
    assert defined_tool["type"] == "function"
    assert defined_tool["function"]["name"] == "get_weather"
    assert defined_tool["function"]["description"] == "Get weather"
    assert defined_tool["function"]["parameters"] == {}

    assert props["$ai_input_tokens"] == 25
    assert props["$ai_output_tokens"] == 10
    assert props["$ai_http_status"] == 200


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_streaming_with_tool_calls(mock_setup, mock_litellm_completion, mock_client):
    mock_setup.return_value = mock_client

    tool_call_chunks = [
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]

    tool_call_chunks[0].choices = [MagicMock()]
    tool_call_chunks[0].choices[0].delta = MagicMock()
    tool_call_chunks[0].choices[0].delta.tool_calls = [MagicMock()]
    tool_call_chunks[0].choices[0].delta.tool_calls[0].index = 0
    tool_call_chunks[0].choices[0].delta.tool_calls[0].id = "call_abc123"
    tool_call_chunks[0].choices[0].delta.tool_calls[0].type = "function"
    tool_call_chunks[0].choices[0].delta.tool_calls[0].function = MagicMock()
    tool_call_chunks[0].choices[0].delta.tool_calls[0].function.name = "get_weather"
    tool_call_chunks[0].choices[0].delta.tool_calls[0].function.arguments = '{"location": "'
    tool_call_chunks[0].choices[0].delta.content = None
    tool_call_chunks[0].choices[0].delta.finish_reason = None

    tool_call_chunks[1].choices = [MagicMock()]
    tool_call_chunks[1].choices[0].delta = MagicMock()
    tool_call_chunks[1].choices[0].delta.tool_calls = [MagicMock()]
    tool_call_chunks[1].choices[0].delta.tool_calls[0].index = 0
    tool_call_chunks[1].choices[0].delta.tool_calls[0].function = MagicMock()
    tool_call_chunks[1].choices[0].delta.tool_calls[0].function.arguments = 'San Francisco"'
    tool_call_chunks[1].choices[0].delta.content = None
    tool_call_chunks[1].choices[0].delta.finish_reason = None

    tool_call_chunks[2].choices = [MagicMock()]
    tool_call_chunks[2].choices[0].delta = MagicMock()
    tool_call_chunks[2].choices[0].delta.tool_calls = [MagicMock()]
    tool_call_chunks[2].choices[0].delta.tool_calls[0].index = 0
    tool_call_chunks[2].choices[0].delta.tool_calls[0].function = MagicMock()
    tool_call_chunks[2].choices[0].delta.tool_calls[0].function.arguments = ', "unit": "celsius"}'
    tool_call_chunks[2].choices[0].delta.content = None
    tool_call_chunks[2].choices[0].delta.finish_reason = None

    tool_call_chunks[3].choices = [MagicMock()]
    tool_call_chunks[3].choices[0].delta = MagicMock()
    tool_call_chunks[3].choices[0].delta.content = "The weather in San Francisco is 15°C."
    tool_call_chunks[3].choices[0].delta.tool_calls = None
    tool_call_chunks[3].usage = MagicMock()
    tool_call_chunks[3].usage.prompt_tokens = 20
    tool_call_chunks[3].usage.completion_tokens = 15
    tool_call_chunks[3].usage.total_tokens = 35
    tool_call_chunks[3].usage.prompt_tokens_details = MagicMock()
    tool_call_chunks[3].usage.prompt_tokens_details.cached_tokens = 0
    tool_call_chunks[3].usage.output_tokens_details = MagicMock()
    tool_call_chunks[3].usage.output_tokens_details.reasoning_tokens = 0

    mock_litellm_completion.return_value = tool_call_chunks

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {},
            },
        }
    ]

    generator = completion(
        model="openai/gpt-4",
        messages=[
            {"role": "user", "content": "What's the weather in San Francisco?"}
        ],
        tools=tools,
        stream=True,
        posthog_distinct_id="test-id",
    )

    chunks = list(generator)

    assert len(chunks) == 4
    assert chunks == tool_call_chunks

    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "litellm"
    assert props["$ai_model"] == "openai/gpt-4"

    assert "$ai_tools" in props
    defined_tools = props["$ai_tools"]
    assert len(defined_tools) == 1

    defined_tool = defined_tools[0]
    assert defined_tool["type"] == "function"
    assert defined_tool["function"]["name"] == "get_weather"
    assert defined_tool["function"]["description"] == "Get weather"
    assert defined_tool["function"]["parameters"] == {}

    assert (
        props["$ai_output_choices"][0]["content"]
        == "The weather in San Francisco is 15°C."
    )

    assert props["$ai_input_tokens"] == 20
    assert props["$ai_output_tokens"] == 15


@patch("posthog.ai.litellm.litellm.litellm.completion")
@patch("posthog.ai.litellm.litellm.setup")
def test_completion_privacy_mode_global(
    mock_setup, mock_litellm_completion, mock_client, mock_response
):
    mock_setup.return_value = mock_client
    mock_litellm_completion.return_value = mock_response

    mock_client.privacy_mode = True

    response = completion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Sensitive information"}],
        posthog_distinct_id="test-id",
        posthog_privacy_mode=False,
    )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] is None
    assert props["$ai_output_choices"] is None


@patch("posthog.ai.litellm.litellm.litellm.acompletion")
@patch("posthog.ai.litellm.litellm.setup")
@pytest.mark.asyncio
async def test_acompletion_streaming(mock_setup, mock_litellm_acompletion, mock_client):
    mock_setup.return_value = mock_client

    mock_chunk1 = MagicMock()
    mock_chunk1.usage = MagicMock()
    mock_chunk1.usage.prompt_tokens = 10
    mock_chunk1.usage.completion_tokens = 5
    mock_chunk1.usage.total_tokens = 15

    mock_chunk1.choices = [MagicMock()]
    mock_chunk1.choices[0].delta = MagicMock()
    mock_chunk1.choices[0].delta.content = "Hello"

    mock_chunk2 = MagicMock()
    mock_chunk2.usage = None
    mock_chunk2.choices = [MagicMock()]
    mock_chunk2.choices[0].delta = MagicMock()
    mock_chunk2.choices[0].delta.content = " world!"

    async def async_generator():
        yield mock_chunk1
        yield mock_chunk2

    mock_litellm_acompletion.return_value = async_generator()

    generator = await acompletion(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Stream test async"}],
        stream=True,
        posthog_distinct_id="test-user-async",
    )

    chunks = []
    async for chunk in generator:
        chunks.append(chunk)

    assert len(chunks) == 2
    assert chunks[0] == mock_chunk1
    assert chunks[1] == mock_chunk2

    mock_client.capture.assert_called_once()
    call_args = mock_client.capture.call_args
    assert call_args[1]["properties"]["$ai_provider"] == "litellm"
    assert call_args[1]["properties"]["$ai_input_tokens"] == 10
    assert call_args[1]["properties"]["$ai_output_tokens"] == 5
    assert call_args[1]["properties"]["$ai_output_choices"][0]["content"] == "Hello world!"
