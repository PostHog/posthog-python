import json
import unittest
from datetime import date, datetime

import mock
import pytest
import requests

import posthog.request as request_module
from posthog.request import (
    APIError,
    DatetimeSerializer,
    GetResponse,
    KEEP_ALIVE_SOCKET_OPTIONS,
    QuotaLimitError,
    _mask_tokens_in_url,
    batch_post,
    decide,
    determine_server_host,
    disable_connection_reuse,
    enable_keep_alive,
    flags,
    get,
    set_socket_options,
)
from posthog.test.test_utils import TEST_API_KEY


@pytest.mark.parametrize(
    "url, expected",
    [
        # Token with params after - masks keeping first 10 chars
        (
            "https://example.com/api/flags?token=phc_abc123xyz789&send_cohorts",
            "https://example.com/api/flags?token=phc_abc123...&send_cohorts",
        ),
        # Token at end of URL
        (
            "https://example.com/api/flags?token=phc_abc123xyz789",
            "https://example.com/api/flags?token=phc_abc123...",
        ),
        # No token - unchanged
        (
            "https://example.com/api/flags?other=value",
            "https://example.com/api/flags?other=value",
        ),
        # Short token (<10 chars) - unchanged
        (
            "https://example.com/api/flags?token=short",
            "https://example.com/api/flags?token=short",
        ),
        # Exactly 10 char token - gets ellipsis
        (
            "https://example.com/api/flags?token=1234567890",
            "https://example.com/api/flags?token=1234567890...",
        ),
    ],
)
def test_mask_tokens_in_url(url, expected):
    assert _mask_tokens_in_url(url) == expected


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
        mock_response._content = json.dumps({"flags": [{"key": "test-flag"}]}).encode(
            "utf-8"
        )
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
        self.assertTrue(
            call_kwargs["headers"]["User-Agent"].startswith("posthog-python/")
        )

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


def test_enable_keep_alive_sets_socket_options():
    try:
        enable_keep_alive()
        from posthog.request import _session

        adapter = _session.get_adapter("https://example.com")
        assert adapter.socket_options == KEEP_ALIVE_SOCKET_OPTIONS
    finally:
        set_socket_options(None)


def test_set_socket_options_clears_with_none():
    try:
        enable_keep_alive()
        set_socket_options(None)
        from posthog.request import _session

        adapter = _session.get_adapter("https://example.com")
        assert adapter.socket_options is None
    finally:
        set_socket_options(None)


def test_disable_connection_reuse_creates_fresh_sessions():
    try:
        disable_connection_reuse()
        session1 = request_module._get_session()
        session2 = request_module._get_session()
        assert session1 is not session2
    finally:
        request_module._pooling_enabled = True


def test_set_socket_options_is_idempotent():
    try:
        enable_keep_alive()
        session1 = request_module._session
        enable_keep_alive()
        session2 = request_module._session
        assert session1 is session2
    finally:
        set_socket_options(None)


class TestFlagsSession(unittest.TestCase):
    """Tests for flags session configuration."""

    def test_retry_status_forcelist_excludes_rate_limits(self):
        """Verify 429 (rate limit) is NOT retried - need to wait, not hammer."""
        from posthog.request import RETRY_STATUS_FORCELIST

        self.assertNotIn(429, RETRY_STATUS_FORCELIST)

    def test_retry_status_forcelist_excludes_quota_errors(self):
        """Verify 402 (payment required/quota) is NOT retried - won't resolve."""
        from posthog.request import RETRY_STATUS_FORCELIST

        self.assertNotIn(402, RETRY_STATUS_FORCELIST)

    @mock.patch("posthog.request._get_flags_session")
    def test_flags_uses_flags_session(self, mock_get_flags_session):
        """flags() uses the dedicated flags session, not the general session."""
        mock_response = requests.Response()
        mock_response.status_code = 200
        mock_response._content = json.dumps(
            {
                "featureFlags": {"test-flag": True},
                "featureFlagPayloads": {},
                "errorsWhileComputingFlags": False,
            }
        ).encode("utf-8")

        mock_session = mock.MagicMock()
        mock_session.post.return_value = mock_response
        mock_get_flags_session.return_value = mock_session

        result = flags("test-key", "https://test.posthog.com", distinct_id="user123")

        self.assertEqual(result["featureFlags"]["test-flag"], True)
        mock_get_flags_session.assert_called_once()
        mock_session.post.assert_called_once()

    @mock.patch("posthog.request._get_flags_session")
    def test_flags_no_retry_on_quota_limit(self, mock_get_flags_session):
        """flags() raises QuotaLimitError without retrying (at application level)."""
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

        mock_session = mock.MagicMock()
        mock_session.post.return_value = mock_response
        mock_get_flags_session.return_value = mock_session

        with self.assertRaises(QuotaLimitError):
            flags("test-key", "https://test.posthog.com", distinct_id="user123")

        # QuotaLimitError is raised after response is received, not retried
        self.assertEqual(mock_session.post.call_count, 1)


