import json
import unittest
from datetime import date, datetime

import mock
import pytest
import requests

from posthog.request import (
    APIError,
    DatetimeSerializer,
    GetResponse,
    QuotaLimitError,
    batch_post,
    decide,
    determine_server_host,
    get,
)
from posthog.test.test_utils import TEST_API_KEY


class TestRequests(unittest.TestCase):
    def test_valid_request(self):
        res = batch_post(
            TEST_API_KEY,
            batch=[
                {"distinct_id": "distinct_id", "event": "python event", "type": "track"}
            ],
        )
        self.assertEqual(res.status_code, 200)

    def test_invalid_request_error(self):
        self.assertRaises(
            Exception, batch_post, "testsecret", "https://t.posthog.com", False, "[{]"
        )

    def test_invalid_host(self):
        self.assertRaises(
            Exception, batch_post, "testsecret", "t.posthog.com/", batch=[]
        )

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
            TEST_API_KEY,
            batch=[
                {"distinct_id": "distinct_id", "event": "python event", "type": "track"}
            ],
            timeout=15,
        )
        self.assertEqual(res.status_code, 200)

    def test_should_timeout(self):
        with self.assertRaises(requests.ReadTimeout):
            batch_post(
                "key",
                batch=[
                    {
                        "distinct_id": "distinct_id",
                        "event": "python event",
                        "type": "track",
                    }
                ],
                timeout=0.0001,
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
            {
                "featureFlags": {"flag1": True},
                "featureFlagPayloads": {},
                "errorsWhileComputingFlags": False,
            }
        ).encode("utf-8")

        with mock.patch("posthog.request._session.post", return_value=mock_response):
            response = decide("fake_key", "fake_host")
            self.assertEqual(response["featureFlags"], {"flag1": True})


