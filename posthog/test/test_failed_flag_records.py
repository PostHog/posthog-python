"""Spec 211: server failed records supply diagnostics, not evaluated values."""

import copy
from unittest import mock

import pytest

from posthog import AsyncPosthog
from posthog.client import Client
from posthog.types import normalize_flags_response


@pytest.fixture
def client():
    client = Client("project-key", send=False)
    yield client
    client.shutdown()


def response(shape, error_bit, failed_value="remote"):
    records = {}
    for key, value, failed in [("failed", failed_value, True), ("off", False, False)]:
        record = {
            "key": key,
            "failed": failed,
            "reason": {"code": "dependency_error" if failed else "flag_disabled"},
            "metadata": {"id": 1, "version": 2, "payload": '{"copy":"remote"}'},
        }
        if shape == "v4":
            record["value"] = value
            record["metadata"]["config_version"] = 2
        else:
            record.update(
                enabled=bool(value), variant=value if isinstance(value, str) else None
            )
        records[key] = record
    result = {
        "flags": records,
        "featureFlags": {"failed": "mirror"},
        "featureFlagPayloads": {"failed": '"mirror"'},
    }
    if error_bit is not None:
        result["errorsWhileComputingFlags"] = error_bit
    return result


@pytest.mark.parametrize("shape", ["v2", "v4"])
@pytest.mark.parametrize("error_bit", [None, False, True])
@pytest.mark.parametrize("failed_value", [False, "remote"])
@pytest.mark.parametrize("local_inconclusive", [False, True])
def test_failed_direct_results_and_events(
    client, shape, error_bit, failed_value, local_inconclusive
):
    if local_inconclusive:
        client.feature_flags = [
            {
                "key": "failed",
                "active": True,
                "ensure_experience_continuity": True,
                "filters": {"groups": []},
            }
        ]
    with (
        mock.patch(
            "posthog.client.flags",
            side_effect=lambda *args, **kwargs: copy.deepcopy(
                response(shape, error_bit, failed_value)
            ),
        ),
        mock.patch.object(client, "capture", return_value=(True, {})) as capture,
    ):
        assert (
            client.get_feature_flag_result(
                "failed", "user", send_feature_flag_events=False
            )
            is None
        )
        capture.assert_not_called()
        assert client.get_feature_flag_result("failed", "user") is None
        assert client.get_feature_flag("failed", "user") is None
        assert client.feature_enabled("failed", "user") is None
        assert (
            client.get_feature_flag_payload(
                "failed", "user", match_value=True, send_feature_flag_events=True
            )
            is None
        )
        assert capture.call_count == 1
        props = capture.call_args.kwargs["properties"]
        assert props["$feature_flag_response"] is None
        assert "evaluation_failed" in props["$feature_flag_error"].split(",")
        assert "flag_missing" not in props["$feature_flag_error"]
        assert ("errors_while_computing_flags" in props["$feature_flag_error"]) is bool(
            error_bit
        )
        if shape == "v4":
            assert props["$feature_flag_config_version"] == 2
            assert props["$feature_flag_reason_code"] == "dependency_error"
        assert "$feature_flag_payload" not in props
        assert client.get_feature_flag_result("off", "user").enabled is False
        assert "evaluation_failed" not in capture.call_args.kwargs["properties"].get(
            "$feature_flag_error", ""
        )
        combined = client.get_all_flags_and_payloads("user")
        assert combined["featureFlags"] == {"off": False}
        assert "failed" not in combined["featureFlagPayloads"]


@pytest.mark.parametrize("shape", ["v2", "v4"])
@pytest.mark.parametrize("error_bit", [None, False, True])
def test_failed_snapshot_defaults_diagnostics_and_enrichment(client, shape, error_bit):
    with (
        mock.patch("posthog.client.flags", return_value=response(shape, error_bit)),
        mock.patch.object(client, "capture", return_value=(True, {})) as capture,
    ):
        snapshot = client.evaluate_flags("user")
        assert (
            snapshot.is_enabled("failed", True, send_feature_flag_events=False) is True
        )
        capture.assert_not_called()
        assert snapshot.get_flag("failed") is None
        assert snapshot.get_flag_payload("failed") is None
        props = capture.call_args.kwargs["properties"]
        assert props["$feature_flag_response"] is None
        assert "evaluation_failed" in props["$feature_flag_error"]
        assert "flag_missing" not in props["$feature_flag_error"]
        assert ("errors_while_computing_flags" in props["$feature_flag_error"]) is bool(
            error_bit
        )
        assert snapshot.is_enabled("off", True) is False
        assert snapshot.keys == ["off"]
        assert snapshot._get_event_properties() == {"$feature/off": False}


@pytest.mark.parametrize("shape", ["v2", "v4"])
@pytest.mark.parametrize("error_bit", [None, False, True])
def test_failed_fallback_preserves_only_successful_local_pairs(
    client, shape, error_bit
):
    local = {
        "featureFlags": {"on": True, "off": False, "variant": "blue", "success": True},
        "featureFlagPayloads": {
            k: '{"copy":"local"}' for k in ["on", "off", "variant", "success"]
        },
    }
    remote = response(shape, error_bit)
    for key in ["on", "off", "variant"]:
        remote["flags"][key] = copy.deepcopy(remote["flags"]["failed"])
        remote["flags"][key]["key"] = key
    remote["flags"]["success"] = {
        "key": "success",
        "enabled": True,
        "variant": "remote",
        "metadata": {"payload": '{"copy":"success"}'},
    }
    with (
        mock.patch.object(
            client,
            "_get_all_flags_and_payloads_locally",
            return_value=(local, True, {}),
        ),
        mock.patch.object(
            client, "_get_flags_decision", return_value=normalize_flags_response(remote)
        ),
    ):
        result = client.get_all_flags_and_payloads("user")
    assert result["featureFlags"] == {
        "on": True,
        "off": False,
        "variant": "blue",
        "success": "remote",
    }
    assert result["featureFlagPayloads"] == {
        k: '{"copy":"success"}' if k == "success" else '{"copy":"local"}'
        for k in ["on", "off", "variant", "success"]
    }


async def test_failed_async_snapshot_honors_default():
    client = AsyncPosthog("project-key", send=False)
    try:
        with mock.patch(
            "posthog.async_client._async_flags",
            new=mock.AsyncMock(return_value=response("v4", False)),
        ):
            snapshot = await client.evaluate_flags("user")
        assert (
            snapshot.is_enabled("failed", True, send_feature_flag_events=False) is True
        )
        assert snapshot.get_flag("failed", send_feature_flag_events=False) is None
        assert snapshot.keys == ["off"]
    finally:
        await client.shutdown()
