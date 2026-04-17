import unittest
import warnings
from unittest.mock import MagicMock, patch

from parameterized import parameterized

from posthog.ai.prompts import PromptResult, Prompts


class MockResponse:
    """Mock HTTP response for testing."""

    def __init__(self, json_data=None, status_code=200, ok=True):
        self._json_data = json_data
        self.status_code = status_code
        self.ok = ok

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON data")
        return self._json_data


class TestPrompts(unittest.TestCase):
    """Tests for the Prompts class."""

    mock_prompt_response = {
        "id": 1,
        "name": "test-prompt",
        "prompt": "Hello, {{name}}! You are a helpful assistant for {{company}}.",
        "version": 1,
        "created_by": "user@example.com",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "deleted": False,
    }

    def create_mock_posthog(
        self,
        personal_api_key="phx_test_key",
        project_api_key="phc_test_key",
        host="https://us.posthog.com",
    ):
        """Create a mock PostHog client."""
        mock = MagicMock()
        mock.personal_api_key = personal_api_key
        mock.api_key = project_api_key
        mock.raw_host = host
        return mock


class TestPromptsGet(TestPrompts):
    """Tests for the Prompts.get() method."""

    @patch("posthog.ai.prompts._get_session")
    def test_successfully_fetch_a_prompt(self, mock_get_session):
        """Should successfully fetch a prompt."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.get("test-prompt")

        self.assertEqual(result, self.mock_prompt_response["prompt"])
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/?token=phc_test_key",
        )
        self.assertIn("Authorization", call_args[1]["headers"])
        self.assertEqual(
            call_args[1]["headers"]["Authorization"], "Bearer phx_test_key"
        )

    @patch("posthog.ai.prompts._get_session")
    def test_successfully_fetch_a_specific_prompt_version(self, mock_get_session):
        """Should successfully fetch a specific prompt version."""
        mock_get = mock_get_session.return_value.get
        versioned_prompt_response = {
            **self.mock_prompt_response,
            "prompt": "Prompt version 1",
            "version": 1,
        }
        mock_get.return_value = MockResponse(json_data=versioned_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.get("test-prompt", version=1)

        self.assertEqual(result, versioned_prompt_response["prompt"])
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/?token=phc_test_key&version=1",
        )

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_return_cached_prompt_when_fresh(self, mock_time, mock_get_session):
        """Should return cached prompt when fresh (no API call)."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call - fetches from API
        result1 = prompts.get("test-prompt", cache_ttl_seconds=300)
        self.assertEqual(result1, self.mock_prompt_response["prompt"])
        self.assertEqual(mock_get.call_count, 1)

        # Advance time by 60 seconds (still within TTL)
        mock_time.return_value = 1060.0

        # Second call - should use cache
        result2 = prompts.get("test-prompt", cache_ttl_seconds=300)
        self.assertEqual(result2, self.mock_prompt_response["prompt"])
        self.assertEqual(mock_get.call_count, 1)  # No additional fetch

    @patch("posthog.ai.prompts._get_session")
    def test_cache_latest_and_versioned_prompts_separately(self, mock_get_session):
        """Should cache latest and historical prompt versions separately."""
        mock_get = mock_get_session.return_value.get
        latest_prompt_response = {
            **self.mock_prompt_response,
            "prompt": "Latest prompt",
            "version": 2,
        }
        versioned_prompt_response = {
            **self.mock_prompt_response,
            "prompt": "Prompt version 1",
            "version": 1,
        }

        mock_get.side_effect = [
            MockResponse(json_data=latest_prompt_response),
            MockResponse(json_data=versioned_prompt_response),
        ]

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        self.assertEqual(prompts.get("test-prompt"), latest_prompt_response["prompt"])
        self.assertEqual(
            prompts.get("test-prompt", version=1),
            versioned_prompt_response["prompt"],
        )
        self.assertEqual(prompts.get("test-prompt"), latest_prompt_response["prompt"])
        self.assertEqual(
            prompts.get("test-prompt", version=1),
            versioned_prompt_response["prompt"],
        )
        self.assertEqual(mock_get.call_count, 2)

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_refetch_when_cache_is_stale(self, mock_time, mock_get_session):
        """Should refetch when cache is stale."""
        mock_get = mock_get_session.return_value.get
        updated_prompt_response = {
            **self.mock_prompt_response,
            "prompt": "Updated prompt: Hello, {{name}}!",
        }

        mock_get.side_effect = [
            MockResponse(json_data=self.mock_prompt_response),
            MockResponse(json_data=updated_prompt_response),
        ]
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call - fetches from API
        result1 = prompts.get("test-prompt", cache_ttl_seconds=60)
        self.assertEqual(result1, self.mock_prompt_response["prompt"])
        self.assertEqual(mock_get.call_count, 1)

        # Advance time past TTL
        mock_time.return_value = 1061.0

        # Second call - should refetch
        result2 = prompts.get("test-prompt", cache_ttl_seconds=60)
        self.assertEqual(result2, updated_prompt_response["prompt"])
        self.assertEqual(mock_get.call_count, 2)

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    @patch("posthog.ai.prompts.log")
    def test_use_stale_cache_on_fetch_failure_with_warning(
        self, mock_log, mock_time, mock_get_session
    ):
        """Should use stale cache on fetch failure with warning."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = [
            MockResponse(json_data=self.mock_prompt_response),
            Exception("Network error"),
        ]
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call - populates cache
        result1 = prompts.get("test-prompt", cache_ttl_seconds=60)
        self.assertEqual(result1, self.mock_prompt_response["prompt"])

        # Advance time past TTL
        mock_time.return_value = 1061.0

        # Second call - should use stale cache
        result2 = prompts.get("test-prompt", cache_ttl_seconds=60)
        self.assertEqual(result2, self.mock_prompt_response["prompt"])

        # Check warning was logged
        mock_log.warning.assert_called()
        warning_call = mock_log.warning.call_args
        self.assertIn("using stale cache", warning_call[0][0])

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.log")
    def test_use_fallback_when_no_cache_and_fetch_fails_with_warning(
        self, mock_log, mock_get_session
    ):
        """Should use fallback when no cache and fetch fails with warning."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        fallback = "Default system prompt."
        result = prompts.get("test-prompt", fallback=fallback)

        self.assertEqual(result, fallback)

        # Check warning was logged
        mock_log.warning.assert_called()
        warning_call = mock_log.warning.call_args
        self.assertIn("using fallback", warning_call[0][0])

    @patch("posthog.ai.prompts._get_session")
    def test_throw_when_no_cache_no_fallback_and_fetch_fails(self, mock_get_session):
        """Should throw when no cache, no fallback, and fetch fails."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("test-prompt")

        self.assertIn("Network error", str(context.exception))

    @parameterized.expand(
        [
            ("latest", {}, 'Prompt "nonexistent-prompt" not found'),
            (
                "versioned",
                {"version": 3},
                'Prompt "nonexistent-prompt" version 3 not found',
            ),
        ]
    )
    @patch("posthog.ai.prompts._get_session")
    def test_handle_404_response(
        self, _scenario, get_kwargs, expected_message, mock_get_session
    ):
        """Should handle 404 responses for latest and versioned prompts."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(status_code=404, ok=False)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("nonexistent-prompt", **get_kwargs)

        self.assertIn(expected_message, str(context.exception))

    @patch("posthog.ai.prompts._get_session")
    def test_handle_403_response(self, mock_get_session):
        """Should handle 403 response."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(status_code=403, ok=False)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("restricted-prompt")

        self.assertIn(
            'Access denied for prompt "restricted-prompt"', str(context.exception)
        )

    def test_throw_when_no_personal_api_key_configured(self):
        """Should throw when no personal_api_key is configured."""
        posthog = self.create_mock_posthog(personal_api_key=None)
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("test-prompt")

        self.assertIn(
            "personal_api_key is required to fetch prompts", str(context.exception)
        )

    def test_throw_when_no_project_api_key_configured(self):
        """Should throw when no project_api_key is configured."""
        posthog = self.create_mock_posthog(project_api_key=None)
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("test-prompt")

        self.assertIn(
            "project_api_key is required to fetch prompts", str(context.exception)
        )

    @patch("posthog.ai.prompts._get_session")
    def test_throw_when_api_returns_invalid_response_format(self, mock_get_session):
        """Should throw when API returns invalid response format."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data={"invalid": "response"})

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("test-prompt")

        self.assertIn("Invalid response format", str(context.exception))

    @patch("posthog.ai.prompts._get_session")
    def test_use_custom_host_from_posthog_options(self, mock_get_session):
        """Should use custom host from PostHog options."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog(host="https://eu.posthog.com")
        prompts = Prompts(posthog)

        prompts.get("test-prompt")

        call_args = mock_get.call_args
        self.assertTrue(
            call_args[0][0].startswith(
                "https://eu.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/?token=phc_test_key"
            ),
            f"Expected URL to start with 'https://eu.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/?token=phc_test_key', got {call_args[0][0]}",
        )

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_use_default_cache_ttl_5_minutes(self, mock_time, mock_get_session):
        """Should use default cache TTL (5 minutes) when not specified."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 1)

        # Advance time by 4 minutes (within default 5-minute TTL)
        mock_time.return_value = 1000.0 + (4 * 60)

        # Second call - should use cache
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 1)

        # Advance time past 5-minute TTL
        mock_time.return_value = 1000.0 + (6 * 60)

        # Third call - should refetch
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 2)

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_use_custom_default_cache_ttl_from_constructor(
        self, mock_time, mock_get_session
    ):
        """Should use custom default cache TTL from constructor."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog, default_cache_ttl_seconds=60)

        # First call
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 1)

        # Advance time past custom TTL
        mock_time.return_value = 1061.0

        # Second call - should refetch
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 2)

    @patch("posthog.ai.prompts._get_session")
    def test_url_encode_prompt_names_with_special_characters(self, mock_get_session):
        """Should URL-encode prompt names with special characters."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        prompts.get("prompt with spaces/and/slashes")

        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/prompt%20with%20spaces%2Fand%2Fslashes/?token=phc_test_key",
        )

    @patch("posthog.ai.prompts._get_session")
    def test_work_with_direct_options_no_posthog_client(self, mock_get_session):
        """Should work with direct options (no PostHog client)."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        prompts = Prompts(
            personal_api_key="phx_direct_key", project_api_key="phc_direct_key"
        )

        result = prompts.get("test-prompt")

        self.assertEqual(result, self.mock_prompt_response["prompt"])
        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/?token=phc_direct_key",
        )
        self.assertEqual(
            call_args[1]["headers"]["Authorization"], "Bearer phx_direct_key"
        )

    @patch("posthog.ai.prompts._get_session")
    def test_use_custom_host_from_direct_options(self, mock_get_session):
        """Should use custom host from direct options."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        prompts = Prompts(
            personal_api_key="phx_direct_key",
            project_api_key="phc_direct_key",
            host="https://eu.posthog.com",
        )

        prompts.get("test-prompt")

        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://eu.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/?token=phc_direct_key",
        )

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_use_custom_default_cache_ttl_from_direct_options(
        self, mock_time, mock_get_session
    ):
        """Should use custom default cache TTL from direct options."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)
        mock_time.return_value = 1000.0

        prompts = Prompts(
            personal_api_key="phx_direct_key",
            project_api_key="phc_direct_key",
            default_cache_ttl_seconds=60,
        )

        # First call
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 1)

        # Advance time past custom TTL
        mock_time.return_value = 1061.0

        # Second call - should refetch
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 2)


class TestPromptsGetWithMetadata(TestPrompts):
    """Tests for Prompts.get() with with_metadata=True."""

    @patch("posthog.ai.prompts._get_session")
    def test_return_prompt_result_with_source_api_on_fresh_fetch(
        self, mock_get_session
    ):
        """Should return a PromptResult with source='api' on a fresh fetch."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.get("test-prompt", with_metadata=True)

        self.assertEqual(
            result,
            PromptResult(
                source="api",
                prompt=self.mock_prompt_response["prompt"],
                name="test-prompt",
                version=1,
            ),
        )

    @patch("posthog.ai.prompts._get_session")
    def test_return_source_cache_on_fresh_cache_hit(self, mock_get_session):
        """Should return source='cache' on a fresh cache hit."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call populates cache
        prompts.get("test-prompt", with_metadata=True)

        # Second call should hit cache
        result = prompts.get("test-prompt", with_metadata=True, cache_ttl_seconds=300)

        self.assertEqual(result.source, "cache")
        self.assertEqual(result.prompt, self.mock_prompt_response["prompt"])
        self.assertEqual(result.name, "test-prompt")
        self.assertEqual(result.version, 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_return_source_stale_cache_on_fetch_failure(
        self, mock_time, mock_get_session
    ):
        """Should return source='stale_cache' on fetch failure with stale cache."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = [
            MockResponse(json_data=self.mock_prompt_response),
            Exception("Network error"),
        ]
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call populates cache
        prompts.get("test-prompt", with_metadata=True, cache_ttl_seconds=60)

        # Advance past TTL
        mock_time.return_value = 1061.0

        # Second call should use stale cache
        result = prompts.get("test-prompt", with_metadata=True, cache_ttl_seconds=60)

        self.assertEqual(result.source, "stale_cache")
        self.assertEqual(result.prompt, self.mock_prompt_response["prompt"])
        self.assertEqual(result.name, "test-prompt")
        self.assertEqual(result.version, 1)

    @patch("posthog.ai.prompts._get_session")
    def test_return_source_code_fallback_with_none_metadata(self, mock_get_session):
        """Should return source='code_fallback' with name=None, version=None."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.get(
            "test-prompt", with_metadata=True, fallback="Default system prompt."
        )

        self.assertEqual(
            result,
            PromptResult(
                source="code_fallback",
                prompt="Default system prompt.",
                name=None,
                version=None,
            ),
        )

    @patch("posthog.ai.prompts._get_session")
    def test_throw_when_no_cache_and_no_fallback(self, mock_get_session):
        """Should throw when no cache and no fallback."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("test-prompt", with_metadata=True)

        self.assertIn("Network error", str(context.exception))

    @patch("posthog.ai.prompts._get_session")
    def test_return_correct_version_metadata_for_versioned_fetch(
        self, mock_get_session
    ):
        """Should return correct version metadata for versioned fetches."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(
            json_data={
                **self.mock_prompt_response,
                "version": 3,
                "prompt": "Version 3 prompt",
            }
        )

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.get("test-prompt", with_metadata=True, version=3)

        self.assertEqual(
            result,
            PromptResult(
                source="api",
                prompt="Version 3 prompt",
                name="test-prompt",
                version=3,
            ),
        )

    @patch("posthog.ai.prompts._get_session")
    def test_share_cache_with_non_metadata_calls(self, mock_get_session):
        """Should share cache between with_metadata=True and with_metadata=False."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # First call without metadata populates cache
        string_result = prompts.get("test-prompt", with_metadata=False)
        self.assertEqual(string_result, self.mock_prompt_response["prompt"])

        # Second call with metadata should use cache
        metadata_result = prompts.get("test-prompt", with_metadata=True)
        self.assertEqual(
            metadata_result,
            PromptResult(
                source="cache",
                prompt=self.mock_prompt_response["prompt"],
                name="test-prompt",
                version=1,
            ),
        )
        self.assertEqual(mock_get.call_count, 1)


