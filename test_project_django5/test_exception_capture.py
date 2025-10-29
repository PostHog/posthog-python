"""
Test that verifies exception capture functionality.

These tests verify that exceptions are actually captured to PostHog, not just that
500 responses are returned.

Without process_exception(), view exceptions are NOT captured to PostHog (v6.7.11 and earlier).
With process_exception(), Django calls this method to capture exceptions before
converting them to 500 responses.
"""
import os
import django

# Setup Django before importing anything else
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testdjango.settings")
django.setup()

import pytest
from httpx import AsyncClient, ASGITransport
from django.core.asgi import get_asgi_application


@pytest.fixture(scope="session")
def asgi_app():
    """Shared ASGI application for all tests."""
    return get_asgi_application()


@pytest.mark.asyncio
async def test_async_exception_is_captured(asgi_app):
    """
    Test that async view exceptions are captured to PostHog.

    The middleware's process_exception() method ensures exceptions are captured.
    Without it (v6.7.11 and earlier), exceptions are NOT captured even though 500 is returned.
    """
    from unittest.mock import patch

    # Track captured exceptions
    captured = []

    def mock_capture(exception, **kwargs):
        """Mock capture_exception to record calls."""
        captured.append({
            'exception': exception,
            'type': type(exception).__name__,
            'message': str(exception)
        })

    # Patch at the posthog module level where middleware imports from
    with patch('posthog.capture_exception', side_effect=mock_capture):
        async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
            response = await ac.get("/test/async-exception")

        # Django returns 500
        assert response.status_code == 500

        # CRITICAL: Verify PostHog captured the exception
        assert len(captured) > 0, f"Exception was NOT captured to PostHog!"

        # Verify it's the right exception
        exception_data = captured[0]
        assert exception_data['type'] == 'ValueError'
        assert 'Test exception from Django 5 async view' in exception_data['message']


@pytest.mark.asyncio
async def test_sync_exception_is_captured(asgi_app):
    """
    Test that sync view exceptions are captured to PostHog.

    The middleware's process_exception() method ensures exceptions are captured.
    Without it (v6.7.11 and earlier), exceptions are NOT captured even though 500 is returned.
    """
    from unittest.mock import patch

    # Track captured exceptions
    captured = []

    def mock_capture(exception, **kwargs):
        """Mock capture_exception to record calls."""
        captured.append({
            'exception': exception,
            'type': type(exception).__name__,
            'message': str(exception)
        })

    # Patch at the posthog module level where middleware imports from
    with patch('posthog.capture_exception', side_effect=mock_capture):
        async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
            response = await ac.get("/test/sync-exception")

        # Django returns 500
        assert response.status_code == 500

        # CRITICAL: Verify PostHog captured the exception
        assert len(captured) > 0, f"Exception was NOT captured to PostHog!"

        # Verify it's the right exception
        exception_data = captured[0]
        assert exception_data['type'] == 'ValueError'
        assert 'Test exception from Django 5 sync view' in exception_data['message']
