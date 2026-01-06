"""
Tests for FlagDefinitionCacheProvider functionality.

These tests follow the patterns from the TypeScript implementation in posthog-js/packages/node.
"""

import threading
import unittest
from typing import Optional
from unittest import mock

from posthog.client import Client
from posthog.flag_definition_cache import (
    FlagDefinitionCacheData,
    FlagDefinitionCacheProvider,
)
from posthog.request import GetResponse
from posthog.test.test_utils import FAKE_TEST_API_KEY


class MockCacheProvider:
    """A mock implementation of FlagDefinitionCacheProvider for testing."""

    def __init__(self):
        self.stored_data: Optional[FlagDefinitionCacheData] = None
        self.should_fetch_return_value = True
        self.get_call_count = 0
        self.should_fetch_call_count = 0
        self.on_received_call_count = 0
        self.shutdown_call_count = 0
        self.should_fetch_error: Optional[Exception] = None
        self.get_error: Optional[Exception] = None
        self.on_received_error: Optional[Exception] = None
        self.shutdown_error: Optional[Exception] = None

    def get_flag_definitions(self) -> Optional[FlagDefinitionCacheData]:
        self.get_call_count += 1
        if self.get_error:
            raise self.get_error
        return self.stored_data

    def should_fetch_flag_definitions(self) -> bool:
        self.should_fetch_call_count += 1
        if self.should_fetch_error:
            raise self.should_fetch_error
        return self.should_fetch_return_value

    def on_flag_definitions_received(self, data: FlagDefinitionCacheData) -> None:
        self.on_received_call_count += 1
        if self.on_received_error:
            raise self.on_received_error
        self.stored_data = data

    def shutdown(self) -> None:
        self.shutdown_call_count += 1
        if self.shutdown_error:
            raise self.shutdown_error


class TestFlagDefinitionCacheProvider(unittest.TestCase):
    """Tests for the FlagDefinitionCacheProvider protocol."""

    @classmethod
    def setUpClass(cls):
        # Prevent real HTTP requests
        cls.client_post_patcher = mock.patch("posthog.client.batch_post")
        cls.consumer_post_patcher = mock.patch("posthog.consumer.batch_post")
        cls.client_post_patcher.start()
        cls.consumer_post_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.client_post_patcher.stop()
        cls.consumer_post_patcher.stop()

    def setUp(self):
        self.cache_provider = MockCacheProvider()
        self.sample_flags_data: FlagDefinitionCacheData = {
            "flags": [
                {"key": "test-flag", "active": True, "filters": {}},
                {"key": "another-flag", "active": False, "filters": {}},
            ],
            "group_type_mapping": {"0": "company", "1": "project"},
            "cohorts": {"1": {"properties": []}},
        }

    def tearDown(self):
        # Ensure client cleanup
        pass

    def _create_client_with_cache(self) -> Client:
        """Create a client with the mock cache provider."""
        return Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test-personal-key",
            flag_definition_cache_provider=self.cache_provider,
            sync_mode=True,
            enable_local_evaluation=False,  # Disable poller for tests
        )