class TestPromptsGetDeprecationWarning(TestPrompts):
    """Tests for the deprecation warning when with_metadata is not passed."""

    @parameterized.expand(
        [
            ("not_passed", None, 1),
            ("explicit_false", False, 0),
            ("explicit_true", True, 0),
        ]
    )
    @patch("posthog.ai.prompts._get_session")
    def test_deprecation_warning_count(
        self, _scenario, with_metadata, expected_warnings, mock_get_session
    ):
        """Should emit the correct number of deprecation warnings."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        kwargs = {}
        if with_metadata is not None:
            kwargs["with_metadata"] = with_metadata

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            prompts.get("test-prompt", **kwargs)
            # Second call — should never warn again
            prompts.get("test-prompt", **kwargs)

        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        self.assertEqual(len(deprecation_warnings), expected_warnings)


class TestPromptsApiResponseValidation(TestPrompts):
    """Tests for strengthened API response validation."""

    @parameterized.expand(
        [
            ("missing_name", {"prompt": "hello", "version": 1}),
            ("missing_version", {"prompt": "hello", "name": "test"}),
            ("name_not_string", {"prompt": "hello", "name": 123, "version": 1}),
            ("version_not_int", {"prompt": "hello", "name": "test", "version": "1"}),
        ]
    )
    @patch("posthog.ai.prompts._get_session")
    def test_reject_api_response_with_invalid_metadata(
        self, _scenario, json_data, mock_get_session
    ):
        """Should reject API responses with missing or invalid name/version."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=json_data)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("test-prompt", with_metadata=True)

        self.assertIn("Invalid response format", str(context.exception))


