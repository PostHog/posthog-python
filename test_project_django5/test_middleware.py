"""
Tests for PostHog Django middleware in async context.

These tests verify that the middleware correctly handles:
1. Async user access (request.auser() in Django 5)
2. Exception capture in both sync and async views
3. No SynchronousOnlyOperation errors in async context

Tests run directly against the ASGI application without needing a server.
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
async def test_async_user_access(asgi_app):
    """
    Test that middleware can access request.user in async context.

    In Django 5, this requires using await request.auser() instead of request.user
    to avoid SynchronousOnlyOperation error.

    Without authentication, request.user is AnonymousUser which doesn't
    trigger the lazy loading bug. This test verifies the middleware works
    in the common case.
    """
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
        response = await ac.get("/test/async-user")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "django_version" in data


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_async_authenticated_user_access(asgi_app):
    """
    Test that middleware can access an authenticated user in async context.

    This is the critical test that triggers the SynchronousOnlyOperation bug
    in v6.7.11. When AuthenticationMiddleware sets request.user to a
    SimpleLazyObject wrapping a database query, accessing user.pk or user.email
    in async context causes the error.

    In v6.7.11, extract_request_user() does getattr(user, "is_authenticated", False)
    which triggers the lazy object evaluation synchronously.

    The fix uses await request.auser() instead to avoid this.
    """
    from django.contrib.auth import get_user_model
    from django.test import Client
    from asgiref.sync import sync_to_async
    from django.test import override_settings

    # Create a test user (must use sync_to_async since we're in async test)
    User = get_user_model()

    @sync_to_async
    def create_or_get_user():
        user, created = User.objects.get_or_create(
            username='testuser',
            defaults={
                'email': 'test@example.com',
            }
        )
        if created:
            user.set_password('testpass123')
            user.save()
        return user

    user = await create_or_get_user()

    # Create a session with authenticated user (sync operation)
    @sync_to_async
    def create_session():
        client = Client()
        client.force_login(user)
        return client.cookies.get('sessionid')

    session_cookie = await create_session()

    if not session_cookie:
        pytest.skip("Could not create authenticated session")

    # Make request with session cookie - this should trigger the bug in v6.7.11
    # Disable exception capture to see the SynchronousOnlyOperation clearly
    with override_settings(POSTHOG_MW_CAPTURE_EXCEPTIONS=False):
        async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
            response = await ac.get(
                "/test/async-user",
                cookies={"sessionid": session_cookie.value}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["user_authenticated"] == True


@pytest.mark.asyncio
async def test_sync_user_access(asgi_app):
    """
    Test that middleware works with sync views.

    This should always work regardless of middleware version.
    """
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
        response = await ac.get("/test/sync-user")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"


@pytest.mark.asyncio
async def test_async_exception_capture(asgi_app):
    """
    Test that middleware handles exceptions from async views.

    The middleware's process_exception() method captures view exceptions to PostHog
    before Django converts them to 500 responses. This test verifies the exception
    causes a 500 response. See test_exception_capture.py for tests that verify
    actual exception capture to PostHog.
    """
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
        response = await ac.get("/test/async-exception")

    # Django returns 500 for unhandled exceptions
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_sync_exception_capture(asgi_app):
    """
    Test that middleware handles exceptions from sync views.

    The middleware's process_exception() method captures view exceptions to PostHog.
    This test verifies the exception causes a 500 response.
    """
    async with AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver") as ac:
        response = await ac.get("/test/sync-exception")

    # Django returns 500 for unhandled exceptions
    assert response.status_code == 500
