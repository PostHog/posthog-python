import json
import sys
from unittest.mock import patch

import pytest

from posthog.client import Client
from posthog.types import _parse_flag_payload


@pytest.mark.parametrize(
    "api, local",
    [
        ("payload", True),
        ("payload", False),
        ("snapshot", True),
        ("snapshot", False),
        ("bulk", True),
        ("bulk", False),
        ("remote_bulk", False),
        ("remote_payloads", False),
    ],
)
@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"broken":', None),
        ("not json", None),
        ("   ", None),
        ("", None),
        ("NaN", None),
        ("Infinity", None),
        ("-Infinity", None),
        ('{"nested": [NaN]}', None),
        ("[Infinity, -Infinity]", None),
        pytest.param("[" * 20000, None, id="decoder-recursion-limit"),
        pytest.param(
            "9" * (getattr(sys, "get_int_max_str_digits", lambda: 0)() + 1),
            None,
            id="decoder-integer-limit",
            marks=pytest.mark.skipif(
                not getattr(sys, "get_int_max_str_digits", lambda: 0)(),
                reason="Integer conversion limit is unavailable or disabled",
            ),
        ),
        ("[1, 2]", [1, 2]),
        ('{"ok": true}', {"ok": True}),
        ('  {"ok": true}\n', {"ok": True}),
        ('"text"', "text"),
        ('"123"', "123"),
        ('"true"', "true"),
        ('"NaN"', "NaN"),
        ('"Infinity"', "Infinity"),
        ('""', ""),
        ("false", False),
        ("0", 0),
        ("null", None),
        ({"decoded": True}, {"decoded": True}),
        (None, None),
    ],
)
def test_payload_parsing(local, api, raw, expected):
    client = Client("test-key", send=False)
    if local:
        client.feature_flags = [
            {
                "id": 1,
                "key": "test-flag",
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 100}],
                    "payloads": {"true": raw},
                },
            },
            {
                "id": 2,
                "key": "healthy",
                "active": True,
                "filters": {
                    "groups": [{"properties": [], "rollout_percentage": 100}],
                    "payloads": {"true": '{"ok": true}'},
                },
            },
        ]
    response = {
        "flags": {
            "test-flag": {
                "key": "test-flag",
                "enabled": True,
                "variant": None,
                "reason": {"code": "condition_match", "description": "Matched"},
                "metadata": {"id": 1, "version": 1, "payload": raw},
            },
            "healthy": {
                "enabled": True,
                "metadata": {"payload": '{"ok": true}'},
            },
        }
    }
    try:
        with (
            patch.object(client, "load_feature_flags"),
            patch("posthog.client.flags", return_value=response) as request,
        ):
            if api in ("bulk", "remote_bulk"):
                bulk = (
                    client.get_all_flags_and_payloads(
                        "user", only_evaluate_locally=local
                    )
                    if api == "bulk"
                    else client.get_feature_flags_and_payloads("user")
                )
                assert bulk["featureFlags"] == {"test-flag": True, "healthy": True}
                assert bulk["featureFlagPayloads"]["healthy"] == '{"ok": true}'
                result = bulk["featureFlagPayloads"].get("test-flag")
            elif api == "remote_payloads":
                payloads = client.get_feature_payloads("user")
                assert payloads["healthy"] == '{"ok": true}'
                result = payloads.get("test-flag")
            elif api == "payload":
                with pytest.warns(DeprecationWarning):
                    result = client.get_feature_flag_payload(
                        "test-flag", "user", only_evaluate_locally=local
                    )
            else:
                result = client.evaluate_flags("user", only_evaluate_locally=local)
                assert result.get_flag_payload("healthy") == {"ok": True}
                assert result.get_flag("test-flag") is True
                result = result.get_flag_payload("test-flag")
            if api in ("bulk", "remote_bulk", "remote_payloads") and isinstance(
                raw, str
            ):
                if expected is not None or raw == "null":
                    assert result == raw
                    assert json.loads(result) == expected
                else:
                    assert result is None
            else:
                assert result == expected
                assert type(result) is type(expected)
            assert request.call_count == (0 if local else 1)
    finally:
        client.shutdown()


@pytest.mark.parametrize(
    "raw", ['{"private":', "", "   ", "NaN", "Infinity", "-Infinity"]
)
@pytest.mark.parametrize("decode", [True, False])
def test_parse_failure_logs_without_payload(raw, decode, caplog):
    with caplog.at_level("WARNING", logger="posthog"):
        for _ in range(10):
            assert _parse_flag_payload(raw, decode=decode) is None
    assert not caplog.records

    with caplog.at_level("DEBUG", logger="posthog"):
        assert _parse_flag_payload(raw, decode=decode) is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "DEBUG"
    assert caplog.records[0].getMessage().removeprefix("[PostHog] ") == (
        "[FEATURE FLAGS] Unable to parse flag payload as JSON"
    )
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("value", [True, False, "blue"])
@pytest.mark.parametrize("raw", ['{"broken":', "", "   "])
def test_invalid_payload_preserves_flag_getters(local, value, raw):
    client = Client("test-key", send=False)
    filters = {
        "groups": [{"properties": [], "rollout_percentage": 100 if value else 0}],
        "payloads": {str(value).lower(): raw},
    }
    if isinstance(value, str):
        filters["multivariate"] = {
            "variants": [{"key": value, "rollout_percentage": 100}]
        }
    if local:
        client.feature_flags = [
            {"id": 1, "key": "test-flag", "active": True, "filters": filters}
        ]
    response = {
        "flags": {
            "test-flag": {
                "enabled": value is not False,
                "variant": value if isinstance(value, str) else None,
                "reason": {"code": "condition_match", "description": "Matched"},
                "metadata": {"id": 1, "version": 1, "payload": raw},
            }
        }
    }
    try:
        with (
            patch.object(client, "load_feature_flags"),
            patch("posthog.client.flags", return_value=response) as request,
        ):
            result = client.get_feature_flag_result(
                "test-flag", "user", only_evaluate_locally=local
            )
            assert result is not None
            assert result.key == "test-flag"
            assert result.get_value() == value
            assert result.enabled is (value is not False)
            assert result.variant == (value if isinstance(value, str) else None)
            assert result.payload is None
            assert result.reason == (None if local else "Matched")
            with pytest.warns(DeprecationWarning):
                assert (
                    client.get_feature_flag(
                        "test-flag", "user", only_evaluate_locally=local
                    )
                    == value
                )
            with pytest.warns(DeprecationWarning):
                assert client.feature_enabled(
                    "test-flag", "user", only_evaluate_locally=local
                ) is (value is not False)
            assert request.call_count == (0 if local else 3)
    finally:
        client.shutdown()