class TestPromptsCompile(TestPrompts):
    """Tests for the Prompts.compile() method."""

    def test_replace_a_single_variable(self):
        """Should replace a single variable."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile("Hello, {{name}}!", {"name": "World"})

        self.assertEqual(result, "Hello, World!")

    def test_replace_multiple_variables(self):
        """Should replace multiple variables."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile(
            "Hello, {{name}}! Welcome to {{company}}. Your tier is {{tier}}.",
            {"name": "John", "company": "Acme Corp", "tier": "premium"},
        )

        self.assertEqual(
            result, "Hello, John! Welcome to Acme Corp. Your tier is premium."
        )

    def test_handle_numbers(self):
        """Should handle numbers."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile("You have {{count}} items.", {"count": 42})

        self.assertEqual(result, "You have 42 items.")

    def test_handle_booleans(self):
        """Should handle booleans."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile("Feature enabled: {{enabled}}", {"enabled": True})

        self.assertEqual(result, "Feature enabled: True")

    def test_leave_unmatched_variables_unchanged(self):
        """Should leave unmatched variables unchanged."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile(
            "Hello, {{name}}! Your {{unknown}} is ready.", {"name": "World"}
        )

        self.assertEqual(result, "Hello, World! Your {{unknown}} is ready.")

    def test_handle_prompts_with_no_variables(self):
        """Should handle prompts with no variables."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile("You are a helpful assistant.", {})

        self.assertEqual(result, "You are a helpful assistant.")

    def test_handle_empty_variables_dict(self):
        """Should handle empty variables dict."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile("Hello, {{name}}!", {})

        self.assertEqual(result, "Hello, {{name}}!")

    def test_handle_multiple_occurrences_of_same_variable(self):
        """Should handle multiple occurrences of the same variable."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        result = prompts.compile(
            "Hello, {{name}}! Goodbye, {{name}}!", {"name": "World"}
        )

        self.assertEqual(result, "Hello, World! Goodbye, World!")

    def test_work_with_direct_options_initialization(self):
        """Should work with direct options initialization."""
        prompts = Prompts(
            personal_api_key="phx_test_key", project_api_key="phc_test_key"
        )

        result = prompts.compile("Hello, {{name}}!", {"name": "World"})

        self.assertEqual(result, "Hello, World!")

    def test_handle_variables_with_hyphens(self):
        """Should handle variables with hyphens."""
        prompts = Prompts(
            personal_api_key="phx_test_key", project_api_key="phc_test_key"
        )

        result = prompts.compile("User ID: {{user-id}}", {"user-id": "12345"})

        self.assertEqual(result, "User ID: 12345")

    def test_handle_variables_with_dots(self):
        """Should handle variables with dots."""
        prompts = Prompts(
            personal_api_key="phx_test_key", project_api_key="phc_test_key"
        )

        result = prompts.compile("Company: {{company.name}}", {"company.name": "Acme"})

        self.assertEqual(result, "Company: Acme")


