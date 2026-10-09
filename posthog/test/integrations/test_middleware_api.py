"""Public configuration contract for Django request enrichment."""

import asyncio
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.test import override_settings

from posthog import contexts
from posthog.client import Client
from posthog.integrations.django import PosthogContextMiddleware
from posthog.test.integrations.test_middleware import MockRequest


@pytest.fixture
def client():
    instance = Client("test-key", send=False, enable_exception_autocapture=False)
    yield instance
    instance.shutdown()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("configured", [None, False, True])
def test_capture_configuration_applies_to_context_and_exception_hook(
    client, asynchronous, enabled, configured
):
    client.enable_exception_autocapture = enabled
    error = ValueError("view failed")
    observed = []

    def view(request):
        scope = contexts._get_current_context()
        observed.append(scope.capture_exceptions)
        middleware.process_exception(request, error)
        return "response"

    async def async_view(request):
        return view(request)

    with (
        override_settings(
            POSTHOG_MW_CLIENT=client,
            POSTHOG_MW_CAPTURE_EXCEPTIONS=configured,
        ),
        patch.object(client, "capture_exception") as capture,
    ):
        middleware = PosthogContextMiddleware(async_view if asynchronous else view)
        result = middleware(MockRequest())
        assert (asyncio.run(result) if asynchronous else result) == "response"

    expected = enabled if configured is None else configured
    assert observed == [expected]
    assert capture.call_count == int(expected)


@pytest.mark.parametrize("enabled", [False, True])
def test_capture_none_inherits_global_configuration(enabled):
    with (
        override_settings(POSTHOG_MW_CAPTURE_EXCEPTIONS=None),
        patch("posthog.default_client", None),
        patch("posthog.enable_exception_autocapture", enabled),
        patch("posthog.capture_exception") as capture,
    ):
        middleware = PosthogContextMiddleware(Mock())
        middleware.process_exception(MockRequest(), ValueError("view failed"))
        assert capture.call_count == int(enabled)


def test_capture_none_inherits_initialized_global_client(client):
    with (
        override_settings(POSTHOG_MW_CAPTURE_EXCEPTIONS=None),
        patch("posthog.default_client", client),
        patch("posthog.enable_exception_autocapture", True),
        patch("posthog.capture_exception") as capture,
    ):
        # An initialized global client's setting takes precedence over the module flag.
        middleware = PosthogContextMiddleware(Mock())
        middleware.process_exception(MockRequest(), ValueError("view failed"))
        capture.assert_not_called()


def test_omitted_capture_setting_preserves_legacy_default():
    with override_settings(POSTHOG_MW_CAPTURE_EXCEPTIONS=True):
        del settings.POSTHOG_MW_CAPTURE_EXCEPTIONS
        middleware = PosthogContextMiddleware(Mock())
        assert middleware.capture_exceptions is True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("trust", [False, True])
def test_tracing_headers_require_opt_in_and_user_fallback(asynchronous, trust):
    observed = {}

    def view(request):
        observed["distinct_id"] = contexts.get_context_distinct_id()
        observed["session_id"] = contexts.get_context_session_id()
        return "response"

    async def async_view(request):
        return view(request)

    request = MockRequest(
        headers={
            "X-POSTHOG-DISTINCT-ID": "browser-user",
            "X-POSTHOG-SESSION-ID": "browser-session",
        }
    )
    request.user = Mock(is_authenticated=True, pk=42, email="user@example.com")

    async def auser():
        return request.user

    request.auser = auser
    with override_settings(
        POSTHOG_MW_CAPTURE_EXCEPTIONS=False,
        POSTHOG_MW_TRUST_TRACING_HEADERS=trust,
    ):
        middleware = PosthogContextMiddleware(async_view if asynchronous else view)
        with contexts.new_context(fresh=True, capture_exceptions=False):
            result = middleware(request)
            assert (asyncio.run(result) if asynchronous else result) == "response"

    assert observed == {
        "distinct_id": "browser-user" if trust else "42",
        "session_id": "browser-session" if trust else None,
    }


@pytest.mark.parametrize("asynchronous", [False, True])
def test_context_only_enriches_events_without_redirecting_the_client(
    client, asynchronous
):
    integration_client = Mock(spec=Client)
    captured = []

    def view(request):
        client.capture("custom-event", properties={"tenant": "explicit"})
        middleware.process_exception(request, ValueError("view failed"))
        return "response"

    async def async_view(request):
        return view(request)

    with (
        override_settings(
            POSTHOG_MW_CAPTURE_EXCEPTIONS=False,
            POSTHOG_MW_EXTRA_PROPERTIES=lambda request: {
                "tenant": "context",
                "role": "admin",
            },
        ),
        patch.object(
            client,
            "_enqueue",
            side_effect=lambda message, *args, **kwargs: captured.append(message),
        ),
    ):
        middleware = PosthogContextMiddleware(async_view if asynchronous else view)
        middleware.client = integration_client
        result = middleware(MockRequest(path="/checkout"))
        assert (asyncio.run(result) if asynchronous else result) == "response"

    assert captured[0]["event"] == "custom-event"
    assert captured[0]["properties"]["$request_path"] == "/checkout"
    assert captured[0]["properties"]["tenant"] == "explicit"
    assert captured[0]["properties"]["role"] == "admin"
    integration_client.capture.assert_not_called()
    integration_client.capture_exception.assert_not_called()
    assert contexts.get_tags() == {}


def test_property_settings_take_precedence_over_legacy_aliases():
    with override_settings(
        POSTHOG_MW_EXTRA_PROPERTIES=lambda request: {"new": True},
        POSTHOG_MW_EXTRA_TAGS=lambda request: {"old": True},
        POSTHOG_MW_PROPERTIES_MAP=lambda properties: {**properties, "mapped": "new"},
        POSTHOG_MW_TAG_MAP=lambda properties: {**properties, "mapped": "old"},
    ):
        middleware = PosthogContextMiddleware(Mock())
        properties = middleware.extract_properties(MockRequest())
        assert properties["new"] is True
        assert "old" not in properties
        assert properties["mapped"] == "new"
        assert middleware.extra_tags is middleware.extra_properties
        assert middleware.tag_map is middleware.properties_map


@pytest.mark.parametrize("asynchronous", [False, True])
def test_request_filter_disables_context_and_capture(client, asynchronous):
    properties = Mock()
    error = ValueError("view failed")

    def view(request):
        assert contexts.get_tags() == {}
        middleware.process_exception(request, error)
        return "response"

    async def async_view(request):
        return view(request)

    with (
        override_settings(
            POSTHOG_MW_CLIENT=client,
            POSTHOG_MW_CAPTURE_EXCEPTIONS=True,
            POSTHOG_MW_REQUEST_FILTER=lambda request: False,
            POSTHOG_MW_EXTRA_PROPERTIES=properties,
        ),
        patch.object(client, "capture_exception") as capture,
    ):
        middleware = PosthogContextMiddleware(async_view if asynchronous else view)
        result = middleware(MockRequest())
        assert (asyncio.run(result) if asynchronous else result) == "response"
        capture.assert_not_called()
        properties.assert_not_called()
