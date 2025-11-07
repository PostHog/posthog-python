from unittest.mock import MagicMock, patch

import pytest

try:
    from google import genai as google_genai

    from posthog.ai.gemini import Client

    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not GEMINI_AVAILABLE, reason="Google Gemini package is not available"
)


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


@pytest.fixture
def mock_gemini_response():
    mock_response = MagicMock()
    mock_response.text = "Test response from Gemini"

    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 20
    mock_usage.candidates_token_count = 10
    # Ensure cache and reasoning tokens are not present (not MagicMock)
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    mock_candidate = MagicMock()
    mock_candidate.text = "Test response from Gemini"
    mock_content = MagicMock()
    mock_part = MagicMock()
    mock_part.text = "Test response from Gemini"
    mock_content.parts = [mock_part]
    mock_candidate.content = mock_content
    mock_response.candidates = [mock_candidate]

    return mock_response


@pytest.fixture
def mock_google_genai_client():
    """Mock for the new google-genai Client"""
    with patch.object(google_genai, "Client") as mock_client_class:
        mock_client_instance = MagicMock()
        mock_models = MagicMock()
        mock_client_instance.models = mock_models
        mock_client_class.return_value = mock_client_instance
        yield mock_client_instance


@pytest.fixture
def mock_gemini_response_with_function_calls():
    mock_response = MagicMock()

    # Mock usage metadata
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 25
    mock_usage.candidates_token_count = 15
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    # Mock function call
    mock_function_call = MagicMock()
    mock_function_call.name = "get_current_weather"
    mock_function_call.args = {"location": "San Francisco"}

    # Mock text part 1
    mock_text_part1 = MagicMock()
    mock_text_part1.text = "I'll check the weather for you."
    # Make hasattr(part, "text") return True
    type(mock_text_part1).text = mock_text_part1.text

    # Mock text part 2
    mock_text_part2 = MagicMock()
    mock_text_part2.text = " Let me look that up."
    type(mock_text_part2).text = mock_text_part2.text

    # Mock function call part - need to ensure hasattr() works correctly
    mock_function_part = MagicMock()
    mock_function_part.function_call = mock_function_call
    # Make hasattr(part, "function_call") return True
    type(mock_function_part).function_call = mock_function_part.function_call
    # Ensure hasattr(part, "text") returns False for the function part
    del mock_function_part.text

    # Mock content with 2 text parts and 1 function call part
    mock_content = MagicMock()
    mock_content.parts = [mock_text_part1, mock_text_part2, mock_function_part]

    # Mock candidate
    mock_candidate = MagicMock()
    mock_candidate.content = mock_content
    mock_response.candidates = [mock_candidate]

    return mock_response


@pytest.fixture
def mock_gemini_response_function_calls_only():
    mock_response = MagicMock()

    # Mock usage metadata
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 30
    mock_usage.candidates_token_count = 12
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    # Mock function call
    mock_function_call = MagicMock()
    mock_function_call.name = "get_current_weather"
    mock_function_call.args = {"location": "New York", "unit": "fahrenheit"}

    # Mock function call part (no text part) - need to ensure hasattr() works correctly
    mock_function_part = MagicMock()
    mock_function_part.function_call = mock_function_call
    # Make hasattr(part, "function_call") return True
    type(mock_function_part).function_call = mock_function_part.function_call
    # Ensure hasattr(part, "text") returns False for the function part
    del mock_function_part.text

    # Mock content with only function call part
    mock_content = MagicMock()
    mock_content.parts = [mock_function_part]

    # Mock candidate
    mock_candidate = MagicMock()
    mock_candidate.content = mock_content
    mock_response.candidates = [mock_candidate]

    return mock_response


def test_new_client_basic_generation(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test the new Client/Models API structure"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Tell me a fun fact about hedgehogs"],
        posthog_distinct_id="test-id",
        posthog_properties={"foo": "bar"},
    )

    assert response == mock_gemini_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.0-flash"
    assert props["$ai_input_tokens"] == 20
    assert props["$ai_output_tokens"] == 10
    assert props["foo"] == "bar"
    assert "$ai_trace_id" in props
    assert props["$ai_latency"] > 0


