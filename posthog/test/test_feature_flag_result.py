import unittest

import mock

from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.types import (
    FeatureFlag,
    FeatureFlagError,
    FeatureFlagResult,
    FlagMetadata,
    FlagReason,
)


class TestFeatureFlagResult(unittest.TestCase):
    def test_from_bool_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload(
            "test-flag", True, "[1, 2, 3]"
        )

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, [1, 2, 3])

    def test_from_false_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload(
            "test-flag", False, '{"some": "value"}'
        )

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, False)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, {"some": "value"})

    def test_from_variant_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload(
            "test-flag", "control", "true"
        )

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, "control")
        self.assertEqual(result.payload, True)

    def test_from_none_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload(
            "test-flag", None, '{"some": "value"}'
        )
        self.assertIsNone(result)

    def test_from_boolean_flag_details(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant=None,
            metadata=FlagMetadata(
                id=1, version=1, description="test-flag", payload='"Some string"'
            ),
            reason=FlagReason(
                code="test-reason", description="test-reason", condition_index=0
            ),
        )

        result = FeatureFlagResult.from_flag_details(flag_details)

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, "Some string")

    def test_from_boolean_flag_details_with_override_variant_match_value(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant=None,
            metadata=FlagMetadata(
                id=1, version=1, description="test-flag", payload='"Some string"'
            ),
            reason=FlagReason(
                code="test-reason", description="test-reason", condition_index=0
            ),
        )

        result = FeatureFlagResult.from_flag_details(
            flag_details, override_match_value="control"
        )

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, "control")
        self.assertEqual(result.payload, "Some string")

    def test_from_boolean_flag_details_with_override_boolean_match_value(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant="control",
            metadata=FlagMetadata(
                id=1, version=1, description="test-flag", payload='{"some": "value"}'
            ),
            reason=FlagReason(
                code="test-reason", description="test-reason", condition_index=0
            ),
        )

        result = FeatureFlagResult.from_flag_details(
            flag_details, override_match_value=True
        )

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, {"some": "value"})

    def test_from_boolean_flag_details_with_override_false_match_value(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant="control",
            metadata=FlagMetadata(
                id=1, version=1, description="test-flag", payload='{"some": "value"}'
            ),
            reason=FlagReason(
                code="test-reason", description="test-reason", condition_index=0
            ),
        )

        result = FeatureFlagResult.from_flag_details(
            flag_details, override_match_value=False
        )

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, False)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, {"some": "value"})

    def test_from_variant_flag_details(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant="control",
            metadata=FlagMetadata(
                id=1, version=1, description="test-flag", payload='{"some": "value"}'
            ),
            reason=FlagReason(
                code="test-reason", description="test-reason", condition_index=0
            ),
        )

        result = FeatureFlagResult.from_flag_details(flag_details)

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, "control")
        self.assertEqual(result.payload, {"some": "value"})

    def test_from_none_flag_details(self):
        result = FeatureFlagResult.from_flag_details(None)

        self.assertIsNone(result)

    def test_from_flag_details_with_none_payload(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant=None,
            metadata=FlagMetadata(
                id=1, version=1, description="test-flag", payload=None
            ),
            reason=FlagReason(
                code="test-reason", description="test-reason", condition_index=0
            ),
        )

        result = FeatureFlagResult.from_flag_details(flag_details)

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertIsNone(result.payload)