class TestGet(unittest.TestCase):
    """Unit tests for the get() function HTTP-level behavior."""

    @mock.patch("posthog.request._session.get")
    def test_get_returns_data_and_etag(self, mock_get):
        """Test that get() returns GetResponse with data and etag from headers."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response.headers["ETag"] = '"abc123"'
        mock_response._content = json.dumps(
            {"flags": [{"key": "test-flag"}]}
        ).encode("utf-8")
        mock_get.return_value = mock_response

        response = get("api_key", "/test-url", host="https://example.com")

        self.assertIsInstance(response, GetResponse)
        self.assertEqual(response.data, {"flags": [{"key": "test-flag"}]})
        self.assertEqual(response.etag, '"abc123"')
        self.assertFalse(response.not_modified)

    @mock.patch("posthog.request._session.get")
    def test_get_sends_if_none_match_header_when_etag_provided(self, mock_get):
        """Test that If-None-Match header is sent when etag parameter is provided."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response.headers["ETag"] = '"new-etag"'
        mock_response._content = json.dumps({"flags": []}).encode("utf-8")
        mock_get.return_value = mock_response

        get("api_key", "/test-url", host="https://example.com", etag='"previous-etag"')

        call_kwargs = mock_get.call_args[1]
        self.assertEqual(call_kwargs["headers"]["If-None-Match"], '"previous-etag"')

    @mock.patch("posthog.request._session.get")
    def test_get_does_not_send_if_none_match_when_no_etag(self, mock_get):
        """Test that If-None-Match header is not sent when no etag provided."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({"flags": []}).encode("utf-8")
        mock_get.return_value = mock_response

        get("api_key", "/test-url", host="https://example.com")

        call_kwargs = mock_get.call_args[1]
        self.assertNotIn("If-None-Match", call_kwargs["headers"])

    @mock.patch("posthog.request._session.get")
    def test_get_handles_304_not_modified(self, mock_get):
        """Test that 304 Not Modified response returns not_modified=True with no data."""
        mock_response = requests.Response()
        mock_response.status_code = 304
        mock_response.headers["ETag"] = '"unchanged-etag"'
        mock_get.return_value = mock_response

        response = get(
            "api_key", "/test-url", host="https://example.com", etag='"unchanged-etag"'
        )

        self.assertIsInstance(response, GetResponse)
        self.assertIsNone(response.data)
        self.assertEqual(response.etag, '"unchanged-etag"')
        self.assertTrue(response.not_modified)

    @mock.patch("posthog.request._session.get")
    def test_get_304_without_etag_header_uses_request_etag(self, mock_get):
        """Test that 304 response without ETag header falls back to request etag."""
        mock_response = requests.Response()
        mock_response.status_code = 304
        # Server doesn't return ETag header on 304
        mock_get.return_value = mock_response

        response = get(
            "api_key", "/test-url", host="https://example.com", etag='"original-etag"'
        )

        self.assertTrue(response.not_modified)
        self.assertEqual(response.etag, '"original-etag"')

    @mock.patch("posthog.request._session.get")
    def test_get_200_without_etag_header(self, mock_get):
        """Test that 200 response without ETag header returns None for etag."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({"flags": []}).encode("utf-8")
        # No ETag header
        mock_get.return_value = mock_response

        response = get("api_key", "/test-url", host="https://example.com")

        self.assertFalse(response.not_modified)
        self.assertIsNone(response.etag)
        self.assertEqual(response.data, {"flags": []})

    @mock.patch("posthog.request._session.get")
    def test_get_error_response_raises_api_error(self, mock_get):
        """Test that error responses raise APIError."""
        mock_response = requests.Response()
        mock_response.status_code = 401
        mock_response._content = json.dumps({"detail": "Unauthorized"}).encode("utf-8")
        mock_get.return_value = mock_response

        with self.assertRaises(APIError) as ctx:
            get("bad_key", "/test-url", host="https://example.com")

        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.message, "Unauthorized")

    @mock.patch("posthog.request._session.get")
    def test_get_sends_authorization_header(self, mock_get):
        """Test that Authorization header is sent with Bearer token."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({}).encode("utf-8")
        mock_get.return_value = mock_response

        get("my-api-key", "/test-url", host="https://example.com")

        call_kwargs = mock_get.call_args[1]
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer my-api-key")

    @mock.patch("posthog.request._session.get")
    def test_get_sends_user_agent_header(self, mock_get):
        """Test that User-Agent header is sent."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({}).encode("utf-8")
        mock_get.return_value = mock_response

        get("api_key", "/test-url", host="https://example.com")

        call_kwargs = mock_get.call_args[1]
        self.assertIn("User-Agent", call_kwargs["headers"])
        self.assertTrue(call_kwargs["headers"]["User-Agent"].startswith("posthog-python/"))

    @mock.patch("posthog.request._session.get")
    def test_get_passes_timeout(self, mock_get):
        """Test that timeout parameter is passed to the request."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({}).encode("utf-8")
        mock_get.return_value = mock_response

        get("api_key", "/test-url", host="https://example.com", timeout=30)

        call_kwargs = mock_get.call_args[1]
        self.assertEqual(call_kwargs["timeout"], 30)

    @mock.patch("posthog.request._session.get")
    def test_get_constructs_full_url(self, mock_get):
        """Test that host and url are combined correctly."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({}).encode("utf-8")
        mock_get.return_value = mock_response

        get("api_key", "/api/flags", host="https://example.com")

        call_args = mock_get.call_args[0]
        self.assertEqual(call_args[0], "https://example.com/api/flags")

    @mock.patch("posthog.request._session.get")
    def test_get_removes_trailing_slash_from_host(self, mock_get):
        """Test that trailing slash is removed from host."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps({}).encode("utf-8")
        mock_get.return_value = mock_response

        get("api_key", "/api/flags", host="https://example.com/")

        call_args = mock_get.call_args[0]
        self.assertEqual(call_args[0], "https://example.com/api/flags")


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