class TestPromptsCaptureErrors(TestPrompts):
    """Tests for the capture_errors option."""

    @patch("posthog.ai.prompts._get_session")
    def test_capture_exception_called_on_fetch_failure_with_fallback(
        self, mock_get_session
    ):
        """Should call capture_exception on fetch failure when capture_errors=True."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog, capture_errors=True)

        result = prompts.get("test-prompt", fallback="fallback prompt", version=3)

        self.assertEqual(result, "fallback prompt")
        posthog.capture_exception.assert_called_once()
        captured_exc = posthog.capture_exception.call_args[0][0]
        self.assertIn("Network error", str(captured_exc))

        properties = posthog.capture_exception.call_args.kwargs["properties"]
        self.assertEqual(properties["$lib_feature"], "ai.prompts")
        self.assertEqual(properties["prompt_name"], "test-prompt")
        self.assertEqual(properties["prompt_version"], 3)
        self.assertEqual(properties["posthog_host"], "https://us.posthog.com")

    @patch("posthog.ai.prompts._get_session")
    @patch("posthog.ai.prompts.time.time")
    def test_capture_exception_called_on_fetch_failure_with_stale_cache(
        self, mock_time, mock_get_session
    ):
        """Should call capture_exception when falling back to stale cache."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = [
            MockResponse(json_data=self.mock_prompt_response),
            Exception("Network error"),
        ]
        mock_time.return_value = 1000.0

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog, capture_errors=True)

        # First call populates cache
        prompts.get("test-prompt", cache_ttl_seconds=60)

        # Expire cache
        mock_time.return_value = 1061.0

        # Second call falls back to stale cache
        result = prompts.get("test-prompt", cache_ttl_seconds=60)
        self.assertEqual(result, self.mock_prompt_response["prompt"])
        posthog.capture_exception.assert_called_once()

    @patch("posthog.ai.prompts._get_session")
    def test_capture_exception_called_when_error_is_raised(self, mock_get_session):
        """Should call capture_exception even when the error is re-raised (no fallback, no cache)."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog, capture_errors=True)

        with self.assertRaises(Exception):
            prompts.get("test-prompt")

        posthog.capture_exception.assert_called_once()

    @patch("posthog.ai.prompts._get_session")
    def test_no_capture_exception_when_capture_errors_is_false(self, mock_get_session):
        """Should NOT call capture_exception when capture_errors=False (default)."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        prompts.get("test-prompt", fallback="fallback prompt")

        posthog.capture_exception.assert_not_called()

    @patch("posthog.ai.prompts._get_session")
    def test_no_capture_exception_without_client(self, mock_get_session):
        """Should not error when capture_errors=True but no client provided."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        prompts = Prompts(
            personal_api_key="phx_test_key",
            project_api_key="phc_test_key",
            capture_errors=True,
        )

        result = prompts.get("test-prompt", fallback="fallback prompt")

        self.assertEqual(result, "fallback prompt")

    @patch("posthog.ai.prompts._get_session")
    def test_no_capture_exception_on_successful_fetch(self, mock_get_session):
        """Should NOT call capture_exception on successful fetch."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog, capture_errors=True)

        prompts.get("test-prompt")

        posthog.capture_exception.assert_not_called()

    @patch("posthog.ai.prompts._get_session")
    def test_capture_exception_failure_does_not_affect_fallback(self, mock_get_session):
        """If capture_exception itself throws, the fallback should still be returned."""
        mock_get = mock_get_session.return_value.get
        mock_get.side_effect = Exception("Network error")

        posthog = self.create_mock_posthog()
        posthog.capture_exception.side_effect = Exception("capture failed")
        prompts = Prompts(posthog, capture_errors=True)

        result = prompts.get("test-prompt", fallback="fallback prompt")

        self.assertEqual(result, "fallback prompt")