class TestGetFeatureFlagResult(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This ensures no real HTTP POST requests are made
        cls.capture_patch = mock.patch.object(Client, "capture")
        cls.capture_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.capture_patch.stop()

    def set_fail(self, e, batch):
        """Mark the failure handler"""
        self.failed = True

    def setUp(self):
        self.failed = False
        self.client = Client(FAKE_TEST_API_KEY, on_error=self.set_fail)

    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_boolean_local_evaluation(self, patch_capture):
        basic_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "person-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "region",
                                "operator": "exact",
                                "value": ["USA"],
                                "type": "person",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
                "payloads": {"true": "300"},
            },
        }
        self.client.feature_flags = [basic_flag]

        flag_result = self.client.get_feature_flag_result(
            "person-flag", "some-distinct-id", person_properties={"region": "USA"}
        )
        self.assertEqual(flag_result.enabled, True)
        self.assertEqual(flag_result.variant, None)
        self.assertEqual(flag_result.payload, 300)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": True,
                "locally_evaluated": True,
                "$feature/person-flag": True,
                "$feature_flag_payload": 300,
            },
            groups={},
            disable_geoip=None,
        )
        # Verify error property is NOT present on successful evaluation
        captured_properties = patch_capture.call_args[1]["properties"]
        self.assertNotIn("$feature_flag_error", captured_properties)

    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_variant_local_evaluation(self, patch_capture):
        basic_flag = {
            "id": 1,
            "name": "Beta Feature",
            "key": "person-flag",
            "active": True,
            "filters": {
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "region",
                                "operator": "exact",
                                "value": ["USA"],
                                "type": "person",
                            }
                        ],
                        "rollout_percentage": 100,
                    }
                ],
                "multivariate": {
                    "variants": [
                        {"key": "variant-1", "rollout_percentage": 50},
                        {"key": "variant-2", "rollout_percentage": 50},
                    ]
                },
                "payloads": {"variant-1": '{"some": "value"}'},
            },
        }
        self.client.feature_flags = [basic_flag]

        flag_result = self.client.get_feature_flag_result(
            "person-flag", "distinct_id", person_properties={"region": "USA"}
        )
        self.assertEqual(flag_result.enabled, True)
        self.assertEqual(flag_result.variant, "variant-1")
        self.assertEqual(flag_result.get_value(), "variant-1")
        self.assertEqual(flag_result.payload, {"some": "value"})

        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="distinct_id",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": "variant-1",
                "locally_evaluated": True,
                "$feature/person-flag": "variant-1",
                "$feature_flag_payload": {"some": "value"},
            },
            groups={},
            disable_geoip=None,
        )
        # Verify error property is NOT present on successful evaluation
        captured_properties = patch_capture.call_args[1]["properties"]
        self.assertNotIn("$feature_flag_error", captured_properties)

        another_flag_result = self.client.get_feature_flag_result(
            "person-flag", "another-distinct-id", person_properties={"region": "USA"}
        )
        self.assertEqual(another_flag_result.enabled, True)
        self.assertEqual(another_flag_result.variant, "variant-2")
        self.assertEqual(another_flag_result.get_value(), "variant-2")
        self.assertIsNone(another_flag_result.payload)

        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="another-distinct-id",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": "variant-2",
                "locally_evaluated": True,
                "$feature/person-flag": "variant-2",
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_boolean_decide(self, patch_capture, patch_flags):
        patch_flags.return_value = {
            "flags": {
                "person-flag": {
                    "key": "person-flag",
                    "enabled": True,
                    "variant": None,
                    "reason": {
                        "description": "Matched condition set 1",
                    },
                    "metadata": {
                        "id": 23,
                        "version": 42,
                        "payload": "300",
                    },
                },
            },
        }

        flag_result = self.client.get_feature_flag_result(
            "person-flag", "some-distinct-id"
        )
        self.assertEqual(flag_result.enabled, True)
        self.assertEqual(flag_result.variant, None)
        self.assertEqual(flag_result.payload, 300)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": True,
                "locally_evaluated": False,
                "$feature/person-flag": True,
                "$feature_flag_reason": "Matched condition set 1",
                "$feature_flag_id": 23,
                "$feature_flag_version": 42,
                "$feature_flag_payload": 300,
            },
            groups={},
            disable_geoip=None,
        )
        # Verify error property is NOT present on successful evaluation
        captured_properties = patch_capture.call_args[1]["properties"]
        self.assertNotIn("$feature_flag_error", captured_properties)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_variant_decide(self, patch_capture, patch_flags):
        patch_flags.return_value = {
            "flags": {
                "person-flag": {
                    "key": "person-flag",
                    "enabled": True,
                    "variant": "variant-1",
                    "reason": {
                        "description": "Matched condition set 1",
                    },
                    "metadata": {
                        "id": 1,
                        "version": 2,
                        "payload": "[1, 2, 3]",
                    },
                },
            },
        }

        flag_result = self.client.get_feature_flag_result("person-flag", "distinct_id")
        self.assertEqual(flag_result.enabled, True)
        self.assertEqual(flag_result.variant, "variant-1")
        self.assertEqual(flag_result.get_value(), "variant-1")
        self.assertEqual(flag_result.payload, [1, 2, 3])
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="distinct_id",
            properties={
                "$feature_flag": "person-flag",
                "$feature_flag_response": "variant-1",
                "locally_evaluated": False,
                "$feature/person-flag": "variant-1",
                "$feature_flag_reason": "Matched condition set 1",
                "$feature_flag_id": 1,
                "$feature_flag_version": 2,
                "$feature_flag_payload": [1, 2, 3],
            },
            groups={},
            disable_geoip=None,
        )
        # Verify error property is NOT present on successful evaluation
        captured_properties = patch_capture.call_args[1]["properties"]
        self.assertNotIn("$feature_flag_error", captured_properties)

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_unknown_flag(self, patch_capture, patch_flags):
        patch_flags.return_value = {
            "flags": {
                "person-flag": {
                    "key": "person-flag",
                    "enabled": True,
                    "variant": None,
                    "reason": {
                        "description": "Matched condition set 1",
                    },
                    "metadata": {
                        "id": 23,
                        "version": 42,
                        "payload": "300",
                    },
                },
            },
        }

        flag_result = self.client.get_feature_flag_result(
            "no-person-flag", "some-distinct-id"
        )

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "no-person-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/no-person-flag": None,
                "$feature_flag_error": FeatureFlagError.FLAG_MISSING,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_with_errors_while_computing_flags(
        self, patch_capture, patch_flags
    ):
        """Test that errors_while_computing_flags is included in the $feature_flag_called event.

        When the server returns errorsWhileComputingFlags=true, it indicates that there
        was an error computing one or more flags. We include this in the event so users
        can identify and debug flag evaluation issues.
        """
        patch_flags.return_value = {
            "flags": {
                "my-flag": {
                    "key": "my-flag",
                    "enabled": True,
                    "variant": None,
                    "reason": {"description": "Matched condition set 1"},
                    "metadata": {"id": 1, "version": 1, "payload": None},
                },
            },
            "requestId": "test-request-id-789",
            "errorsWhileComputingFlags": True,
        }

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        self.assertEqual(flag_result.enabled, True)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": True,
                "locally_evaluated": False,
                "$feature/my-flag": True,
                "$feature_flag_request_id": "test-request-id-789",
                "$feature_flag_reason": "Matched condition set 1",
                "$feature_flag_id": 1,
                "$feature_flag_version": 1,
                "$feature_flag_error": FeatureFlagError.ERRORS_WHILE_COMPUTING,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_flag_not_in_response(
        self, patch_capture, patch_flags
    ):
        """Test that when a flag is not in the API response, we capture flag_missing error.

        This happens when a flag doesn't exist or the user doesn't match any conditions.
        """
        patch_flags.return_value = {
            "flags": {
                "other-flag": {
                    "key": "other-flag",
                    "enabled": True,
                    "variant": None,
                    "reason": {"description": "Matched condition set 1"},
                    "metadata": {"id": 1, "version": 1, "payload": None},
                },
            },
            "requestId": "test-request-id-456",
        }

        flag_result = self.client.get_feature_flag_result(
            "missing-flag", "some-distinct-id"
        )

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "missing-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/missing-flag": None,
                "$feature_flag_request_id": "test-request-id-456",
                "$feature_flag_error": FeatureFlagError.FLAG_MISSING,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_errors_computing_and_flag_missing(
        self, patch_capture, patch_flags
    ):
        """Test that both errors are reported when errorsWhileComputingFlags=true AND flag is missing.

        This can happen when the server encounters errors computing flags AND the requested
        flag is not in the response. Both conditions should be reported for debugging.
        """
        patch_flags.return_value = {
            "flags": {},  # Flag is missing
            "requestId": "test-request-id-999",
            "errorsWhileComputingFlags": True,  # But errors also occurred
        }

        flag_result = self.client.get_feature_flag_result(
            "missing-flag", "some-distinct-id"
        )

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "missing-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/missing-flag": None,
                "$feature_flag_request_id": "test-request-id-999",
                "$feature_flag_error": f"{FeatureFlagError.ERRORS_WHILE_COMPUTING},{FeatureFlagError.FLAG_MISSING}",
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_unknown_error(self, patch_capture, patch_flags):
        """Test that unexpected exceptions are captured as unknown_error."""
        patch_flags.side_effect = Exception("Unexpected error")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/my-flag": None,
                "$feature_flag_error": FeatureFlagError.UNKNOWN_ERROR,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_timeout_error(self, patch_capture, patch_flags):
        """Test that timeout errors are captured specifically."""
        from posthog.request import RequestsTimeout

        patch_flags.side_effect = RequestsTimeout("Request timed out")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/my-flag": None,
                "$feature_flag_error": FeatureFlagError.TIMEOUT,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_connection_error(self, patch_capture, patch_flags):
        """Test that connection errors are captured specifically."""
        from posthog.request import RequestsConnectionError

        patch_flags.side_effect = RequestsConnectionError("Connection refused")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/my-flag": None,
                "$feature_flag_error": FeatureFlagError.CONNECTION_ERROR,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_api_error(self, patch_capture, patch_flags):
        """Test that API errors include the status code."""
        from posthog.request import APIError

        patch_flags.side_effect = APIError(500, "Internal server error")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/my-flag": None,
                "$feature_flag_error": FeatureFlagError.api_error(500),
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_get_feature_flag_result_quota_limited(self, patch_capture, patch_flags):
        """Test that quota limit errors are captured specifically."""
        from posthog.request import QuotaLimitError

        patch_flags.side_effect = QuotaLimitError(429, "Rate limit exceeded")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        self.assertIsNone(flag_result)
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/my-flag": None,
                "$feature_flag_error": FeatureFlagError.QUOTA_LIMITED,
            },
            groups={},
            disable_geoip=None,
        )


