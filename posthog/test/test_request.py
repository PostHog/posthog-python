import json
import unittest
from datetime import date, datetime
from unittest.mock import patch, MagicMock
from parameterized import parameterized

import mock
import pytest
import requests

from posthog.request import (
    DatetimeSerializer,
    QuotaLimitError,
    batch_post,
    decide,
    determine_server_host,
    normalize_decide_response,
)
from posthog.test.test_utils import TEST_API_KEY
from posthog.types import FeatureFlag, FlagMetadata, FlagReason, LegacyFlagMetadata


class TestRequests(unittest.TestCase):
    def test_valid_request(self):
        res = batch_post(TEST_API_KEY, batch=[{"distinct_id": "distinct_id", "event": "python event", "type": "track"}])
        self.assertEqual(res.status_code, 200)

    def test_invalid_request_error(self):
        self.assertRaises(Exception, batch_post, "testsecret", "https://t.posthog.com", False, "[{]")

    def test_invalid_host(self):
        self.assertRaises(Exception, batch_post, "testsecret", "t.posthog.com/", batch=[])

    def test_datetime_serialization(self):
        data = {"created": datetime(2012, 3, 4, 5, 6, 7, 891011)}
        result = json.dumps(data, cls=DatetimeSerializer)
        self.assertEqual(result, '{"created": "2012-03-04T05:06:07.891011"}')

    def test_date_serialization(self):
        today = date.today()
        data = {"created": today}
        result = json.dumps(data, cls=DatetimeSerializer)
        expected = '{"created": "%s"}' % today.isoformat()
        self.assertEqual(result, expected)

    def test_should_not_timeout(self):
        res = batch_post(
            TEST_API_KEY, batch=[{"distinct_id": "distinct_id", "event": "python event", "type": "track"}], timeout=15
        )
        self.assertEqual(res.status_code, 200)

    def test_should_timeout(self):
        with self.assertRaises(requests.ReadTimeout):
            batch_post(
                "key", batch=[{"distinct_id": "distinct_id", "event": "python event", "type": "track"}], timeout=0.0001
            )

    def test_quota_limited_response(self):
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps(
            {
                "quotaLimited": ["feature_flags"],
                "featureFlags": {},
                "featureFlagPayloads": {},
                "errorsWhileComputingFlags": False,
            }
        ).encode("utf-8")

        with mock.patch("posthog.request._session.post", return_value=mock_response):
            with self.assertRaises(QuotaLimitError) as cm:
                decide("fake_key", "fake_host")

            self.assertEqual(cm.exception.status, 200)
            self.assertEqual(cm.exception.message, "Feature flags quota limited")

    def test_normal_decide_response(self):
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps(
            {"featureFlags": {"flag1": True}, "featureFlagPayloads": {}, "errorsWhileComputingFlags": False}
        ).encode("utf-8")

        with mock.patch("posthog.request._session.post", return_value=mock_response):
            response = decide("fake_key", "fake_host")
            self.assertEqual(response["featureFlags"], {"flag1": True})

    @parameterized.expand([(True,), (False,)])
    def test_normalize_decide_response_v4(self, has_errors: bool):
        resp = {
            "flags": {
                "my-flag": FeatureFlag(
                    key="my-flag",
                    enabled=True,
                    variant="test-variant",
                    reason=FlagReason(
                        code="matched_condition", condition_index=0, description="Matched condition set 1"
                    ),
                    metadata=FlagMetadata(id=1, payload='{"some": "json"}', version=2, description="test-description"),
                )
            },
            "errorsWhileComputingFlags": has_errors,
            "requestId": "test-id",
        }

        result = normalize_decide_response(resp)
        
        flag = result["flags"]["my-flag"]
        self.assertEqual(flag.key, "my-flag")
        self.assertTrue(flag.enabled)
        self.assertEqual(flag.variant, "test-variant")
        self.assertEqual(flag.get_value(), "test-variant")
        self.assertEqual(
            flag.reason, FlagReason(code="matched_condition", condition_index=0, description="Matched condition set 1")
        )
        self.assertEqual(
            flag.metadata, FlagMetadata(id=1, payload='{"some": "json"}', version=2, description="test-description")
        )
        self.assertEqual(result["errorsWhileComputingFlags"], has_errors)
        self.assertEqual(result["requestId"], "test-id")

    def test_normalize_decide_response_legacy(self):
        # Test legacy response format with "featureFlags" and "featureFlagPayloads"
        resp = {
            "featureFlags": {"my-flag": "test-variant"},
            "featureFlagPayloads": {"my-flag": "{\"some\": \"json-payload\"}"},
            "errorsWhileComputingFlags": False,
            "requestId": "test-id",
        }

        result = normalize_decide_response(resp)

        flag = result["flags"]["my-flag"]
        self.assertEqual(flag.key, "my-flag")
        self.assertTrue(flag.enabled)
        self.assertEqual(flag.variant, "test-variant")
        self.assertEqual(flag.get_value(), "test-variant")
        self.assertIsNone(flag.reason)
        self.assertEqual(
            flag.metadata, LegacyFlagMetadata(payload='{"some": "json-payload"}')
        )
        self.assertFalse(result["errorsWhileComputingFlags"])
        self.assertEqual(result["requestId"], "test-id")
        # Verify legacy fields are removed
        self.assertNotIn("featureFlags", result)
        self.assertNotIn("featureFlagPayloads", result)

    def test_normalize_decide_response_boolean_flag(self):
        # Test legacy response with boolean flag
        resp = {
            "featureFlags": {"my-flag": True},
            "errorsWhileComputingFlags": False
        }

        result = normalize_decide_response(resp)

        self.assertIn("requestId", result)
        self.assertIsNone(result["requestId"])

        flag = result["flags"]["my-flag"]
        self.assertEqual(flag.key, "my-flag")
        self.assertTrue(flag.enabled)
        self.assertIsNone(flag.variant)
        self.assertIsNone(flag.reason)
        self.assertEqual(
            flag.metadata, LegacyFlagMetadata(payload=None)
        )
        self.assertFalse(result["errorsWhileComputingFlags"])
        self.assertNotIn("featureFlags", result)
        self.assertNotIn("featureFlagPayloads", result)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("https://t.posthog.com", "https://t.posthog.com"),
        ("https://t.posthog.com/", "https://t.posthog.com/"),
        ("t.posthog.com", "t.posthog.com"),
        ("t.posthog.com/", "t.posthog.com/"),
        ("https://us.posthog.com.rg.proxy.com", "https://us.posthog.com.rg.proxy.com"),
        ("app.posthog.com", "app.posthog.com"),
        ("eu.posthog.com", "eu.posthog.com"),
        ("https://app.posthog.com", "https://us.i.posthog.com"),
        ("https://eu.posthog.com", "https://eu.i.posthog.com"),
        ("https://us.posthog.com", "https://us.i.posthog.com"),
        ("https://app.posthog.com/", "https://us.i.posthog.com"),
        ("https://eu.posthog.com/", "https://eu.i.posthog.com"),
        ("https://us.posthog.com/", "https://us.i.posthog.com"),
        (None, "https://us.i.posthog.com"),
    ],
)
def test_routing_to_custom_host(host, expected):
    assert determine_server_host(host) == expected
