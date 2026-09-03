"""Event token counts trace back to a provider report: absent means unknown, 0 is a report of nothing."""

from unittest.mock import patch

import pytest

from posthog.ai.types import StreamingEventData, TokenUsage
from posthog.ai.utils import capture_streaming_event


@pytest.fixture
def mock_client():
    with patch("posthog.client.Client") as mock_client:
        mock_client.privacy_mode = False
        yield mock_client


def _event_data(
    usage_stats: TokenUsage, provider: str = "gemini"
) -> StreamingEventData:
    return StreamingEventData(
        provider=provider,
        model="gemini-2.0-flash",
        base_url="https://generativelanguage.googleapis.com",
        kwargs={},
        formatted_input=[{"role": "user", "content": "hi"}],
        formatted_output=[{"role": "assistant", "content": "hello"}],
        usage_stats=usage_stats,
        latency=0.5,
        distinct_id="user-1",
        trace_id="trace-1",
        properties=None,
        privacy_mode=False,
        groups=None,
        stop_reason=None,
    )


@pytest.mark.parametrize(
    ("usage_stats", "expected"),
    [
        (TokenUsage(), {}),
        (
            TokenUsage(input_tokens=0, output_tokens=0),
            {"$ai_input_tokens": 0, "$ai_output_tokens": 0},
        ),
        (TokenUsage(input_tokens=100), {"$ai_input_tokens": 100}),
    ],
)
def test_token_counts_trace_back_to_a_provider_report(
    mock_client, usage_stats, expected
):
    capture_streaming_event(mock_client, _event_data(usage_stats))

    props = mock_client.capture.call_args[1]["properties"]
    for key in ("$ai_input_tokens", "$ai_output_tokens"):
        if key in expected:
            assert props[key] == expected[key]
        else:
            assert key not in props


@pytest.mark.parametrize(
    ("provider", "usage_stats", "expected"),
    [
        # A stream interrupted before any usage report has nothing to default:
        # a fabricated 0 would read as a report of nothing.
        ("openai", TokenUsage(), {}),
        ("anthropic", TokenUsage(), {}),
        # Reported usage keeps the historical zero-defaults.
        (
            "openai",
            TokenUsage(input_tokens=12, output_tokens=7),
            {"$ai_cache_read_input_tokens": 0, "$ai_reasoning_tokens": 0},
        ),
        (
            "anthropic",
            TokenUsage(input_tokens=10),
            {"$ai_cache_read_input_tokens": 0, "$ai_cache_creation_input_tokens": 0},
        ),
    ],
)
def test_aux_token_fields_trace_back_to_a_provider_report(
    mock_client, provider, usage_stats, expected
):
    capture_streaming_event(mock_client, _event_data(usage_stats, provider=provider))

    props = mock_client.capture.call_args[1]["properties"]
    for key in (
        "$ai_cache_read_input_tokens",
        "$ai_cache_creation_input_tokens",
        "$ai_reasoning_tokens",
    ):
        if key in expected:
            assert props[key] == expected[key]
        else:
            assert key not in props