def test_new_client_streaming_with_generate_content_stream(
    mock_client, mock_google_genai_client
):
    """Test the new generate_content_stream method"""

    def mock_streaming_response():
        mock_chunk1 = MagicMock()
        mock_chunk1.text = "Hello "
        mock_usage1 = MagicMock()
        mock_usage1.prompt_token_count = 10
        mock_usage1.candidates_token_count = 5
        mock_usage1.cached_content_token_count = 0
        mock_usage1.thoughts_token_count = 0
        mock_chunk1.usage_metadata = mock_usage1

        mock_chunk2 = MagicMock()
        mock_chunk2.text = "world!"
        mock_usage2 = MagicMock()
        mock_usage2.prompt_token_count = 10
        mock_usage2.candidates_token_count = 10
        mock_usage2.cached_content_token_count = 0
        mock_usage2.thoughts_token_count = 0
        mock_chunk2.usage_metadata = mock_usage2

        yield mock_chunk1
        yield mock_chunk2

    # Mock the generate_content_stream method
    mock_google_genai_client.models.generate_content_stream.return_value = (
        mock_streaming_response()
    )

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content_stream(
        model="gemini-2.0-flash",
        contents=["Write a short story"],
        posthog_distinct_id="test-id",
        posthog_properties={"feature": "streaming"},
    )

    chunks = list(response)
    assert len(chunks) == 2
    assert chunks[0].text == "Hello "
    assert chunks[1].text == "world!"

    # Check that the streaming event was captured
    assert mock_client.capture.call_count == 1
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.0-flash"
    assert props["$ai_input_tokens"] == 10
    assert props["$ai_output_tokens"] == 10
    assert props["feature"] == "streaming"
    assert isinstance(props["$ai_latency"], float)


def test_new_client_streaming_with_tools(mock_client, mock_google_genai_client):
    """Test that tools are captured in streaming mode"""

    def mock_streaming_response():
        mock_chunk1 = MagicMock()
        mock_chunk1.text = "I'll check "
        mock_usage1 = MagicMock()
        mock_usage1.prompt_token_count = 15
        mock_usage1.candidates_token_count = 5
        mock_usage1.cached_content_token_count = 0
        mock_usage1.thoughts_token_count = 0
        mock_chunk1.usage_metadata = mock_usage1

        mock_chunk2 = MagicMock()
        mock_chunk2.text = "the weather"
        mock_usage2 = MagicMock()
        mock_usage2.prompt_token_count = 15
        mock_usage2.candidates_token_count = 10
        mock_usage2.cached_content_token_count = 0
        mock_usage2.thoughts_token_count = 0
        mock_chunk2.usage_metadata = mock_usage2

        yield mock_chunk1
        yield mock_chunk2

    # Mock the generate_content_stream method
    mock_google_genai_client.models.generate_content_stream.return_value = (
        mock_streaming_response()
    )

    client = Client(api_key="test-key", posthog_client=mock_client)

    # Create mock tools configuration
    mock_tool = MagicMock()
    mock_tool.function_declarations = [
        MagicMock(
            name="get_current_weather",
            description="Gets the current weather for a given location.",
            parameters=MagicMock(
                type="OBJECT",
                properties={
                    "location": MagicMock(
                        type="STRING",
                        description="The city and state, e.g. San Francisco, CA",
                    )
                },
                required=["location"],
            ),
        )
    ]

    mock_config = MagicMock()
    mock_config.tools = [mock_tool]

    response = client.models.generate_content_stream(
        model="gemini-2.0-flash",
        contents=["What's the weather in SF?"],
        config=mock_config,
        posthog_distinct_id="test-id",
        posthog_properties={"feature": "streaming_with_tools"},
    )

    chunks = list(response)
    assert len(chunks) == 2
    assert chunks[0].text == "I'll check "
    assert chunks[1].text == "the weather"

    # Check that the streaming event was captured with tools
    assert mock_client.capture.call_count == 1
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.0-flash"
    assert props["$ai_input_tokens"] == 15
    assert props["$ai_output_tokens"] == 10
    assert props["feature"] == "streaming_with_tools"
    assert isinstance(props["$ai_latency"], float)

    # Verify that tools are captured in the $ai_tools property in streaming mode
    assert props["$ai_tools"] == [mock_tool]


