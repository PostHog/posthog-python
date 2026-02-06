import unittest
from unittest.mock import MagicMock, patch

from posthog.ai.prompts import Prompts


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
        self, personal_api_key="phx_test_key", host="https://us.posthog.com"
    ):
        """Create a mock PostHog client."""
        mock = MagicMock()
        mock.personal_api_key = personal_api_key
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
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/",
        )
        self.assertIn("Authorization", call_args[1]["headers"])
        self.assertEqual(
            call_args[1]["headers"]["Authorization"], "Bearer phx_test_key"
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

    @patch("posthog.ai.prompts._get_session")
    def test_handle_404_response(self, mock_get_session):
        """Should handle 404 response."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(status_code=404, ok=False)

        posthog = self.create_mock_posthog()
        prompts = Prompts(posthog)

        with self.assertRaises(Exception) as context:
            prompts.get("nonexistent-prompt")

        self.assertIn('Prompt "nonexistent-prompt" not found', str(context.exception))

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

        posthog = self.create_mock_posthog(host="https://eu.i.posthog.com")
        prompts = Prompts(posthog)

        prompts.get("test-prompt")

        call_args = mock_get.call_args
        self.assertTrue(
            call_args[0][0].startswith("https://eu.i.posthog.com/"),
            f"Expected URL to start with 'https://eu.i.posthog.com/', got {call_args[0][0]}",
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
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/prompt%20with%20spaces%2Fand%2Fslashes/",
        )

    @patch("posthog.ai.prompts._get_session")
    def test_work_with_direct_options_no_posthog_client(self, mock_get_session):
        """Should work with direct options (no PostHog client)."""
        mock_get = mock_get_session.return_value.get
        mock_get.return_value = MockResponse(json_data=self.mock_prompt_response)

        prompts = Prompts(personal_api_key="phx_direct_key")

        result = prompts.get("test-prompt")

        self.assertEqual(result, self.mock_prompt_response["prompt"])
        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://us.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/",
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
            personal_api_key="phx_direct_key", host="https://eu.posthog.com"
        )

        prompts.get("test-prompt")

        call_args = mock_get.call_args
        self.assertEqual(
            call_args[0][0],
            "https://eu.posthog.com/api/environments/@current/llm_prompts/name/test-prompt/",
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
            personal_api_key="phx_direct_key", default_cache_ttl_seconds=60
        )

        # First call
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 1)

        # Advance time past custom TTL
        mock_time.return_value = 1061.0

        # Second call - should refetch
        prompts.get("test-prompt")
        self.assertEqual(mock_get.call_count, 2)


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
        prompts = Prompts(personal_api_key="phx_test_key")

        result = prompts.compile("Hello, {{name}}!", {"name": "World"})

        self.assertEqual(result, "Hello, World!")

    def test_handle_variables_with_hyphens(self):
        """Should handle variables with hyphens."""
        prompts = Prompts(personal_api_key="phx_test_key")

        result = prompts.compile("User ID: {{user-id}}", {"user-id": "12345"})

        self.assertEqual(result, "User ID: 12345")

    def test_handle_variables_with_dots(self):
        """Should handle variables with dots."""
        prompts = Prompts(personal_api_key="phx_test_key")

        result = prompts.compile("Company: {{company.name}}", {"company.name": "Acme"})

        self.assertEqual(result, "Company: Acme")


class TestPromptsClearCache(TestPrompts):
    """Tests for the Prompts.clear_cache() method."""

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