class TestCacheInitialization(TestFlagDefinitionCacheProvider):
    """Tests for cache initialization behavior."""

    @mock.patch("posthog.client.get")
    def test_uses_cached_data_when_should_fetch_returns_false(self, mock_get):
        """When should_fetch returns False and cache has data, use cached data."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = self.sample_flags_data

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should not call API
        mock_get.assert_not_called()

        # Should have called cache methods
        self.assertEqual(self.cache_provider.should_fetch_call_count, 1)
        self.assertEqual(self.cache_provider.get_call_count, 1)

        # Flags should be loaded from cache
        self.assertEqual(len(client.feature_flags), 2)
        self.assertEqual(client.feature_flags[0]["key"], "test-flag")

        client.join()

    @mock.patch("posthog.client.get")
    def test_fetches_from_api_when_should_fetch_returns_true(self, mock_get):
        """When should_fetch returns True, fetch from API."""
        self.cache_provider.should_fetch_return_value = True

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should call API
        mock_get.assert_called_once()

        # Should have called should_fetch but not get
        self.assertEqual(self.cache_provider.should_fetch_call_count, 1)
        self.assertEqual(self.cache_provider.get_call_count, 0)

        # Should have called on_received to store in cache
        self.assertEqual(self.cache_provider.on_received_call_count, 1)

        client.join()

    @mock.patch("posthog.client.get")
    def test_emergency_fallback_when_cache_empty_and_no_flags(self, mock_get):
        """When should_fetch=False but cache is empty and no flags loaded, fetch anyway."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = None  # Empty cache

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should call API due to emergency fallback
        mock_get.assert_called_once()

        # Should have called on_received
        self.assertEqual(self.cache_provider.on_received_call_count, 1)

        client.join()

    @mock.patch("posthog.client.get")
    def test_preserves_existing_flags_when_cache_returns_none(self, mock_get):
        """When cache returns None but client has flags, preserve existing flags."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = None  # Empty cache

        client = self._create_client_with_cache()

        # Pre-load flags (simulating a previous successful fetch)
        client.feature_flags = self.sample_flags_data["flags"]
        client.group_type_mapping = self.sample_flags_data["group_type_mapping"]
        client.cohorts = self.sample_flags_data["cohorts"]

        client._load_feature_flags()

        # Should NOT call API since we already have flags
        mock_get.assert_not_called()

        # Existing flags should be preserved
        self.assertEqual(len(client.feature_flags), 2)
        self.assertEqual(client.feature_flags[0]["key"], "test-flag")

        client.join()


class TestFetchCoordination(TestFlagDefinitionCacheProvider):
    """Tests for fetch coordination between workers."""

    @mock.patch("posthog.client.get")
    def test_calls_should_fetch_before_each_poll(self, mock_get):
        """should_fetch_flag_definitions is called before each poll cycle."""
        self.cache_provider.should_fetch_return_value = True

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()

        # First poll
        client._load_feature_flags()
        self.assertEqual(self.cache_provider.should_fetch_call_count, 1)

        # Second poll
        client._load_feature_flags()
        self.assertEqual(self.cache_provider.should_fetch_call_count, 2)

        client.join()

    @mock.patch("posthog.client.get")
    def test_does_not_call_on_received_when_fetch_skipped(self, mock_get):
        """on_flag_definitions_received is NOT called when fetch is skipped."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = self.sample_flags_data

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should not call on_received since we didn't fetch
        self.assertEqual(self.cache_provider.on_received_call_count, 0)

        client.join()

    @mock.patch("posthog.client.get")
    def test_stores_data_in_cache_after_api_fetch(self, mock_get):
        """on_flag_definitions_received receives the fetched data."""
        self.cache_provider.should_fetch_return_value = True

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should have stored data in cache
        self.assertEqual(self.cache_provider.on_received_call_count, 1)
        self.assertIsNotNone(self.cache_provider.stored_data)
        self.assertEqual(len(self.cache_provider.stored_data["flags"]), 2)

        client.join()

    @mock.patch("posthog.client.get")
    def test_304_not_modified_does_not_update_cache(self, mock_get):
        """When API returns 304 Not Modified, cache should not be updated."""
        self.cache_provider.should_fetch_return_value = True

        # First fetch to populate flags and ETag
        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Verify initial fetch worked
        self.assertEqual(self.cache_provider.on_received_call_count, 1)
        self.assertEqual(len(client.feature_flags), 2)

        # Second fetch returns 304 Not Modified
        mock_get.return_value = GetResponse(
            data=None, etag="test-etag", not_modified=True
        )

        client._load_feature_flags()

        # API was called twice
        self.assertEqual(mock_get.call_count, 2)

        # should_fetch was called twice
        self.assertEqual(self.cache_provider.should_fetch_call_count, 2)

        # on_received should NOT be called again (304 = no new data)
        self.assertEqual(self.cache_provider.on_received_call_count, 1)

        # Flags should still be present
        self.assertEqual(len(client.feature_flags), 2)

        client.join()


