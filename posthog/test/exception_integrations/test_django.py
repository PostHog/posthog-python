from posthog.exception_integrations.django import DjangoRequestExtractor
from django.test import RequestFactory
from django.conf import settings
from django.core.management import call_command
import django

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"

# setup a test app
if not settings.configured:
    settings.configure(
        SECRET_KEY="test",
        DEFAULT_CHARSET="utf-8",
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
    )
    django.setup()

    call_command("migrate", verbosity=0, interactive=False)


def mock_request_factory(override_headers):
    factory = RequestFactory(
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Referrer": "http://example.com",
            "X-Forwarded-For": "193.4.5.12",
            **(override_headers or {}),
        }
    )

    request = factory.get("/api/endpoint")
    return request


def test_request_extractor_with_no_trace():
    request = mock_request_factory(None)
    extractor = DjangoRequestExtractor(request)
    assert extractor.extract_person_data() == {
        "ip": "193.4.5.12",
        "user_agent": DEFAULT_USER_AGENT,
        "traceparent": None,
        "distinct_id": None,
        "$request_path": "/api/endpoint",
    }


def test_request_extractor_with_trace():
    request = mock_request_factory(
        {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    )

    extractor = DjangoRequestExtractor(request)
    assert extractor.extract_person_data() == {
        "ip": "193.4.5.12",
        "user_agent": DEFAULT_USER_AGENT,
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "distinct_id": None,
        "$request_path": "/api/endpoint",
    }


def test_request_extractor_with_tracestate():
    request = mock_request_factory(
        {
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "tracestate": "posthog-distinct-id=1234",
        }
    )
    extractor = DjangoRequestExtractor(request)
    assert extractor.extract_person_data() == {
        "ip": "193.4.5.12",
        "user_agent": DEFAULT_USER_AGENT,
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "distinct_id": "1234",
        "$request_path": "/api/endpoint",
    }


def test_request_extractor_with_complicated_tracestate():
    request = mock_request_factory(
        {"tracestate": "posthog-distinct-id=alohaMountainsXUYZ,rojo=00f067aa0ba902b7"}
    )
    extractor = DjangoRequestExtractor(request)
    assert extractor.extract_person_data() == {
        "ip": "193.4.5.12",
        "user_agent": DEFAULT_USER_AGENT,
        "traceparent": None,
        "distinct_id": "alohaMountainsXUYZ",
        "$request_path": "/api/endpoint",
    }


def test_request_extractor_with_request_user():
    from django.contrib.auth.models import User

    user = User.objects.create_user(
        username="test", email="test@posthog.com", password="top_secret"
    )

    request = mock_request_factory(None)
    request.user = user

    extractor = DjangoRequestExtractor(request)
    assert extractor.extract_person_data() == {
        "ip": "193.4.5.12",
        "user_agent": DEFAULT_USER_AGENT,
        "traceparent": None,
        "distinct_id": None,
        "$request_path": "/api/endpoint",
        "email": "test@posthog.com",
        "$user_id": "1",
    }
