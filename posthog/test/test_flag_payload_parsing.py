from unittest.mock import patch

import pytest

from posthog.client import Client


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"broken":', None),
        ("not json", None),
        ("   ", None),
        ("", None),
        ("[1, 2]", [1, 2]),
        ('{"ok": true}', {"ok": True}),
        ('"text"', "text"),
        ("false", False),
        ("0", 0),
        ("null", None),
        ({"decoded": True}, {"decoded": True}),
        (None, None),
    ],
)
def test_payload_parsing(local, legacy, raw, expected):
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
            }
        ]
    response = {
        "flags": {
            "test-flag": {
                "key": "test-flag",
                "enabled": True,
                "variant": None,
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
            if legacy:
                with pytest.warns(DeprecationWarning):
                    result = client.get_feature_flag_payload(
                        "test-flag", "user", only_evaluate_locally=local
                    )
            else:
                result = client.evaluate_flags(
                    "user", only_evaluate_locally=local
                ).get_flag_payload("test-flag")
            assert result == expected
            assert request.call_count == (0 if local else 1)
    finally:
        client.shutdown()
