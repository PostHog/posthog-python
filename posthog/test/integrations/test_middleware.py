from posthog.contexts import (
    new_context,
    get_context_session_id,
    get_context_distinct_id,
)
import unittest
from unittest.mock import Mock, patch
import asyncio

# Configure Django settings before importing middleware
import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-secret-key",
        INSTALLED_APPS=[],
        MIDDLEWARE=[],
    )
    django.setup()

from posthog.integrations.django import PosthogContextMiddleware


class MockRequest:
    """Mock Django HttpRequest object"""

    def __init__(
        self,
        headers=None,
        method="GET",
        path="/test",
        host="example.com",
        is_secure=False,
    ):
        self.headers = headers or {}
        self.method = method
        self.path = path
        self._host = host
        self._is_secure = is_secure

    def build_absolute_uri(self):
        scheme = "https" if self._is_secure else "http"
        return f"{scheme}://{self._host}{self.path}"


class TestPosthogContextMiddleware(unittest.TestCase):
    def create_middleware(
        self,
        extra_tags=None,
        request_filter=None,
        tag_map=None,
        capture_exceptions=True,
        get_response=None,
    ):
        """Helper to create middleware instance with mock Django settings"""
        if get_response is None:
            get_response = Mock()

        with patch("django.conf.settings") as mock_settings:
            # Configure mock settings
            mock_settings.POSTHOG_MW_EXTRA_TAGS = extra_tags
            mock_settings.POSTHOG_MW_REQUEST_FILTER = request_filter
            mock_settings.POSTHOG_MW_TAG_MAP = tag_map
            mock_settings.POSTHOG_MW_CAPTURE_EXCEPTIONS = capture_exceptions
            mock_settings.POSTHOG_MW_CLIENT = None

            # Make hasattr work correctly
            def mock_hasattr(obj, name):
                return name in [
                    "POSTHOG_MW_EXTRA_TAGS",
                    "POSTHOG_MW_REQUEST_FILTER",
                    "POSTHOG_MW_TAG_MAP",
                    "POSTHOG_MW_CAPTURE_EXCEPTIONS",
                    "POSTHOG_MW_CLIENT",
                ]

            with patch("builtins.hasattr", side_effect=mock_hasattr):
                middleware = PosthogContextMiddleware(get_response)

        return middleware

    def test_extract_tags_basic(self):
        with new_context():
            """Test basic tag extraction from request"""
            middleware = self.create_middleware()
            request = MockRequest(
                headers={
                    "X-POSTHOG-SESSION-ID": "session-123",
                    "X-POSTHOG-DISTINCT-ID": "user-456",
                },
                method="POST",
                path="/api/test",
                host="example.com",
                is_secure=True,
            )

            tags = middleware.extract_tags(request)

            self.assertEqual(get_context_session_id(), "session-123")
            self.assertEqual(get_context_distinct_id(), "user-456")
            self.assertEqual(tags["$current_url"], "https://example.com/api/test")
            self.assertEqual(tags["$request_method"], "POST")

    def test_extract_tags_missing_headers(self):
        """Test tag extraction when PostHog headers are missing"""

        with new_context():
            middleware = self.create_middleware()
            request = MockRequest(headers={}, method="GET", path="/home")

            tags = middleware.extract_tags(request)

            self.assertIsNone(get_context_session_id())
            self.assertIsNone(get_context_distinct_id())
            self.assertEqual(tags["$current_url"], "http://example.com/home")
            self.assertEqual(tags["$request_method"], "GET")

    def test_extract_tags_partial_headers(self):
        """Test tag extraction with only some PostHog headers present"""

        with new_context():
            middleware = self.create_middleware()
            request = MockRequest(
                headers={"X-POSTHOG-SESSION-ID": "session-only"}, method="PUT"
            )

            tags = middleware.extract_tags(request)

            self.assertEqual(get_context_session_id(), "session-only")
            self.assertIsNone(get_context_distinct_id())
            self.assertEqual(tags["$request_method"], "PUT")

    def test_extract_tags_with_extra_tags(self):
        """Test tag extraction with extra_tags function"""

        def extra_tags_func(request):
            return {"custom_tag": "custom_value", "user_id": "789"}

        with new_context():
            middleware = self.create_middleware(extra_tags=extra_tags_func)
            request = MockRequest(
                headers={"X-POSTHOG-SESSION-ID": "session-123"}, method="GET"
            )

            tags = middleware.extract_tags(request)

            self.assertEqual(get_context_session_id(), "session-123")
            self.assertEqual(tags["custom_tag"], "custom_value")
            self.assertEqual(tags["user_id"], "789")

    def test_extract_tags_with_tag_map(self):
        """Test tag extraction with tag_map function"""

        def extra_tags_func(request):
            return {"custom_tag": "custom_value", "user_id": "789"}

        def tag_map_func(tags):
            if "custom_tag" in tags:
                tags["mapped_custom_tag"] = f"mapped_{tags['custom_tag']}"
                del tags["custom_tag"]
            return tags

        with new_context():
            middleware = self.create_middleware(
                tag_map=tag_map_func, extra_tags=extra_tags_func
            )
            request = MockRequest(
                headers={"X-POSTHOG-SESSION-ID": "session-123"}, method="GET"
            )

            tags = middleware.extract_tags(request)

            self.assertEqual(tags["mapped_custom_tag"], "mapped_custom_value")

    def test_extract_tags_extra_tags_returns_none(self):
        """Test tag extraction when extra_tags returns None"""

        def extra_tags_func(request):
            return None

        middleware = self.create_middleware(extra_tags=extra_tags_func)
        request = MockRequest(method="GET")

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$request_method"], "GET")
        # Should not crash when extra_tags returns None

    def test_extract_tags_extra_tags_returns_empty_dict(self):
        """Test tag extraction when extra_tags returns empty dict"""

        def extra_tags_func(request):
            return {}

        middleware = self.create_middleware(extra_tags=extra_tags_func)
        request = MockRequest(method="PATCH")

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$request_method"], "PATCH")


