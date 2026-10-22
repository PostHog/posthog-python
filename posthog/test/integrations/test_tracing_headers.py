"""Shared tracing-header contract through each framework's public entry point."""

from unittest.mock import Mock, patch
from uuid import UUID

import pytest
import django
from django.conf import settings as django_settings
from django.test import RequestFactory, override_settings
from flask import Flask

from posthog import contexts
from posthog.client import Client
from posthog.integrations.asgi import PosthogASGIMiddleware
from posthog.integrations.django import PosthogContextMiddleware
from posthog.integrations.flask import PosthogFlaskIntegration


@pytest.fixture(params=["asgi", "django", "django-async", "flask"])
def run_request(request):
    if not django_settings.configured:
        django_settings.configure(SECRET_KEY="test-secret", INSTALLED_APPS=[])
        django.setup()

    async def run(callback, headers=None, **options):
        headers = headers or {}
        if request.param == "asgi":

            async def app(scope, receive, send):
                callback()

            scope = {
                "type": "http",
                "headers": [
                    (k.encode(), v.encode("latin-1")) for k, v in headers.items()
                ],
            }
            await PosthogASGIMiddleware(app, **options)(scope, Mock(), Mock())
        elif request.param.startswith("django"):

            def view(request):
                callback()
                return "response"

            async def async_view(request):
                return view(request)

            settings = {"POSTHOG_MW_" + k.upper(): v for k, v in options.items()}
            with override_settings(
                POSTHOG_MW_CAPTURE_EXCEPTIONS=False,
                ALLOWED_HOSTS=["testserver"],
                **settings,
            ):
                middleware = PosthogContextMiddleware(
                    async_view if request.param == "django-async" else view
                )
                result = middleware(RequestFactory().get("/", headers=headers))
                if request.param == "django-async":
                    await result
        else:
            app = Flask(__name__)
            PosthogFlaskIntegration(app, **options)

            @app.get("/")
            def view():
                callback()
                return "response"

            # WSGI values can contain controls even though Werkzeug's header builder
            # rejects them. Exercise the integration's own sanitization as well.
            environ = {
                "HTTP_" + k.upper().replace("-", "_"): v for k, v in headers.items()
            }
            assert (
                app.test_client().get("/", environ_overrides=environ).status_code == 200
            )

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [None, False, True])
async def test_capture_tracing_headers_configuration(run_request, enabled):
    observed = []
    options = {} if enabled is None else {"capture_tracing_headers": enabled}
    await run_request(
        lambda: observed.append(
            (contexts.get_context_distinct_id(), contexts.get_context_session_id())
        ),
        {"x-Posthog-Distinct-ID": "browser", "X-posthog-SESSION-id": "session"},
        **options,
    )
    assert observed == (
        [(None, None)] if enabled is False else [("browser", "session")]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("canonical", [None, False, True])
async def test_deprecated_alias_and_canonical_precedence(
    run_request, legacy, canonical
):
    observed = []
    options = {"trust_tracing_headers": legacy}
    if canonical is not None:
        options["capture_tracing_headers"] = canonical
    with pytest.warns(
        DeprecationWarning, match="CAPTURE_TRACING_HEADERS|capture_tracing_headers"
    ):
        await run_request(
            lambda: observed.append(contexts.get_context_distinct_id()),
            {"X-PostHog-Distinct-ID": "browser"},
            **options,
        )
    assert observed == (
        ["browser"] if (legacy if canonical is None else canonical) else [None]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, expected",
    [("  user\n-\t1\x85  ", "user-1"), ("x" * 1001, "x" * 1000), (" \r\n ", None)],
)
async def test_sanitization_is_identical(run_request, value, expected):
    observed = []
    await run_request(
        lambda: observed.append(
            (contexts.get_context_distinct_id(), contexts.get_context_session_id())
        ),
        {"X-PostHog-Distinct-ID": value, "X-PostHog-Session-ID": value},
    )
    assert observed == [(expected, expected)]


@pytest.fixture
def client():
    instance = Client("test-key", send=False)
    yield instance
    instance.shutdown()


@pytest.mark.asyncio
async def test_explicit_capture_values_override_headers(run_request, client):
    with patch.object(client, "_enqueue") as enqueue:

        def capture():
            client.capture("implicit")
            client.capture(
                "explicit",
                distinct_id="explicit-user",
                properties={"$session_id": "explicit-session"},
            )
            contexts.identify_context("application-user")
            client.capture("application")

        await run_request(
            capture,
            {
                "X-PostHog-Distinct-ID": "header-user",
                "X-PostHog-Session-ID": "header-session",
            },
        )
    messages = [call.args[0] for call in enqueue.call_args_list]
    assert [m["distinct_id"] for m in messages] == [
        "header-user",
        "explicit-user",
        "application-user",
    ]
    assert [m["properties"]["$session_id"] for m in messages] == [
        "header-session",
        "explicit-session",
        "header-session",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("process_person_profile", [None, False, True])
async def test_session_only_capture_is_personless_and_flags_are_empty(
    run_request, client, process_person_profile
):
    with (
        patch.object(client, "_enqueue") as enqueue,
        patch("requests.Session.request") as network,
    ):

        def capture():
            properties = (
                {}
                if process_person_profile is None
                else {"$process_person_profile": process_person_profile}
            )
            client.capture("session-only", properties=properties)
            flags = client.evaluate_flags()
            assert flags.is_enabled("unknown") is False
            assert flags.get_flag("unknown") is None

        await run_request(capture, {"X-PostHog-Session-ID": "header-session"})
        network.assert_not_called()
    enqueue.assert_called_once()
    event = enqueue.call_args.args[0]
    assert UUID(event["distinct_id"])
    assert event["properties"]["$session_id"] == "header-session"
    assert enqueue.call_args.kwargs["derived_options"] == {
        "process_person_profile": False
    }


@pytest.mark.asyncio
async def test_request_context_is_restored_and_not_reused(run_request):
    observed = []

    def observe():
        observed.append(
            (contexts.get_context_distinct_id(), contexts.get_context_session_id())
        )

    with contexts.new_context(fresh=True, capture_exceptions=False):
        await run_request(
            observe,
            {
                "X-PostHog-Distinct-ID": "first-user",
                "X-PostHog-Session-ID": "first-session",
            },
        )
        assert contexts.get_context_distinct_id() is None
        assert contexts.get_context_session_id() is None
        await run_request(observe)
    assert observed == [("first-user", "first-session"), (None, None)]
