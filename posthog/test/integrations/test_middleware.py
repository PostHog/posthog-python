import unittest
from unittest.mock import Mock

from posthog.integrations.django import PosthogContextMiddleware


class MockRequest:
    """Mock Django HttpRequest object"""

    def __init__(
        self, meta=None, method="GET", path="/test", host="example.com", is_secure=False
    ):
        self.META = meta or {}
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
    ):
        """Helper to create middleware instance without calling __init__"""
        middleware = PosthogContextMiddleware.__new__(PosthogContextMiddleware)
        middleware.get_response = Mock()
        middleware.extra_tags = extra_tags
        middleware.request_filter = request_filter
        middleware.tag_map = tag_map
        middleware.capture_exceptions = capture_exceptions
        return middleware

    def test_extract_tags_basic(self):
        """Test basic tag extraction from request"""
        middleware = self.create_middleware()
        request = MockRequest(
            meta={
                "HTTP_X_POSTHOG_SESSION_ID": "session-123",
                "HTTP_X_POSTHOG_DISTINCT_ID": "user-456",
            },
            method="POST",
            path="/api/test",
            host="example.com",
            is_secure=True,
        )

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$session_id"], "session-123")
        self.assertEqual(tags["$distinct_id"], "user-456")
        self.assertEqual(tags["$current_url"], "https://example.com/api/test")
        self.assertEqual(tags["request_method"], "POST")

    def test_extract_tags_missing_headers(self):
        """Test tag extraction when PostHog headers are missing"""
        middleware = self.create_middleware()
        request = MockRequest(meta={}, method="GET", path="/home")

        tags = middleware.extract_tags(request)

        self.assertNotIn("$session_id", tags)
        self.assertNotIn("$distinct_id", tags)
        self.assertEqual(tags["$current_url"], "http://example.com/home")
        self.assertEqual(tags["request_method"], "GET")

    def test_extract_tags_partial_headers(self):
        """Test tag extraction with only some PostHog headers present"""
        middleware = self.create_middleware()
        request = MockRequest(
            meta={"HTTP_X_POSTHOG_SESSION_ID": "session-only"}, method="PUT"
        )

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$session_id"], "session-only")
        self.assertNotIn("$distinct_id", tags)
        self.assertEqual(tags["request_method"], "PUT")

    def test_extract_tags_with_extra_tags(self):
        """Test tag extraction with extra_tags function"""

        def extra_tags_func(request):
            return {"custom_tag": "custom_value", "user_id": "789"}

        middleware = self.create_middleware(extra_tags=extra_tags_func)
        request = MockRequest(
            meta={"HTTP_X_POSTHOG_SESSION_ID": "session-123"}, method="GET"
        )

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$session_id"], "session-123")
        self.assertEqual(tags["custom_tag"], "custom_value")
        self.assertEqual(tags["user_id"], "789")

    def test_extract_tags_with_tag_map(self):
        """Test tag extraction with tag_map function"""

        def tag_map_func(tags):
            # Remove session_id and add a mapped version
            if "$session_id" in tags:
                tags["mapped_session"] = f"mapped_{tags['$session_id']}"
                del tags["$session_id"]
            return tags

        middleware = self.create_middleware(tag_map=tag_map_func)
        request = MockRequest(
            meta={"HTTP_X_POSTHOG_SESSION_ID": "session-123"}, method="GET"
        )

        tags = middleware.extract_tags(request)

        self.assertNotIn("$session_id", tags)
        self.assertEqual(tags["mapped_session"], "mapped_session-123")

    def test_extract_tags_with_both_extra_and_map(self):
        """Test tag extraction with both extra_tags and tag_map"""

        def extra_tags_func(request):
            return {"extra": "value"}

        def tag_map_func(tags):
            tags["modified"] = True
            return tags

        middleware = self.create_middleware(
            extra_tags=extra_tags_func, tag_map=tag_map_func
        )
        request = MockRequest(
            meta={"HTTP_X_POSTHOG_DISTINCT_ID": "user-123"}, method="DELETE"
        )

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$distinct_id"], "user-123")
        self.assertEqual(tags["extra"], "value")
        self.assertEqual(tags["modified"], True)
        self.assertEqual(tags["request_method"], "DELETE")

    def test_extract_tags_extra_tags_returns_none(self):
        """Test tag extraction when extra_tags returns None"""

        def extra_tags_func(request):
            return None

        middleware = self.create_middleware(extra_tags=extra_tags_func)
        request = MockRequest(method="GET")

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["request_method"], "GET")
        # Should not crash when extra_tags returns None

    def test_extract_tags_extra_tags_returns_empty_dict(self):
        """Test tag extraction when extra_tags returns empty dict"""

        def extra_tags_func(request):
            return {}

        middleware = self.create_middleware(extra_tags=extra_tags_func)
        request = MockRequest(method="PATCH")

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["request_method"], "PATCH")

    def test_extract_tags_url_encoding(self):
        """Test URL building with different scenarios"""
        middleware = self.create_middleware()

        # Test with query parameters in path
        request = MockRequest(
            path="/search?q=test&page=1", host="api.example.com", is_secure=True
        )

        tags = middleware.extract_tags(request)

        self.assertEqual(
            tags["$current_url"], "https://api.example.com/search?q=test&page=1"
        )

    def test_extract_tags_case_sensitivity(self):
        """Test that header extraction is case sensitive for META keys"""
        middleware = self.create_middleware()
        request = MockRequest(
            meta={
                "HTTP_X_POSTHOG_SESSION_ID": "correct-session",
                "http_x_posthog_session_id": "wrong-session",  # lowercase won't match
            }
        )

        tags = middleware.extract_tags(request)

        self.assertEqual(tags["$session_id"], "correct-session")


if __name__ == "__main__":
    unittest.main()