def test_new_client_groups(mock_client, mock_google_genai_client, mock_gemini_response):
    """Test groups functionality with new Client API"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Hello"],
        posthog_distinct_id="test-id",
        posthog_groups={"company": "company_123"},
    )

    call_args = mock_client.capture.call_args[1]
    assert call_args["groups"] == {"company": "company_123"}


def test_new_client_privacy_mode_local(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test local privacy mode with new Client API"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Hello"],
        posthog_distinct_id="test-id",
        posthog_privacy_mode=True,
    )

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] is None
    assert props["$ai_output_choices"] is None


def test_new_client_privacy_mode_global(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test global privacy mode with new Client API"""
    mock_client.privacy_mode = True

    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Hello"],
        posthog_distinct_id="test-id",
    )

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] is None
    assert props["$ai_output_choices"] is None


def test_new_client_different_input_formats(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test different input formats with new Client API"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    # Test string input
    client.models.generate_content(
        model="gemini-2.0-flash", contents="Hello", posthog_distinct_id="test-id"
    )
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]

    # Test Gemini-specific format with parts array (like in the screenshot)
    mock_client.reset_mock()
    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[{"role": "user", "parts": [{"text": "hey"}]}],
        posthog_distinct_id="test-id",
    )
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] == [{"role": "user", "content": "hey"}]

    # Test multiple parts in the parts array
    mock_client.reset_mock()
    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[{"role": "user", "parts": [{"text": "Hello "}, {"text": "world"}]}],
        posthog_distinct_id="test-id",
    )
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] == [{"role": "user", "content": "Hello world"}]

    # Test list input with string
    mock_client.capture.reset_mock()
    client.models.generate_content(
        model="gemini-2.0-flash", contents=["List item"], posthog_distinct_id="test-id"
    )
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] == [{"role": "user", "content": "List item"}]


def test_new_client_model_parameters(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test model parameters with new Client API"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Hello"],
        posthog_distinct_id="test-id",
        temperature=0.7,
        max_tokens=100,
    )

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_model_parameters"]["temperature"] == 0.7
    assert props["$ai_model_parameters"]["max_tokens"] == 100


def test_new_client_default_settings(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test client with default PostHog settings"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(
        api_key="test-key",
        posthog_client=mock_client,
        posthog_distinct_id="default_user",
        posthog_properties={"team": "ai"},
        posthog_privacy_mode=False,
        posthog_groups={"company": "acme_corp"},
    )

    # Call without overriding defaults
    client.models.generate_content(model="gemini-2.0-flash", contents=["Hello"])

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "default_user"
    assert call_args["groups"] == {"company": "acme_corp"}
    assert props["team"] == "ai"


def test_new_client_override_defaults(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test overriding client defaults per call"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(
        api_key="test-key",
        posthog_client=mock_client,
        posthog_distinct_id="default_user",
        posthog_properties={"team": "ai"},
        posthog_privacy_mode=False,
        posthog_groups={"company": "acme_corp"},
    )

    # Override defaults in call
    client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Hello"],
        posthog_distinct_id="specific_user",
        posthog_properties={"feature": "chat", "urgent": True},
        posthog_privacy_mode=True,
        posthog_groups={"organization": "special_org"},
    )

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Check overrides
    assert call_args["distinct_id"] == "specific_user"
    assert call_args["groups"] == {"organization": "special_org"}
    assert props["$ai_input"] is None  # privacy mode was overridden

    # Check merged properties (defaults + call-specific)
    assert props["team"] == "ai"  # from defaults
    assert props["feature"] == "chat"  # from call
    assert props["urgent"] is True  # from call


