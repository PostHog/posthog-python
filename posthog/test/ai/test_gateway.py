import logging

from parameterized import parameterized

from posthog.ai.gateway import (
    is_posthog_ai_gateway_url,
    warn_if_posthog_ai_gateway,
)


@parameterized.expand(
    [
        ("bare_host", "https://gateway.us.posthog.com", True),
        ("with_path", "https://gateway.us.posthog.com/openai/v1", True),
        ("with_port", "http://gateway.us.posthog.com:443/anthropic", True),
        ("http_scheme", "http://gateway.us.posthog.com", True),
        ("uppercase_host", "https://GATEWAY.US.POSTHOG.COM/v1", True),
        ("openai", "https://api.openai.com/v1", False),
        ("anthropic", "https://api.anthropic.com", False),
        ("suffix_attack", "https://gateway.us.posthog.com.evil.com", False),
        ("other_posthog_host", "https://us.i.posthog.com", False),
        ("none", None, False),
        ("empty", "", False),
        ("garbage", "not a url", False),
    ]
)
def test_is_posthog_ai_gateway_url(name, base_url, expected):
    assert is_posthog_ai_gateway_url(base_url) is expected


def test_warn_if_posthog_ai_gateway_warns_for_gateway(caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway("https://gateway.us.posthog.com/openai/v1")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "PostHog AI Gateway" in warnings[0].getMessage()


def test_warn_if_posthog_ai_gateway_warns_on_every_call(caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        warn_if_posthog_ai_gateway("https://gateway.us.posthog.com")
        warn_if_posthog_ai_gateway("https://gateway.us.posthog.com")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_warn_if_posthog_ai_gateway_silent_for_non_gateway(caplog):
    with caplog.at_level(logging.WARNING, logger="posthog"):
        for base_url in ("https://api.openai.com/v1", None, ""):
            warn_if_posthog_ai_gateway(base_url)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