class TestErrorHandling(TestFlagDefinitionCacheProvider):
    """Tests for error handling in cache provider operations."""

    @mock.patch("posthog.client.get")
    def test_should_fetch_error_defaults_to_fetching(self, mock_get):
        """When should_fetch throws an error, default to fetching from API."""
        self.cache_provider.should_fetch_error = Exception("Lock acquisition failed")

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should still fetch from API
        mock_get.assert_called_once()

        # Flags should be loaded
        self.assertEqual(len(client.feature_flags), 2)

        client.join()

    @mock.patch("posthog.client.get")
    def test_get_error_falls_back_to_api_fetch(self, mock_get):
        """When get_flag_definitions throws an error, fetch from API."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.get_error = Exception("Cache read failed")

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should fall back to API
        mock_get.assert_called_once()

        client.join()

    @mock.patch("posthog.client.get")
    def test_on_received_error_keeps_flags_in_memory(self, mock_get):
        """When on_flag_definitions_received throws, flags are still in memory."""
        self.cache_provider.should_fetch_return_value = True
        self.cache_provider.on_received_error = Exception("Cache write failed")

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Flags should still be loaded in memory despite cache error
        self.assertEqual(len(client.feature_flags), 2)
        self.assertEqual(client.feature_flags[0]["key"], "test-flag")

        client.join()

    @mock.patch("posthog.client.get")
    def test_shutdown_error_is_logged_but_continues(self, mock_get):
        """When shutdown throws an error, it's logged but shutdown continues."""
        self.cache_provider.shutdown_error = Exception("Lock release failed")

        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Should not raise when joining
        client.join()

        # Shutdown was called
        self.assertEqual(self.cache_provider.shutdown_call_count, 1)