def test_vertex_ai_parameters_passed_through(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test that Vertex AI parameters are properly passed to genai.Client"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    # Mock credentials object
    mock_credentials = MagicMock()
    mock_debug_config = MagicMock()
    mock_http_options = MagicMock()

    # Create client with Vertex AI parameters
    Client(
        vertexai=True,
        credentials=mock_credentials,
        project="test-project",
        location="us-central1",
        debug_config=mock_debug_config,
        http_options=mock_http_options,
        posthog_client=mock_client,
    )

    # Verify genai.Client was called with correct parameters
    google_genai.Client.assert_called_once_with(
        vertexai=True,
        credentials=mock_credentials,
        project="test-project",
        location="us-central1",
        debug_config=mock_debug_config,
        http_options=mock_http_options,
    )


def test_api_key_mode(mock_client, mock_google_genai_client):
    """Test API key authentication mode"""

    # Create client with just API key (traditional mode)
    Client(
        api_key="test-api-key",
        posthog_client=mock_client,
    )

    # Verify genai.Client was called with only api_key
    google_genai.Client.assert_called_once_with(api_key="test-api-key")


def test_vertex_ai_mode_with_optional_api_key(
    mock_client, mock_google_genai_client, mock_gemini_response
):
    """Test Vertex AI mode with optional API key"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    mock_credentials = MagicMock()

    # Create client with Vertex AI + API key
    Client(
        vertexai=True,
        api_key="test-api-key",
        credentials=mock_credentials,
        project="test-project",
        posthog_client=mock_client,
    )

    # Verify genai.Client was called with both Vertex AI params and API key
    google_genai.Client.assert_called_once_with(
        vertexai=True,
        api_key="test-api-key",
        credentials=mock_credentials,
        project="test-project",
    )


def test_tool_use_response(mock_client, mock_google_genai_client, mock_gemini_response):
    """Test that tools defined in config are captured in $ai_tools property"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    # Create mock tools configuration
    mock_tool = MagicMock()
    mock_tool.function_declarations = [
        MagicMock(
            name="get_current_weather",
            description="Gets the current weather for a given location.",
            parameters=MagicMock(
                type="OBJECT",
                properties={
                    "location": MagicMock(
                        type="STRING",
                        description="The city and state, e.g. San Francisco, CA",
                    )
                },
                required=["location"],
            ),
        )
    ]

    mock_config = MagicMock()
    mock_config.tools = [mock_tool]
    # Explicitly specify this config doesn't have system_instruction
    del mock_config.system_instruction

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=["hey"],
        config=mock_config,
        posthog_distinct_id="test-id",
        posthog_properties={"foo": "bar"},
    )

    assert response == mock_gemini_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.5-flash"
    assert props["$ai_input"] == [{"role": "user", "content": "hey"}]
    assert props["$ai_output_choices"] == [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Test response from Gemini"}],
        }
    ]
    assert props["$ai_input_tokens"] == 20
    assert props["$ai_output_tokens"] == 10
    assert props["$ai_http_status"] == 200
    assert props["foo"] == "bar"
    assert isinstance(props["$ai_latency"], float)
    # Verify that tools are captured in the $ai_tools property
    assert props["$ai_tools"] == [mock_tool]


def test_function_calls_in_output_choices(
    mock_client, mock_google_genai_client, mock_gemini_response_with_function_calls
):
    """Test that function calls are properly included in $ai_output_choices"""
    mock_google_genai_client.models.generate_content.return_value = (
        mock_gemini_response_with_function_calls
    )

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=["What's the weather in San Francisco?"],
        posthog_distinct_id="test-id",
    )

    assert response == mock_gemini_response_with_function_calls
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.5-flash"
    assert props["$ai_output_choices"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll check the weather for you."},
                {"type": "text", "text": " Let me look that up."},
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_weather",
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


def test_function_calls_only_no_content(
    mock_client, mock_google_genai_client, mock_gemini_response_function_calls_only
):
    """Test function calls without text content in $ai_output_choices"""
    mock_google_genai_client.models.generate_content.return_value = (
        mock_gemini_response_function_calls_only
    )

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=["Get weather for New York"],
        posthog_distinct_id="test-id",
    )

    assert response == mock_gemini_response_function_calls_only
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert call_args["event"] == "$ai_generation"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.5-flash"
    assert props["$ai_output_choices"] == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_weather",
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


def test_cache_and_reasoning_tokens(mock_client, mock_google_genai_client):
    """Test that cache and reasoning tokens are properly extracted"""
    # Create a mock response with cache and reasoning tokens
    mock_response = MagicMock()
    mock_response.text = "Test response with cache"

    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 100
    mock_usage.candidates_token_count = 50
    mock_usage.cached_content_token_count = 30  # Cache tokens
    mock_usage.thoughts_token_count = 10  # Reasoning tokens
    mock_response.usage_metadata = mock_usage

    # Mock candidates
    mock_candidate = MagicMock()
    mock_candidate.text = "Test response with cache"
    mock_response.candidates = [mock_candidate]

    mock_google_genai_client.models.generate_content.return_value = mock_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content(
        model="gemini-2.5-pro",
        contents="Test with cache",
        posthog_distinct_id="test-id",
    )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Check that all token types are present
    assert props["$ai_input_tokens"] == 100
    assert props["$ai_output_tokens"] == 50
    assert props["$ai_cache_read_input_tokens"] == 30
    assert props["$ai_reasoning_tokens"] == 10