class TestFlagsSessionNetworkRetries(unittest.TestCase):
    """Tests for network failure retries in the flags session."""

    def test_flags_session_retry_config_includes_connection_errors(self):
        """
        Verify that the flags session is configured to retry on connection errors.

        The urllib3 Retry adapter with connect=2 and read=2 automatically
        retries on network-level failures (DNS failures, connection refused,
        connection reset, etc.) up to 2 times each.
        """
        from posthog.request import _build_flags_session

        session = _build_flags_session()

        # Get the adapter for https://
        adapter = session.get_adapter("https://test.posthog.com")

        # Verify retry configuration
        retry = adapter.max_retries
        self.assertEqual(retry.total, 2, "Should have 2 total retries")
        self.assertEqual(retry.connect, 2, "Should retry connection errors twice")
        self.assertEqual(retry.read, 2, "Should retry read errors twice")
        self.assertIn("POST", retry.allowed_methods, "Should allow POST retries")

    def test_flags_session_retries_on_server_errors(self):
        """
        Verify that transient server errors (5xx) trigger retries.

        This tests the status_forcelist configuration which specifies
        which HTTP status codes should trigger a retry.
        """
        from posthog.request import _build_flags_session, RETRY_STATUS_FORCELIST

        session = _build_flags_session()
        adapter = session.get_adapter("https://test.posthog.com")
        retry = adapter.max_retries

        # Verify the status codes that trigger retries
        self.assertEqual(
            set(retry.status_forcelist),
            set(RETRY_STATUS_FORCELIST),
            "Should retry on transient server errors",
        )

        # Verify specific codes are included
        self.assertIn(500, retry.status_forcelist)
        self.assertIn(502, retry.status_forcelist)
        self.assertIn(503, retry.status_forcelist)
        self.assertIn(504, retry.status_forcelist)

        # Verify rate limits and quota errors are NOT retried
        self.assertNotIn(429, retry.status_forcelist)
        self.assertNotIn(402, retry.status_forcelist)

    def test_flags_session_has_backoff(self):
        """
        Verify that retries use exponential backoff to avoid thundering herd.
        """
        from posthog.request import _build_flags_session

        session = _build_flags_session()
        adapter = session.get_adapter("https://test.posthog.com")
        retry = adapter.max_retries

        self.assertEqual(
            retry.backoff_factor,
            0.5,
            "Should use 0.5s backoff factor (0.5s, 1s delays)",
        )


class TestFlagsSessionRetryIntegration(unittest.TestCase):
    """Integration tests that verify actual retry behavior with a local server."""

    def test_retries_on_503_then_succeeds(self):
        """
        Verify that 503 errors trigger retries and eventually succeed.

        Uses a local HTTP server that fails twice with 503, then succeeds.
        This tests the full retry flow including backoff timing.
        """
        import threading
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from socketserver import ThreadingMixIn
        from urllib3.util.retry import Retry
        from posthog.request import HTTPAdapterWithSocketOptions, RETRY_STATUS_FORCELIST

        request_count = 0

        class RetryTestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                nonlocal request_count
                request_count += 1

                # Read and discard request body to prevent connection issues
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length > 0:
                    self.rfile.read(content_length)

                if request_count <= 2:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    body = b'{"error": "Service unavailable"}'
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    body = (
                        b'{"featureFlags": {"test": true}, "featureFlagPayloads": {}}'
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, format, *args):
                pass  # Suppress logging

        # Use ThreadingMixIn for cleaner shutdown
        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        # Start server on a random available port
        server = ThreadedHTTPServer(("127.0.0.1", 0), RetryTestHandler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()

        try:
            # Build session with same retry config as _build_flags_session
            # but mounted on http:// for local testing
            adapter = HTTPAdapterWithSocketOptions(
                max_retries=Retry(
                    total=2,
                    connect=2,
                    read=2,
                    backoff_factor=0.01,  # Fast backoff for testing
                    status_forcelist=RETRY_STATUS_FORCELIST,
                    allowed_methods=["POST"],
                ),
            )
            session = requests.Session()
            session.mount("http://", adapter)

            response = session.post(
                f"http://127.0.0.1:{port}/flags/?v=2",
                json={"distinct_id": "user123"},
                timeout=5,
            )

            # Should succeed on 3rd attempt
            self.assertEqual(response.status_code, 200)
            self.assertEqual(request_count, 3)  # 1 initial + 2 retries
        finally:
            server.shutdown()
            server.server_close()

    def test_connection_errors_are_retried(self):
        """
        Verify that connection errors (no server) trigger retries.

        Binds a socket to get a guaranteed available port, then closes it
        so connection attempts fail with ConnectionError.
        """
        import socket
        import time
        from urllib3.util.retry import Retry
        from posthog.request import HTTPAdapterWithSocketOptions, RETRY_STATUS_FORCELIST

        # Get an available port by binding then closing a socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # Port is now available but nothing is listening

        adapter = HTTPAdapterWithSocketOptions(
            max_retries=Retry(
                total=2,
                connect=2,
                read=2,
                backoff_factor=0.05,  # Very fast for testing
                status_forcelist=RETRY_STATUS_FORCELIST,
                allowed_methods=["POST"],
            ),
        )
        session = requests.Session()
        session.mount("http://", adapter)

        start = time.time()
        with self.assertRaises(requests.exceptions.ConnectionError):
            session.post(
                f"http://127.0.0.1:{port}/flags/?v=2",
                json={"distinct_id": "user123"},
                timeout=1,
            )
        elapsed = time.time() - start

        # With 3 attempts and backoff, should take more than instant
        # but less than timeout (confirms retries happened)
        self.assertGreater(elapsed, 0.05, "Should have some delay from retries")
