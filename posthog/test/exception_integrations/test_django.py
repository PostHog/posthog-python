from posthog.exception_integrations.django import DjangoRequestExtractor

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"


def mock_request_factory(override_headers):
    class Request:
        META = {}
        # TRICKY: Actual django request dict object has case insensitive matching, and strips http from the names
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referrer": "http://example.com",
            "X-Forwarded-For": "193.4.5.12",
            **(override_headers or {}),
        }

    return Request()


def test_request_extractor_with_no_trace():
    request = mock_request_factory(None)
    extractor = DjangoRequestExtractor(request)
    assert extractor.extract_person_data() == {
        "ip": "193.4.5.12",
        "user_agent": DEFAULT_USER_AGENT,
        "traceparent": None,
        "distinct_id": None,
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
    }