def test_streaming_cache_and_reasoning_tokens(mock_client, mock_google_genai_client):
    """Test that cache and reasoning tokens are properly extracted in streaming"""
    # Create mock chunks with cache and reasoning tokens
    chunk1 = MagicMock()
    chunk1.text = "Hello "
    chunk1_usage = MagicMock()
    chunk1_usage.prompt_token_count = 100
    chunk1_usage.candidates_token_count = 5
    chunk1_usage.cached_content_token_count = 30  # Cache tokens
    chunk1_usage.thoughts_token_count = 0
    chunk1.usage_metadata = chunk1_usage

    chunk2 = MagicMock()
    chunk2.text = "world!"
    chunk2_usage = MagicMock()
    chunk2_usage.prompt_token_count = 100
    chunk2_usage.candidates_token_count = 10
    chunk2_usage.cached_content_token_count = 30  # Same cache tokens
    chunk2_usage.thoughts_token_count = 5  # Reasoning tokens
    chunk2.usage_metadata = chunk2_usage

    mock_stream = iter([chunk1, chunk2])
    mock_google_genai_client.models.generate_content_stream.return_value = mock_stream

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content_stream(
        model="gemini-2.5-pro",
        contents="Test streaming with cache",
        posthog_distinct_id="test-id",
    )

    # Consume the stream
    result = list(response)
    assert len(result) == 2

    # Check PostHog capture was called
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Check that all token types are present (should use final chunk's usage)
    assert props["$ai_input_tokens"] == 100
    assert props["$ai_output_tokens"] == 10
    assert props["$ai_cache_read_input_tokens"] == 30
    assert props["$ai_reasoning_tokens"] == 5


def test_web_search_grounding(mock_client, mock_google_genai_client):
    """Test web search detection via grounding_metadata."""

    # Create mock response with grounding metadata
    mock_response = MagicMock()

    # Mock usage metadata
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 60
    mock_usage.candidates_token_count = 40
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    # Mock grounding metadata
    mock_grounding_chunk = MagicMock()
    mock_grounding_chunk.uri = "https://example.com"

    mock_grounding_metadata = MagicMock()
    mock_grounding_metadata.grounding_chunks = [mock_grounding_chunk]

    # Mock text part
    mock_text_part = MagicMock()
    mock_text_part.text = "According to search results..."
    type(mock_text_part).text = mock_text_part.text

    # Mock content with parts
    mock_content = MagicMock()
    mock_content.parts = [mock_text_part]

    # Mock candidate with grounding metadata
    mock_candidate = MagicMock()
    mock_candidate.content = mock_content
    mock_candidate.grounding_metadata = mock_grounding_metadata
    type(mock_candidate).grounding_metadata = mock_candidate.grounding_metadata

    mock_response.candidates = [mock_candidate]
    mock_response.text = "According to search results..."

    # Mock the generate_content method
    mock_google_genai_client.models.generate_content.return_value = mock_response

    client = Client(api_key="test-key", posthog_client=mock_client)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="What's the latest news?",
        posthog_distinct_id="test-id",
    )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Verify web search count is detected (binary for grounding)
    assert props["$ai_web_search_count"] == 1
    assert props["$ai_input_tokens"] == 60
    assert props["$ai_output_tokens"] == 40


def test_streaming_with_web_search(mock_client, mock_google_genai_client):
    """Test that web search count is properly captured in streaming mode."""

    def mock_streaming_response():
        # Create chunk 1 with grounding metadata
        mock_chunk1 = MagicMock()
        mock_chunk1.text = "According to "

        mock_usage1 = MagicMock()
        mock_usage1.prompt_token_count = 30
        mock_usage1.candidates_token_count = 5
        mock_usage1.cached_content_token_count = 0
        mock_usage1.thoughts_token_count = 0
        mock_chunk1.usage_metadata = mock_usage1

        # Add grounding metadata to first chunk
        mock_grounding_chunk = MagicMock()
        mock_grounding_chunk.uri = "https://example.com"

        mock_grounding_metadata = MagicMock()
        mock_grounding_metadata.grounding_chunks = [mock_grounding_chunk]

        mock_candidate1 = MagicMock()
        mock_candidate1.grounding_metadata = mock_grounding_metadata
        type(mock_candidate1).grounding_metadata = mock_candidate1.grounding_metadata

        mock_chunk1.candidates = [mock_candidate1]

        # Create chunk 2
        mock_chunk2 = MagicMock()
        mock_chunk2.text = "search results..."

        mock_usage2 = MagicMock()
        mock_usage2.prompt_token_count = 30
        mock_usage2.candidates_token_count = 15
        mock_usage2.cached_content_token_count = 0
        mock_usage2.thoughts_token_count = 0
        mock_chunk2.usage_metadata = mock_usage2

        mock_candidate2 = MagicMock()
        mock_chunk2.candidates = [mock_candidate2]

        yield mock_chunk1
        yield mock_chunk2

    # Mock the generate_content_stream method
    mock_google_genai_client.models.generate_content_stream.return_value = (
        mock_streaming_response()
    )

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content_stream(
        model="gemini-2.5-flash",
        contents="What's the latest news?",
        posthog_distinct_id="test-id",
    )

    chunks = list(response)
    assert len(chunks) == 2
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Verify web search count is detected (binary for grounding)
    assert props["$ai_web_search_count"] == 1
    assert props["$ai_input_tokens"] == 30
    assert props["$ai_output_tokens"] == 15


