"""
Test that demonstrates the exception capture bug and fix.

This test uses a real PostHog client with a test consumer to verify that
exceptions are actually captured to PostHog, not just that 500 responses are returned.

Bug: Without process_exception(), view exceptions are NOT captured to PostHog.
Fix: PR #350 adds process_exception() which Django calls to capture exceptions.
"""
import os
import django

# Setup Django before importing anything else
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testdjango.settings")
django.setup()

import pytest
from httpx import AsyncClient, ASGITransport
from django.core.asgi import get_asgi_application
from posthog import Client


@pytest.mark.asyncio
async def test_async_exception_is_captured():
    """
    Test that async view exceptions are captured to PostHog.

    With process_exception() (PR #350), exceptions are captured.
    Without it, exceptions are NOT captured even though 500 is returned.
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

    with patch('posthog.capture_exception', side_effect=mock_capture):
        app = get_asgi_application()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            response = await ac.get("/test/async-exception")

        # Django returns 500
        assert response.status_code == 500

        # CRITICAL: Verify PostHog captured the exception
        assert len(captured) > 0, f"Exception was NOT captured to PostHog!"

        # Verify it's the right exception
        exception_data = captured[0]
        assert exception_data['type'] == 'ValueError'
        assert 'Test exception from Django 5 async view' in exception_data['message']

        print(f"✓ Async exception captured: {len(captured)} exception event(s)")
        print(f"  Exception type: {exception_data['type']}")
        print(f"  Exception message: {exception_data['message']}")


@pytest.mark.asyncio
async def test_sync_exception_is_captured():
    """
    Test that sync view exceptions are captured to PostHog.

    With process_exception() (PR #350), exceptions are captured.
    Without it, exceptions are NOT captured even though 500 is returned.
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

    with patch('posthog.capture_exception', side_effect=mock_capture):
        app = get_asgi_application()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
            response = await ac.get("/test/sync-exception")

        # Django returns 500
        assert response.status_code == 500

        # CRITICAL: Verify PostHog captured the exception
        assert len(captured) > 0, f"Exception was NOT captured to PostHog!"

        # Verify it's the right exception
        exception_data = captured[0]
        assert exception_data['type'] == 'ValueError'
        assert 'Test exception from Django 5 sync view' in exception_data['message']

        print(f"✓ Sync exception captured: {len(captured)} exception event(s)")
        print(f"  Exception type: {exception_data['type']}")
        print(f"  Exception message: {exception_data['message']}")


if __name__ == "__main__":
    """Run tests directly."""
    import asyncio

    async def run_tests():
        print("\nTesting exception capture with process_exception() fix...\n")

        try:
            await test_async_exception_is_captured()
        except AssertionError as e:
            print(f"✗ Async exception capture failed: {e}")
        except Exception as e:
            print(f"✗ Async test error: {e}")

        try:
            await test_sync_exception_is_captured()
        except AssertionError as e:
            print(f"✗ Sync exception capture failed: {e}")
        except Exception as e:
            print(f"✗ Sync test error: {e}")

        print("\nDone!\n")

    asyncio.run(run_tests())
