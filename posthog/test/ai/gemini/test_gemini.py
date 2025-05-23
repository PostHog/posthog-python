from unittest.mock import MagicMock, patch

import pytest

try:
    from google import genai as google_genai

    from posthog.ai.gemini import Client

    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

pytestmark = pytest.mark.skipif(not GEMINI_AVAILABLE, reason="Google Gemini package is not available")


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


def test_new_client_basic_generation(mock_client, mock_google_genai_client, mock_gemini_response):
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


def test_new_client_streaming_with_generate_content_stream(mock_client, mock_google_genai_client):
    """Test the new generate_content_stream method"""

    def mock_streaming_response():
        mock_chunk1 = MagicMock()
        mock_chunk1.text = "Hello "
        mock_usage1 = MagicMock()
        mock_usage1.prompt_token_count = 10
        mock_usage1.candidates_token_count = 5
        mock_chunk1.usage_metadata = mock_usage1

        mock_chunk2 = MagicMock()
        mock_chunk2.text = "world!"
        mock_usage2 = MagicMock()
        mock_usage2.prompt_token_count = 10
        mock_usage2.candidates_token_count = 10
        mock_chunk2.usage_metadata = mock_usage2

        yield mock_chunk1
        yield mock_chunk2

    # Mock the generate_content_stream method
    mock_google_genai_client.models.generate_content_stream.return_value = mock_streaming_response()

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


def test_new_client_privacy_mode_local(mock_client, mock_google_genai_client, mock_gemini_response):
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


def test_new_client_privacy_mode_global(mock_client, mock_google_genai_client, mock_gemini_response):
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


def test_new_client_different_input_formats(mock_client, mock_google_genai_client, mock_gemini_response):
    """Test different input formats with new Client API"""
    mock_google_genai_client.models.generate_content.return_value = mock_gemini_response

    client = Client(api_key="test-key", posthog_client=mock_client)

    # Test string input
    client.models.generate_content(model="gemini-2.0-flash", contents="Hello", posthog_distinct_id="test-id")
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] == [{"role": "user", "content": "Hello"}]

    # Test list input
    mock_client.capture.reset_mock()
    mock_part = MagicMock()
    mock_part.text = "List item"
    client.models.generate_content(model="gemini-2.0-flash", contents=[mock_part], posthog_distinct_id="test-id")
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]
    assert props["$ai_input"] == [{"role": "user", "content": "List item"}]


def test_new_client_model_parameters(mock_client, mock_google_genai_client, mock_gemini_response):
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


def test_new_client_default_settings(mock_client, mock_google_genai_client, mock_gemini_response):
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


def test_new_client_override_defaults(mock_client, mock_google_genai_client, mock_gemini_response):
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