def test_empty_grounding_metadata_no_web_search(mock_client, mock_google_genai_client):
    """Test that empty grounding_metadata (all null fields) does not count as web search."""

    # Create mock response with empty grounding metadata (all null fields)
    mock_response = MagicMock()

    # Mock usage metadata
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 10
    mock_usage.candidates_token_count = 10
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    # Mock empty grounding metadata (all fields are None)
    mock_grounding_metadata = MagicMock()
    mock_grounding_metadata.web_search_queries = None
    mock_grounding_metadata.grounding_chunks = None
    mock_grounding_metadata.grounding_supports = None
    mock_grounding_metadata.retrieval_metadata = None
    mock_grounding_metadata.retrieval_queries = None
    mock_grounding_metadata.search_entry_point = None

    # Mock text part
    mock_text_part = MagicMock()
    mock_text_part.text = "Hey there! How can I help you today?"
    type(mock_text_part).text = mock_text_part.text

    # Mock content with parts
    mock_content = MagicMock()
    mock_content.parts = [mock_text_part]

    # Mock candidate with empty grounding metadata
    mock_candidate = MagicMock()
    mock_candidate.content = mock_content
    mock_candidate.grounding_metadata = mock_grounding_metadata
    type(mock_candidate).grounding_metadata = mock_candidate.grounding_metadata

    mock_response.candidates = [mock_candidate]
    mock_response.text = "Hey there! How can I help you today?"

    # Mock the generate_content method
    mock_google_genai_client.models.generate_content.return_value = mock_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="Hello",
        posthog_distinct_id="test-id",
    )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Verify web search count is 0 (not present in properties when 0)
    assert "$ai_web_search_count" not in props
    assert props["$ai_input_tokens"] == 10
    assert props["$ai_output_tokens"] == 10


def test_empty_array_grounding_metadata_no_web_search(
    mock_client, mock_google_genai_client
):
    """Test that grounding_metadata with empty arrays does not count as web search."""

    # Create mock response with grounding metadata having empty arrays
    mock_response = MagicMock()

    # Mock usage metadata
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 15
    mock_usage.candidates_token_count = 12
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    # Mock grounding metadata with empty arrays
    mock_grounding_metadata = MagicMock()
    mock_grounding_metadata.web_search_queries = []
    mock_grounding_metadata.grounding_chunks = []
    mock_grounding_metadata.grounding_supports = []

    # Mock text part
    mock_text_part = MagicMock()
    mock_text_part.text = "I can help with that."
    type(mock_text_part).text = mock_text_part.text

    # Mock content with parts
    mock_content = MagicMock()
    mock_content.parts = [mock_text_part]

    # Mock candidate with grounding metadata containing empty arrays
    mock_candidate = MagicMock()
    mock_candidate.content = mock_content
    mock_candidate.grounding_metadata = mock_grounding_metadata
    type(mock_candidate).grounding_metadata = mock_candidate.grounding_metadata

    mock_response.candidates = [mock_candidate]
    mock_response.text = "I can help with that."

    # Mock the generate_content method
    mock_google_genai_client.models.generate_content.return_value = mock_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="What can you do?",
        posthog_distinct_id="test-id",
    )

    assert response == mock_response
    assert mock_client.capture.call_count == 1

    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    # Verify web search count is 0 (not present in properties when 0)
    assert "$ai_web_search_count" not in props
    assert props["$ai_input_tokens"] == 15
    assert props["$ai_output_tokens"] == 12