class TestPosthogContextMiddlewareSync(unittest.TestCase):
    """Test synchronous middleware behavior"""

    def test_sync_middleware_call(self):
        """Test that sync middleware correctly processes requests"""
        mock_response = Mock()
        get_response = Mock(return_value=mock_response)

        # Create middleware with sync get_response
        middleware = PosthogContextMiddleware(get_response)

        # Verify sync mode detected
        self.assertFalse(middleware._is_coroutine)

        request = MockRequest(
            headers={"X-POSTHOG-SESSION-ID": "test-session"},
            method="GET",
            path="/test",
        )

        with new_context():
            response = middleware(request)

            # Verify response returned
            self.assertEqual(response, mock_response)
            get_response.assert_called_once_with(request)

    def test_sync_middleware_with_filter(self):
        """Test sync middleware respects request filter"""
        mock_response = Mock()
        get_response = Mock(return_value=mock_response)

        # Create middleware with request filter that filters all requests
        request_filter = lambda req: False
        middleware = PosthogContextMiddleware.__new__(PosthogContextMiddleware)
        middleware.get_response = get_response
        middleware._is_coroutine = False
        middleware.request_filter = request_filter
        middleware.capture_exceptions = True
        middleware.client = None

        request = MockRequest()

        # Should skip context creation and return response directly
        response = middleware(request)
        self.assertEqual(response, mock_response)
        get_response.assert_called_once_with(request)

    def test_sync_middleware_exception_capture(self):
        """Test that sync middleware captures exceptions during request processing"""
        mock_client = Mock()

        # Make get_response raise an exception
        def raise_exception(request):
            raise ValueError("Test exception")

        get_response = Mock(side_effect=raise_exception)

        # Properly initialize middleware
        middleware = PosthogContextMiddleware(get_response)
        middleware.client = mock_client  # Override with mock client

        request = MockRequest()

        # Should capture exception and re-raise
        with self.assertRaises(ValueError):
            middleware(request)

        # Verify exception was captured by middleware
        mock_client.capture_exception.assert_called_once()
        captured_exception = mock_client.capture_exception.call_args[0][0]
        self.assertIsInstance(captured_exception, ValueError)
        self.assertEqual(str(captured_exception), "Test exception")

    def test_process_exception_integration(self):
        """
        Integration test simulating Django's actual exception handling flow.

        When a view raises an exception:
        1. Middleware.__call__ creates context with request tags
        2. __call__ calls self.get_response(request)
        3. Inside Django's handler: catches exception, checks if middleware.process_exception
           exists (hasattr), calls it if present, returns error response
        4. Context manager exits

        This verifies exception capture works in the real Django flow.
        """
        mock_client = Mock()

        get_response = Mock(return_value=Mock())
        middleware = PosthogContextMiddleware(get_response)
        middleware.client = mock_client

        view_exception = ValueError("View error")
        error_response = Mock(status_code=500)

        def mock_get_response(request):
            # Simulate Django: check if process_exception exists, call it
            if hasattr(middleware, "process_exception"):
                middleware.process_exception(request, view_exception)
            return error_response

        middleware.get_response = mock_get_response

        request = MockRequest(
            headers={"X-POSTHOG-DISTINCT-ID": "user123"},
            method="POST",
            path="/api/test",
        )
        response = middleware(request)

        self.assertEqual(response, error_response)
        mock_client.capture_exception.assert_called_once_with(view_exception)


