import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from posthog.ai.gemini.gemini_async import AsyncClient, AsyncAio
from posthog.client import Client as PostHogClient


@pytest.fixture
def mock_posthog_client():
    """Mock PostHog client for testing."""
    client = MagicMock(spec=PostHogClient)
    client.capture = MagicMock()
    client.privacy_mode = False  # Add privacy_mode attribute
    return client


@pytest.fixture
def mock_genai_client():
    """Mock the underlying genai.Client."""
    with patch("posthog.ai.gemini.gemini_async.genai.Client") as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock the aio.models interface
        mock_client.aio = MagicMock()
        mock_client.aio.models = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock()
        mock_client.aio.models.generate_content_stream = AsyncMock()

        yield mock_client


@pytest.fixture
def async_client(mock_posthog_client, mock_genai_client):
    """Create an AsyncClient instance for testing."""
    return AsyncClient(
        api_key="test-api-key",
        posthog_client=mock_posthog_client,
        posthog_distinct_id="test-user",
        posthog_properties={"test": "property"},
    )


class TestAsyncClient:
    """Test the AsyncClient class."""

    def test_init_with_api_key(self, mock_posthog_client):
        """Test AsyncClient initialization with API key."""
        with patch("posthog.ai.gemini.gemini_async.genai.Client") as mock_client_class:
            client = AsyncClient(api_key="test-key", posthog_client=mock_posthog_client)

            assert client._ph_client == mock_posthog_client
            assert isinstance(client.aio, AsyncAio)
            mock_client_class.assert_called_once()

    def test_init_without_api_key_raises_error(self, mock_posthog_client):
        """Test that AsyncClient raises error when no API key is provided."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="API key must be provided"):
                AsyncClient(posthog_client=mock_posthog_client)

    def test_init_with_vertex_ai(self, mock_posthog_client):
        """Test AsyncClient initialization with Vertex AI."""
        with patch("posthog.ai.gemini.gemini_async.genai.Client") as mock_client_class:
            client = AsyncClient(
                vertexai=True,
                project="test-project",
                location="us-central1",
                posthog_client=mock_posthog_client,
            )

            assert client._ph_client == mock_posthog_client
            mock_client_class.assert_called_once_with(
                vertexai=True, project="test-project", location="us-central1"
            )

    def test_init_without_posthog_client_raises_error(self):
        """Test that AsyncClient raises error when PostHog client is None."""
        with patch("posthog.ai.gemini.gemini_async.setup", return_value=None):
            with pytest.raises(ValueError, match="posthog_client is required"):
                AsyncClient(api_key="test-key")


class TestAsyncModels:
    """Test the AsyncModels class."""

    @pytest.mark.asyncio
    async def test_generate_content_basic(self, async_client, mock_genai_client):
        """Test basic async content generation."""
        # Mock response
        mock_response = MagicMock()
        mock_response.text = "Generated content"
        mock_genai_client.aio.models.generate_content.return_value = mock_response

        # Mock the async tracking function
        with patch(
            "posthog.ai.gemini.gemini_async.call_llm_and_track_usage_async"
        ) as mock_track:
            mock_track.return_value = mock_response

            response = await async_client.aio.models.generate_content(
                model="gemini-2.0-flash", contents=["Hello world"]
            )

            assert response == mock_response
            mock_track.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_content_with_posthog_params(
        self, async_client, mock_genai_client
    ):
        """Test async content generation with PostHog parameters."""
        mock_response = MagicMock()
        mock_response.text = "Generated content"

        with patch(
            "posthog.ai.gemini.gemini_async.call_llm_and_track_usage_async"
        ) as mock_track:
            mock_track.return_value = mock_response

            response = await async_client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=["Hello world"],
                posthog_distinct_id="custom-user",
                posthog_trace_id="custom-trace",
                posthog_properties={"custom": "property"},
                posthog_privacy_mode=True,
                posthog_groups={"team": "ai"},
            )

            assert response == mock_response
            mock_track.assert_called_once()

            # Verify the call arguments
            call_args = mock_track.call_args
            assert call_args[0][0] == "custom-user"  # distinct_id
            assert call_args[0][2] == "gemini"  # provider
            assert call_args[0][3] == "custom-trace"  # trace_id
            assert call_args[0][4]["custom"] == "property"  # properties
            assert call_args[0][5] is True  # privacy_mode
            assert call_args[0][6] == {"team": "ai"}  # groups

    @pytest.mark.asyncio
    async def test_generate_content_stream_basic(self, async_client, mock_genai_client):
        """Test basic async streaming content generation."""

        # Create a proper async generator mock
        class AsyncGeneratorMock:
            def __init__(self, items):
                self.items = items
                self.index = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.index >= len(self.items):
                    raise StopAsyncIteration
                item = self.items[self.index]
                self.index += 1
                return item

        # Mock streaming response chunks with proper usage_metadata
        chunk1 = MagicMock(text="chunk1")
        chunk1.usage_metadata = MagicMock()
        chunk1.usage_metadata.prompt_token_count = 5
        chunk1.usage_metadata.candidates_token_count = 10
        chunk1.usage_metadata.cached_content_token_count = 0
        chunk1.usage_metadata.thoughts_token_count = 0

        chunk2 = MagicMock(text="chunk2")
        chunk2.usage_metadata = MagicMock()
        chunk2.usage_metadata.prompt_token_count = 5
        chunk2.usage_metadata.candidates_token_count = 15
        chunk2.usage_metadata.cached_content_token_count = 0
        chunk2.usage_metadata.thoughts_token_count = 0

        mock_stream = AsyncGeneratorMock([chunk1, chunk2])

        # Mock the underlying streaming call to return the async generator
        mock_genai_client.aio.models.generate_content_stream.return_value = mock_stream

        # Mock the capture_streaming_event to avoid the privacy_mode issue
        with patch("posthog.ai.gemini.gemini_async.capture_streaming_event"):
            response_generator = async_client.aio.models.generate_content_stream(
                model="gemini-2.0-flash", contents=["Hello world"]
            )

            # Collect all chunks
            chunks = []
            async for chunk in response_generator:
                chunks.append(chunk)

            assert len(chunks) == 2
            assert chunks[0].text == "chunk1"
            assert chunks[1].text == "chunk2"

    @pytest.mark.asyncio
    async def test_merge_posthog_params(self, async_client):
        """Test parameter merging logic."""
        models = async_client.aio.models

        # Test with call-level parameters overriding defaults
        distinct_id, trace_id, properties, privacy_mode, groups = (
            models._merge_posthog_params(
                call_distinct_id="call-user",
                call_trace_id="call-trace",
                call_properties={"call": "prop"},
                call_privacy_mode=True,
                call_groups={"call": "group"},
            )
        )

        assert distinct_id == "call-user"
        assert trace_id == "call-trace"
        assert properties["test"] == "property"  # Default property
        assert properties["call"] == "prop"  # Call property
        assert privacy_mode is True
        assert groups == {"call": "group"}

    @pytest.mark.asyncio
    async def test_merge_posthog_params_defaults(self, async_client):
        """Test parameter merging with defaults."""
        models = async_client.aio.models

        # Test with None values falling back to defaults
        distinct_id, trace_id, properties, privacy_mode, groups = (
            models._merge_posthog_params(
                call_distinct_id=None,
                call_trace_id=None,
                call_properties=None,
                call_privacy_mode=None,
                call_groups=None,
            )
        )

        assert distinct_id == "test-user"  # Default from client
        assert trace_id is not None  # Auto-generated UUID
        assert properties == {"test": "property"}  # Default properties
        assert privacy_mode is False  # Default privacy mode
        assert groups is None  # Default groups

    def test_format_input(self, async_client):
        """Test input formatting."""
        models = async_client.aio.models

        with patch("posthog.ai.gemini.gemini_async.format_gemini_input") as mock_format:
            mock_format.return_value = "formatted input"

            result = models._format_input(["test content"])

            assert result == "formatted input"
            mock_format.assert_called_once_with(["test content"])

    @pytest.mark.asyncio
    async def test_capture_streaming_event(self, async_client, mock_posthog_client):
        """Test streaming event capture."""
        models = async_client.aio.models

        with patch(
            "posthog.ai.gemini.gemini_async.capture_streaming_event"
        ) as mock_capture:
            with patch(
                "posthog.ai.gemini.gemini_async.sanitize_gemini"
            ) as mock_sanitize:
                with patch(
                    "posthog.ai.gemini.gemini_async.format_gemini_input"
                ) as mock_format_input:
                    with patch(
                        "posthog.ai.gemini.gemini_async.format_gemini_streaming_output"
                    ) as mock_format_output:
                        mock_format_input.return_value = "formatted input"
                        mock_sanitize.return_value = "sanitized input"
                        mock_format_output.return_value = "formatted output"

                        from posthog.ai.types import TokenUsage

                        usage_stats = TokenUsage(input_tokens=10, output_tokens=20)

                        await models._capture_streaming_event(
                            model="gemini-2.0-flash",
                            contents=["test"],
                            distinct_id="test-user",
                            trace_id="test-trace",
                            properties={"test": "prop"},
                            privacy_mode=False,
                            groups={"team": "ai"},
                            kwargs={"temperature": 0.7},
                            usage_stats=usage_stats,
                            latency=1.5,
                            output=["output"],
                        )

                        # Verify the capture function was called
                        mock_capture.assert_called_once()
                        # Verify the call was made with the PostHog client and event data
                        call_args = mock_capture.call_args
                        assert (
                            call_args[0][0] == mock_posthog_client
                        )  # First arg is the client
                        # The second arg is the event data object - just verify it exists
                        assert call_args[0][1] is not None


class TestAsyncIntegration:
    """Integration tests for the async Gemini client."""

    @pytest.mark.asyncio
    async def test_full_async_workflow(self, mock_posthog_client):
        """Test a complete async workflow."""
        with patch("posthog.ai.gemini.gemini_async.genai.Client") as mock_client_class:
            # Setup mock client
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            # Mock async response
            mock_response = MagicMock()
            mock_response.text = "Hello! How can I help you?"
            mock_client.aio.models.generate_content = AsyncMock(
                return_value=mock_response
            )

            # Mock tracking function
            with patch(
                "posthog.ai.gemini.gemini_async.call_llm_and_track_usage_async"
            ) as mock_track:
                mock_track.return_value = mock_response

                # Create client and make request
                client = AsyncClient(
                    api_key="test-key",
                    posthog_client=mock_posthog_client,
                    posthog_distinct_id="integration-test-user",
                )

                response = await client.aio.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=["Hello world"],
                    posthog_properties={"test_type": "integration"},
                )

                # Verify response
                assert response.text == "Hello! How can I help you?"

                # Verify tracking was called
                mock_track.assert_called_once()
                call_args = mock_track.call_args
                assert call_args[0][0] == "integration-test-user"  # distinct_id
                assert call_args[0][2] == "gemini"  # provider

    @pytest.mark.asyncio
    async def test_error_handling(self, mock_posthog_client):
        """Test error handling in async operations."""
        with patch("posthog.ai.gemini.gemini_async.genai.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            # Mock an exception
            mock_client.aio.models.generate_content = AsyncMock(
                side_effect=Exception("API Error")
            )

            with patch(
                "posthog.ai.gemini.gemini_async.call_llm_and_track_usage_async"
            ) as mock_track:
                mock_track.side_effect = Exception("API Error")

                client = AsyncClient(
                    api_key="test-key", posthog_client=mock_posthog_client
                )

                with pytest.raises(Exception, match="API Error"):
                    await client.aio.models.generate_content(
                        model="gemini-2.0-flash", contents=["Hello world"]
                    )


if __name__ == "__main__":
    # Run the tests
    pytest.main([__file__, "-v"])
