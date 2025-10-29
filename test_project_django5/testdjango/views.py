"""
Test views for validating PostHog middleware with Django 5 ASGI.
"""
from django.http import JsonResponse


async def test_async_user(request):
    """
    Async view that tests middleware with request.user access.

    The middleware will access request.user (SimpleLazyObject) via auser()
    in async context. Without the fix, this causes SynchronousOnlyOperation.
    """
    # The middleware has already accessed request.user via auser()
    # If we got here, the fix works!
    user = await request.auser()

    return JsonResponse({
        "status": "success",
        "message": "Django 5 async middleware test passed!",
        "django_version": "5.x",
        "user_authenticated": user.is_authenticated if user else False,
        "note": "Middleware used await request.auser() successfully"
    })


def test_sync_user(request):
    """Sync view for comparison."""
    return JsonResponse({
        "status": "success",
        "message": "Sync view works",
        "user_authenticated": request.user.is_authenticated if hasattr(request, 'user') else False
    })


async def test_async_exception(request):
    """Async view that raises an exception for testing exception capture."""
    raise ValueError("Test exception from Django 5 async view")


def test_sync_exception(request):
    """Sync view that raises an exception for testing exception capture."""
    raise ValueError("Test exception from Django 5 sync view")