class TestPosthogContextMiddlewareAsync(unittest.TestCase):
    """Test asynchronous middleware behavior"""

    def test_async_middleware_detection(self):
        """Test that async get_response is correctly detected"""

        async def async_get_response(request):
            return Mock()

        middleware = PosthogContextMiddleware(async_get_response)

        # Verify async mode detected
        self.assertTrue(middleware._is_coroutine)

    def test_async_middleware_call(self):
        """Test that async middleware correctly processes requests"""

        async def run_test():
            mock_response = Mock()

            async def async_get_response(request):
                return mock_response

            middleware = PosthogContextMiddleware(async_get_response)

            request = MockRequest(
                headers={"X-POSTHOG-SESSION-ID": "async-session"},
                method="POST",
                path="/async-test",
            )

            with new_context():
                # Call should return the coroutine from __acall__
                result = middleware(request)

                # Verify it's a coroutine
                self.assertTrue(asyncio.iscoroutine(result))

                # Await the result
                response = await result
                self.assertEqual(response, mock_response)

        asyncio.run(run_test())

    def test_async_middleware_with_filter(self):
        """Test async middleware respects request filter"""

        async def run_test():
            mock_response = Mock()

            async def async_get_response(request):
                return mock_response

            # Properly initialize middleware
            middleware = PosthogContextMiddleware(async_get_response)
            # Override request filter after initialization
            middleware.request_filter = lambda req: False

            request = MockRequest()

            # Should skip context creation and return response directly
            result = middleware(request)
            response = await result
            self.assertEqual(response, mock_response)

        asyncio.run(run_test())

    def test_async_middleware_context_propagation(self):
        """Test that async middleware properly propagates context"""

        async def run_test():
            mock_response = Mock()

            async def async_get_response(request):
                # Verify context is available during async processing
                session_id = get_context_session_id()
                self.assertEqual(session_id, "async-session-123")
                return mock_response

            middleware = PosthogContextMiddleware(async_get_response)

            request = MockRequest(
                headers={"X-POSTHOG-SESSION-ID": "async-session-123"},
                method="GET",
            )

            with new_context():
                result = middleware(request)
                await result

        asyncio.run(run_test())

    def test_async_middleware_exception_capture(self):
        """Test that async middleware captures exceptions during request processing"""

        async def run_test():
            mock_client = Mock()

            # Make async_get_response raise an exception
            async def raise_exception(request):
                raise ValueError("Async test exception")

            # Properly initialize middleware
            middleware = PosthogContextMiddleware(raise_exception)
            middleware.client = mock_client  # Override with mock client

            request = MockRequest()

            # Should capture exception and re-raise
            with self.assertRaises(ValueError):
                result = middleware(request)
                await result

            # Verify exception was captured by middleware
            mock_client.capture_exception.assert_called_once()
            captured_exception = mock_client.capture_exception.call_args[0][0]
            self.assertIsInstance(captured_exception, ValueError)
            self.assertEqual(str(captured_exception), "Async test exception")

        asyncio.run(run_test())


class TestPosthogContextMiddlewareHybrid(unittest.TestCase):
    """Test hybrid middleware behavior with mixed sync/async chains"""

    def test_hybrid_flags_set(self):
        """Test that both capability flags are set"""
        self.assertTrue(PosthogContextMiddleware.sync_capable)
        self.assertTrue(PosthogContextMiddleware.async_capable)

    def test_sync_to_async_routing(self):
        """Test that __call__ routes to __acall__ when async"""

        async def run_test():
            async def async_get_response(request):
                return Mock()

            middleware = PosthogContextMiddleware(async_get_response)

            # Verify routing happens
            request = MockRequest()
            result = middleware(request)

            # Should be a coroutine from __acall__
            self.assertTrue(asyncio.iscoroutine(result))
            await result  # Clean up

        asyncio.run(run_test())

    def test_sync_path_direct_return(self):
        """Test that sync path returns directly without coroutine"""
        mock_response = Mock()

        def sync_get_response(request):
            return mock_response

        middleware = PosthogContextMiddleware(sync_get_response)

        request = MockRequest()
        result = middleware(request)

        # Should NOT be a coroutine
        self.assertFalse(asyncio.iscoroutine(result))
        self.assertEqual(result, mock_response)


if __name__ == "__main__":
    unittest.main()
