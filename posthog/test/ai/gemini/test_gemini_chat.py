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
    mock_response.text = "Chat response"

    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 10
    mock_usage.candidates_token_count = 20
    mock_usage.cached_content_token_count = 0
    mock_usage.thoughts_token_count = 0
    mock_response.usage_metadata = mock_usage

    mock_candidate = MagicMock()
    mock_candidate.text = "Chat response"
    mock_content = MagicMock()
    mock_part = MagicMock()
    mock_part.text = "Chat response"
    mock_content.parts = [mock_part]
    mock_candidate.content = mock_content
    mock_response.candidates = [mock_candidate]

    return mock_response


@pytest.fixture
def mock_google_genai_client():
    with patch.object(google_genai, "Client") as mock_client_class:
        mock_client_instance = MagicMock()
        mock_models = MagicMock()
        mock_client_instance.models = mock_models

        # Mock chats
        mock_chats = MagicMock()
        mock_client_instance.chats = mock_chats

        mock_client_class.return_value = mock_client_instance
        yield mock_client_instance


def test_chat_send_message(mock_client, mock_google_genai_client, mock_gemini_response):
    """Test sending a message in a chat session"""

    # Mock chat session
    mock_chat = MagicMock()
    mock_chat.send_message.return_value = mock_gemini_response
    mock_google_genai_client.chats.create.return_value = mock_chat

    client = Client(api_key="test-key", posthog_client=mock_client)

    # Create chat
    chat = client.chats.create(model="gemini-2.0-flash", posthog_distinct_id="test-id")

    # Send message
    response = chat.send_message(message="Hello chat")

    assert response == mock_gemini_response

    # Verify capture
    assert mock_client.capture.call_count == 1
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert call_args["distinct_id"] == "test-id"
    assert props["$ai_provider"] == "gemini"
    assert props["$ai_model"] == "gemini-2.0-flash"
    assert props["$ai_input"] == [{"role": "user", "content": "Hello chat"}]
    assert props["$ai_input_tokens"] == 10
    assert props["$ai_output_tokens"] == 20


def test_chat_send_message_stream(mock_client, mock_google_genai_client):
    """Test streaming message in a chat session"""

    # Mock streaming response
    def mock_streaming_response():
        mock_chunk = MagicMock()
        mock_chunk.text = "Streamed response"
        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 5
        mock_usage.candidates_token_count = 10
        mock_usage.cached_content_token_count = 0
        mock_usage.thoughts_token_count = 0
        mock_chunk.usage_metadata = mock_usage

        yield mock_chunk

    mock_chat = MagicMock()
    mock_chat.send_message_stream.return_value = mock_streaming_response()
    mock_google_genai_client.chats.create.return_value = mock_chat

    client = Client(api_key="test-key", posthog_client=mock_client)

    chat = client.chats.create(model="gemini-2.0-flash")

    response = chat.send_message_stream(message="Stream me")

    chunks = list(response)
    assert len(chunks) == 1
    assert chunks[0].text == "Streamed response"

    assert mock_client.capture.call_count == 1
    call_args = mock_client.capture.call_args[1]
    props = call_args["properties"]

    assert props["$ai_model"] == "gemini-2.0-flash"
    assert props["$ai_input_tokens"] == 5
    assert props["$ai_output_tokens"] == 10
