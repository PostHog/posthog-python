import logging

import pytest

from posthog.ai.gateway import (
    POSTHOG_AI_GATEWAY_HOSTS,
    is_posthog_ai_gateway_url,
    warn_if_posthog_ai_gateway,
    warn_if_posthog_ai_gateway_otel_attributes,
)


@pytest.mark.parametrize("host", POSTHOG_AI_GATEWAY_HOSTS)
def test_detects_every_gateway_host(host):
    assert is_posthog_ai_gateway_url(f"https://{host}/v1") is True


@pytest.mark.parametrize(
    "url",
    [
        "https://gateway.us.posthog.com/v1",
        "https://gateway.us.posthog.com/signals/v1",
        "https://gateway.us.posthog.com/anthropic",
    ],
)
def test_detects_live_host_with_route_prefix(url):
    assert is_posthog_ai_gateway_url(url) is True


@pytest.mark.parametrize(
    "label,url",
    [
        ("http scheme", "http://gateway.us.posthog.com/v1"),
        ("uppercase host", "https://GATEWAY.US.POSTHOG.COM/v1"),
        ("missing scheme", "gateway.us.posthog.com/v1"),
        ("port", "http://gateway.us.posthog.com:443/anthropic"),
    ],
)
def test_matches_gateway_host_variants(label, url):
    assert is_posthog_ai_gateway_url(url) is True


@pytest.mark.parametrize(
    "label,url",
    [
        ("ingestion host", "https://us.i.posthog.com"),
        ("app host", "https://eu.posthog.com"),
        ("openai", "https://api.openai.com/v1"),
        ("anthropic", "https://api.anthropic.com"),
        ("look-alike domain", "https://gateway.us.posthog.com.evil.example/v1"),
    ],
)
def test_does_not_match_non_gateway_url(label, url):
    assert is_posthog_ai_gateway_url(url) is False


@pytest.mark.parametrize(
    "label,value",
    [
        ("empty string", ""),
        ("none", None),
        ("malformed", "::::not a url"),
    ],
)
def test_does_not_match_empty_or_malformed(label, value):
    assert is_posthog_ai_gateway_url(value) is False


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_warns_with_double_counting_message(caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway("https://gateway.us.posthog.com/v1")

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "[PostHog]" in message
    assert "PostHog AI Gateway" in message
    assert "$ai_generation" in message
    assert "double-counted and double-billed" in message
    assert "https://posthog.com/docs/ai-observability" in message


def test_warns_on_every_gateway_call(caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        for _ in range(5):
            warn_if_posthog_ai_gateway("https://gateway.us.posthog.com/v1")

    assert len(_warnings(caplog)) == 5


@pytest.mark.parametrize(
    "base_url",
    ["https://api.openai.com/v1", None, ""],
)
def test_does_not_warn_for_non_gateway(base_url, caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway(base_url)

    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "label,attributes",
    [
        ("server.address bare host", {"server.address": "gateway.us.posthog.com"}),
        (
            "url.full full URL",
            {"url.full": "https://gateway.us.posthog.com/v1/chat/completions"},
        ),
    ],
)
def test_otel_attributes_warn_when_gateway_detected(label, attributes, caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway_otel_attributes(attributes)

    assert len(_warnings(caplog)) == 1
    assert "PostHog AI Gateway" in _warnings(caplog)[0].getMessage()


def test_otel_attributes_warn_at_most_once_per_span(caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway_otel_attributes(
            {
                "server.address": "gateway.us.posthog.com",
                "url.full": "https://gateway.us.posthog.com/v1",
            }
        )

    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize(
    "label,attributes",
    [
        ("none", None),
        ("empty", {}),
        ("non-gateway", {"server.address": "api.openai.com"}),
    ],
)
def test_otel_attributes_silent_for_non_gateway(label, attributes, caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway_otel_attributes(attributes)

    assert _warnings(caplog) == []