class TestPromptsClearCache(TestPrompts):
    """Tests for the Prompts.clear_cache() method."""

    def _populate_versioned_cache(self, prompts, mock_get):
        """Populate cache with latest and versioned entries for the same prompt."""
        latest_prompt_response = {
            **self.mock_prompt_response,
            "prompt": "Latest prompt",
            "version": 2,
        }
        versioned_prompt_response = {
            **self.mock_prompt_response,
            "prompt": "Prompt version 1",
            "version": 1,
        }
        mock_get.side_effect = [
            MockResponse(json_data=latest_prompt_response),
            MockResponse(json_data=versioned_prompt_response),
        ]

        prompts.get("test-prompt")
        prompts.get("test-prompt", version=1)

        return latest_prompt_response, versioned_prompt_response

    def test_clear_cache_with_version_and_no_name_raises_value_error(self):
        """Should enforce that versioned cache clearing requires a prompt name."""
        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(ValueError) as context:
            prompts.clear_cache(version=1)

        self.assertIn("requires 'name'", str(context.exception))

    @patch("posthog.ai.prompts._get_session")
    def test_clear_a_specific_prompt_from_cache(self, mock_get_session):
        """Should clear a specific prompt from cache."""
        mock_get = mock_get_session.return_value.get
        other_prompt_response = {**self.mock_prompt_response, "name": "other-prompt"}

        mock_get.side_effect = [
            MockResponse(json_data=self.mock_prompt_response),
            MockResponse(json_data=other_prompt_response),
            MockResponse(json_data=self.mock_prompt_response),
        ]

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # Populate cache with two prompts
        prompts.get("test-prompt")
        prompts.get("other-prompt")
        self.assertEqual(mock_get.call_count, 2)

        # Clear only test-prompt
        prompts.clear_cache("test-prompt")

        # test-prompt should be refetched
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 3)

        # other-prompt should still be cached
        prompts.get("other-prompt")
        self.assertEqual(mock_get.call_count, 3)

    @patch("posthog.ai.prompts._get_session")
    def test_clear_a_specific_prompt_version_from_cache(self, mock_get_session):
        """Should clear only the requested prompt version from cache."""
        mock_get = mock_get_session.return_value.get

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        _, versioned_prompt_response = self._populate_versioned_cache(prompts, mock_get)
        self.assertEqual(mock_get.call_count, 2)

        mock_get.side_effect = [MockResponse(json_data=versioned_prompt_response)]
        prompts.clear_cache("test-prompt", version=1)

        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 2)

        prompts.get("test-prompt", version=1)
        self.assertEqual(mock_get.call_count, 3)

    @patch("posthog.ai.prompts._get_session")
    def test_clear_a_prompt_name_clears_all_cached_versions(self, mock_get_session):
        """Should clear latest and versioned cache entries for the same prompt name."""
        mock_get = mock_get_session.return_value.get

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        latest_prompt_response, versioned_prompt_response = (
            self._populate_versioned_cache(prompts, mock_get)
        )
        self.assertEqual(mock_get.call_count, 2)

        mock_get.side_effect = [
            MockResponse(json_data=latest_prompt_response),
            MockResponse(json_data=versioned_prompt_response),
        ]
        prompts.clear_cache("test-prompt")

        prompts.get("test-prompt")
        prompts.get("test-prompt", version=1)
        self.assertEqual(mock_get.call_count, 4)

    @patch("posthog.ai.prompts._get_session")
    def test_clear_all_prompts_from_cache(self, mock_get_session):
        """Should clear all prompts from cache when no name is provided."""
        mock_get = mock_get_session.return_value.get
        other_prompt_response = {**self.mock_prompt_response, "name": "other-prompt"}

        mock_get.side_effect = [
            MockResponse(json_data=self.mock_prompt_response),
            MockResponse(json_data=other_prompt_response),
            MockResponse(json_data=self.mock_prompt_response),
            MockResponse(json_data=other_prompt_response),
        ]

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        # Populate cache with two prompts
        prompts.get("test-prompt")
        prompts.get("other-prompt")
        self.assertEqual(mock_get.call_count, 2)

        # Clear all cache
        prompts.clear_cache()

        # Both prompts should be refetched
        prompts.get("test-prompt")
        prompts.get("other-prompt")
        self.assertEqual(mock_get.call_count, 4)


if __name__ == "__main__":
    unittest.main()