class TestFeatureFlagErrorWithStaleCacheFallback(unittest.TestCase):
    """Tests for stale cache fallback behavior when flag evaluation fails.

    When the PostHog API is unavailable (timeout, connection error, etc.), the SDK
    falls back to stale cached flag values if available. These tests verify that:
    1. The stale cached value is returned when an error occurs
    2. The $feature_flag_error property is still set (for debugging)
    3. The response reflects the cached value, not None
    """

    def set_fail(self, e, batch):
        """Mark the failure handler"""
        self.failed = True

    def setUp(self):
        self.failed = False
        # Create client with memory-based flag cache enabled
        self.client = Client(
            FAKE_TEST_API_KEY,
            on_error=self.set_fail,
            flag_fallback_cache_url="memory://local/?ttl=300&size=10000",
        )

    def _populate_stale_cache(self, distinct_id, flag_key, flag_result):
        """Pre-populate the flag cache with a value that will be used for stale fallback."""
        self.client.flag_cache.set_cached_flag(
            distinct_id,
            flag_key,
            flag_result,
            flag_definition_version=self.client.flag_definition_version,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_timeout_error_returns_stale_cached_value(self, patch_capture, patch_flags):
        """Test that timeout errors return stale cached value when available."""
        from posthog.request import RequestsTimeout

        # Pre-populate cache with a flag result
        cached_result = FeatureFlagResult.from_value_and_payload(
            "my-flag", "cached-variant", '{"from": "cache"}'
        )
        self._populate_stale_cache("some-distinct-id", "my-flag", cached_result)

        # Simulate timeout error
        patch_flags.side_effect = RequestsTimeout("Request timed out")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        # Should return the stale cached value
        self.assertIsNotNone(flag_result)
        self.assertEqual(flag_result.variant, "cached-variant")
        self.assertEqual(flag_result.payload, {"from": "cache"})

        # Error should still be tracked for debugging
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": "cached-variant",
                "locally_evaluated": False,
                "$feature/my-flag": "cached-variant",
                "$feature_flag_payload": {"from": "cache"},
                "$feature_flag_error": FeatureFlagError.TIMEOUT,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_connection_error_returns_stale_cached_value(
        self, patch_capture, patch_flags
    ):
        """Test that connection errors return stale cached value when available."""
        from posthog.request import RequestsConnectionError

        # Pre-populate cache with a boolean flag result
        cached_result = FeatureFlagResult.from_value_and_payload("my-flag", True, None)
        self._populate_stale_cache("some-distinct-id", "my-flag", cached_result)

        # Simulate connection error
        patch_flags.side_effect = RequestsConnectionError("Connection refused")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        # Should return the stale cached value
        self.assertIsNotNone(flag_result)
        self.assertEqual(flag_result.enabled, True)
        self.assertIsNone(flag_result.variant)

        # Error should still be tracked
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": True,
                "locally_evaluated": False,
                "$feature/my-flag": True,
                "$feature_flag_error": FeatureFlagError.CONNECTION_ERROR,
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_api_error_returns_stale_cached_value(self, patch_capture, patch_flags):
        """Test that API errors return stale cached value when available."""
        from posthog.request import APIError

        # Pre-populate cache
        cached_result = FeatureFlagResult.from_value_and_payload(
            "my-flag", "control", None
        )
        self._populate_stale_cache("some-distinct-id", "my-flag", cached_result)

        # Simulate API error
        patch_flags.side_effect = APIError(503, "Service unavailable")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        # Should return the stale cached value
        self.assertIsNotNone(flag_result)
        self.assertEqual(flag_result.variant, "control")

        # Error should still be tracked with status code
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": "control",
                "locally_evaluated": False,
                "$feature/my-flag": "control",
                "$feature_flag_error": FeatureFlagError.api_error(503),
            },
            groups={},
            disable_geoip=None,
        )

    @mock.patch("posthog.client.flags")
    @mock.patch.object(Client, "capture")
    def test_error_without_cache_returns_none(self, patch_capture, patch_flags):
        """Test that errors return None when no stale cache is available."""
        from posthog.request import RequestsTimeout

        # Do NOT populate cache - no fallback available

        patch_flags.side_effect = RequestsTimeout("Request timed out")

        flag_result = self.client.get_feature_flag_result("my-flag", "some-distinct-id")

        # Should return None since no cache available
        self.assertIsNone(flag_result)

        # Error should still be tracked
        patch_capture.assert_called_with(
            "$feature_flag_called",
            distinct_id="some-distinct-id",
            properties={
                "$feature_flag": "my-flag",
                "$feature_flag_response": None,
                "locally_evaluated": False,
                "$feature/my-flag": None,
                "$feature_flag_error": FeatureFlagError.TIMEOUT,
            },
            groups={},
            disable_geoip=None,
        )