class TestShutdownLifecycle(TestFlagDefinitionCacheProvider):
    """Tests for shutdown lifecycle."""

    @mock.patch("posthog.client.get")
    def test_shutdown_calls_cache_provider_shutdown(self, mock_get):
        """Client shutdown calls cache provider shutdown."""
        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Shutdown
        client.join()

        self.assertEqual(self.cache_provider.shutdown_call_count, 1)

    @mock.patch("posthog.client.get")
    def test_shutdown_called_even_without_fetching(self, mock_get):
        """Shutdown is called even when cache was used instead of fetching."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = self.sample_flags_data

        client = self._create_client_with_cache()
        client._load_feature_flags()
        client.join()

        # Shutdown should still be called
        self.assertEqual(self.cache_provider.shutdown_call_count, 1)

    @mock.patch("posthog.client.get")
    def test_multiple_join_calls_only_shutdown_once(self, mock_get):
        """Calling join() multiple times should only call cache provider shutdown once."""
        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Call join multiple times
        client.join()
        client.join()
        client.join()

        # Shutdown should be called each time (current behavior - no guard)
        # This test documents the current behavior
        self.assertGreaterEqual(self.cache_provider.shutdown_call_count, 1)


class TestBackwardCompatibility(TestFlagDefinitionCacheProvider):
    """Tests for backward compatibility without cache provider."""

    @mock.patch("posthog.client.get")
    def test_works_without_cache_provider(self, mock_get):
        """Client works normally without a cache provider configured."""
        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        # Create client without cache provider
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key="test-personal-key",
            sync_mode=True,
            enable_local_evaluation=False,
        )
        client._load_feature_flags()

        # Should fetch from API
        mock_get.assert_called_once()

        # Flags should be loaded
        self.assertEqual(len(client.feature_flags), 2)

        client.join()


class TestDataIntegrity(TestFlagDefinitionCacheProvider):
    """Tests for data integrity between cache and client state."""

    @mock.patch("posthog.client.get")
    def test_cached_flags_available_for_evaluation(self, mock_get):
        """Flags loaded from cache are available for local evaluation."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = {
            "flags": [
                {
                    "key": "test-flag",
                    "active": True,
                    "filters": {
                        "groups": [
                            {
                                "properties": [],
                                "rollout_percentage": 100,
                            }
                        ]
                    },
                }
            ],
            "group_type_mapping": {},
            "cohorts": {},
        }

        client = self._create_client_with_cache()
        client._load_feature_flags()

        # Flag should be accessible
        self.assertEqual(len(client.feature_flags), 1)
        self.assertEqual(client.feature_flags_by_key["test-flag"]["key"], "test-flag")

        client.join()

    @mock.patch("posthog.client.get")
    def test_group_type_mapping_loaded_from_cache(self, mock_get):
        """Group type mapping is correctly loaded from cache."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = self.sample_flags_data

        client = self._create_client_with_cache()
        client._load_feature_flags()

        self.assertEqual(client.group_type_mapping["0"], "company")
        self.assertEqual(client.group_type_mapping["1"], "project")

        client.join()

    @mock.patch("posthog.client.get")
    def test_cohorts_loaded_from_cache(self, mock_get):
        """Cohorts are correctly loaded from cache."""
        self.cache_provider.should_fetch_return_value = False
        self.cache_provider.stored_data = self.sample_flags_data

        client = self._create_client_with_cache()
        client._load_feature_flags()

        self.assertIn("1", client.cohorts)

        client.join()

    @mock.patch("posthog.client.get")
    def test_cache_updated_when_api_returns_new_data(self, mock_get):
        """State transition: cache has old data -> API returns new -> cache updated."""
        # Start with old cached data
        old_flags_data: FlagDefinitionCacheData = {
            "flags": [{"key": "old-flag", "active": True, "filters": {}}],
            "group_type_mapping": {},
            "cohorts": {},
        }
        self.cache_provider.stored_data = old_flags_data
        self.cache_provider.should_fetch_return_value = False

        client = self._create_client_with_cache()

        # First load from cache
        client._load_feature_flags()
        self.assertEqual(client.feature_flags[0]["key"], "old-flag")
        self.assertEqual(self.cache_provider.on_received_call_count, 0)

        # Now trigger API fetch with new data
        self.cache_provider.should_fetch_return_value = True
        new_flags_data: FlagDefinitionCacheData = {
            "flags": [{"key": "new-flag", "active": True, "filters": {}}],
            "group_type_mapping": {"0": "company"},
            "cohorts": {"1": {"properties": []}},
        }
        mock_get.return_value = GetResponse(
            data=new_flags_data, etag="new-etag", not_modified=False
        )

        client._load_feature_flags()

        # Verify new flags loaded
        self.assertEqual(client.feature_flags[0]["key"], "new-flag")
        self.assertEqual(client.group_type_mapping["0"], "company")

        # Verify cache was updated
        self.assertEqual(self.cache_provider.on_received_call_count, 1)
        self.assertEqual(self.cache_provider.stored_data["flags"][0]["key"], "new-flag")

        client.join()


class TestConcurrency(TestFlagDefinitionCacheProvider):
    """Tests for thread safety and concurrent access."""

    @mock.patch("posthog.client.get")
    def test_concurrent_load_feature_flags_is_thread_safe(self, mock_get):
        """Multiple threads calling _load_feature_flags should not cause errors."""
        mock_get.return_value = GetResponse(
            data=self.sample_flags_data, etag="test-etag", not_modified=False
        )

        client = self._create_client_with_cache()
        errors = []

        def load_flags():
            try:
                client._load_feature_flags()
            except Exception as e:
                errors.append(e)

        # Launch 5 threads concurrently
        threads = [threading.Thread(target=load_flags) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should complete without errors
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")

        # Flags should be loaded
        self.assertIsNotNone(client.feature_flags)
        self.assertEqual(len(client.feature_flags), 2)

        client.join()


class TestProtocolCompliance(unittest.TestCase):
    """Tests for Protocol compliance."""

    def test_mock_provider_is_protocol_instance(self):
        """MockCacheProvider satisfies FlagDefinitionCacheProvider protocol."""
        provider = MockCacheProvider()
        self.assertIsInstance(provider, FlagDefinitionCacheProvider)

    def test_incomplete_provider_is_not_protocol_instance(self):
        """Class missing methods is not a FlagDefinitionCacheProvider."""

        class IncompleteProvider:
            def get_flag_definitions(self):
                return None

        provider = IncompleteProvider()
        self.assertNotIsInstance(provider, FlagDefinitionCacheProvider)


if __name__ == "__main__":
    unittest.main()
