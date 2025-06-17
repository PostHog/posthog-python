from posthog.scopes import new_context, get_context_session_id, get_context_distinct_id
import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()
