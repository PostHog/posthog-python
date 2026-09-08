"""Adapter protocol tests, run separately from the SDK's optional-dependency suite."""

import importlib.util
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import posthog.capture_v1
import posthog.client
import posthog.consumer
import posthog.request
from posthog.request import APIError, GetResponse


@pytest.fixture
def adapter(monkeypatch):
    # Importing the adapter installs transport instrumentation. Restore it after
    # every test so collecting these tests alongside SDK tests is safe.
    for module, name in [
        (posthog.request, "batch_post"),
        (posthog.consumer, "batch_post"),
        (posthog.capture_v1, "_post_v1"),
    ]:
        monkeypatch.setattr(module, name, getattr(module, name))
    spec = importlib.util.spec_from_file_location(
        "compliance_adapter_test", Path(__file__).with_name("adapter.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    yield module
    module.state.reset()


def definitions(version=1):
    return {
        "flags": [
            {
                "id": 1,
                "key": "flag",
                "active": True,
                "filters": {
                    "groups": [
                        {
                            "rollout_percentage": 100,
                            "properties": [
                                {
                                    "key": "plan",
                                    "value": False,
                                    "operator": "exact",
                                    "type": "person",
                                }
                            ],
                        }
                    ]
                },
            }
        ],
        "cohorts": {},
        "group_type_mapping": {},
        "property_matching_version": version,
    }


def initialize(adapter, **overrides):
    config = {
        "api_key": "phc_test_key",
        "host": "http://127.0.0.1:1",
        "personal_api_key": "phx_test_key",
    }
    config.update(overrides)
    response = adapter.app.test_client().post("/init", json=config)
    assert response.status_code == 200
    assert response.json["success"] is True
    return adapter.app.test_client()


@pytest.mark.parametrize("mode,capability", [("", "capture_v0"), ("v1", "capture_v1")])
def test_health_opts_into_local_evaluation_without_losing_capture(
    adapter, monkeypatch, mode, capability
):
    monkeypatch.setattr(adapter, "CAPTURE_MODE", mode)
    capabilities = adapter.app.test_client().get("/health").json["capabilities"]
    assert "feature_flags_local_evaluation_v1" in capabilities
    assert capability in capabilities
    assert "capture_ai_v0" in capabilities


def test_init_enables_explicit_definitions_loading_without_polling(adapter):
    initialize(adapter)
    assert adapter.state.client.personal_api_key == "phx_test_key"
    assert adapter.state.client.enable_local_evaluation is False
    assert adapter.state.client.poller is None
    assert adapter.state.remote_client.personal_api_key is None


def test_remote_only_init_does_not_require_a_privileged_key(adapter):
    initialize(adapter, personal_api_key=None)
    assert adapter.state.client.personal_api_key is None
    assert adapter.state.remote_client is None


@pytest.mark.parametrize("version,expected", [(1, True), (2, False)])
def test_reload_and_conclusive_local_result(adapter, monkeypatch, version, expected):
    client = initialize(adapter)
    get = Mock(return_value=GetResponse(data=definitions(version)))
    monkeypatch.setattr(posthog.client, "get", get)
    remote = Mock(side_effect=AssertionError("Local-only must never request /flags"))
    monkeypatch.setattr(adapter.state.client, "_get_flags_decision", remote)
    assert client.post("/reload_feature_flag_definitions", json={}).json == {
        "success": True,
        "ready": True,
    }
    response = client.post(
        "/get_feature_flag",
        json={
            "key": "flag",
            "distinct_id": "user",
            "person_properties": {"plan": "banana"},
            "only_evaluate_locally": True,
        },
    )
    assert response.json == {
        "success": True,
        "value": expected,
        "locally_evaluated": True,
    }
    get.assert_called_once()
    assert get.call_args.args[0] == "phx_test_key"
    assert get.call_args.args[1].startswith("/flags/definitions?token=phc_test_key")
    assert adapter.state.client.poller is None
    remote.assert_not_called()


def test_inconclusive_local_result_is_not_reported_as_false(adapter, monkeypatch):
    client = initialize(adapter)
    monkeypatch.setattr(
        posthog.client, "get", Mock(return_value=GetResponse(data=definitions()))
    )
    client.post("/reload_feature_flag_definitions", json={})
    remote = Mock(side_effect=AssertionError("Unexpected /flags fallback"))
    monkeypatch.setattr(adapter.state.client, "_get_flags_decision", remote)
    response = client.post(
        "/get_feature_flag",
        json={"key": "flag", "distinct_id": "user", "only_evaluate_locally": True},
    )
    assert response.json["success"] is False
    assert response.json["value"] is None
    assert response.json["locally_evaluated"] is False
    remote.assert_not_called()


def test_force_remote_bypasses_loaded_local_definitions(adapter, monkeypatch):
    client = initialize(adapter)
    monkeypatch.setattr(
        posthog.client, "get", Mock(return_value=GetResponse(data=definitions()))
    )
    client.post("/reload_feature_flag_definitions", json={})
    remote = Mock(
        return_value=posthog.client.normalize_flags_response(
            {"featureFlags": {"flag": False}}
        )
    )
    monkeypatch.setattr(adapter.state.remote_client, "_get_flags_decision", remote)
    monkeypatch.setattr(adapter.state.remote_client, "capture", Mock())
    response = client.post(
        "/get_feature_flag",
        json={
            "key": "flag",
            "distinct_id": "user",
            "person_properties": {"plan": "banana"},
            "force_remote": True,
        },
    )
    assert response.json == {"success": True, "value": False}
    remote.assert_called_once()


def test_rejects_conflicting_evaluation_modes(adapter):
    client = initialize(adapter)
    response = client.post(
        "/get_feature_flag",
        json={
            "key": "flag",
            "distinct_id": "user",
            "only_evaluate_locally": True,
            "force_remote": True,
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("timeout", [0, -1, 30001, True, "100", None])
def test_reload_validates_deadline(adapter, timeout):
    client = initialize(adapter)
    assert (
        client.post(
            "/reload_feature_flag_definitions", json={"timeout_ms": timeout}
        ).status_code
        == 400
    )


def test_reload_requires_client_and_privileged_key(adapter):
    client = adapter.app.test_client()
    assert client.post("/reload_feature_flag_definitions", json={}).status_code == 400
    initialize(adapter, personal_api_key=None)
    assert client.post("/reload_feature_flag_definitions", json={}).status_code == 400


@pytest.mark.parametrize("status", [401, 402, 500])
def test_failed_reload_does_not_report_previous_snapshot_ready(
    adapter, monkeypatch, status
):
    client = initialize(adapter)
    get = Mock(return_value=GetResponse(data=definitions()))
    monkeypatch.setattr(posthog.client, "get", get)
    assert (
        client.post("/reload_feature_flag_definitions", json={}).json["ready"] is True
    )
    get.side_effect = APIError(status, "definitions unavailable")
    response = client.post("/reload_feature_flag_definitions", json={})
    assert response.status_code == 502
    assert response.json["ready"] is False
    assert response.json["success"] is False
    assert get.call_count == 2


def test_reload_is_bounded_and_does_not_overlap_requests(adapter, monkeypatch):
    client = initialize(adapter)
    release = threading.Event()
    get = Mock(
        side_effect=lambda *args, **kwargs: (
            release.wait(2),
            GetResponse(data=definitions()),
        )[1]
    )
    monkeypatch.setattr(posthog.client, "get", get)
    try:
        start = time.monotonic()
        response = client.post(
            "/reload_feature_flag_definitions", json={"timeout_ms": 10}
        )
        assert response.status_code == 504
        assert response.json["ready"] is False
        assert time.monotonic() - start < 1
        assert (
            client.post("/reload_feature_flag_definitions", json={}).status_code == 409
        )
        get.assert_called_once()
    finally:
        release.set()
        thread = getattr(adapter.state, "reload_thread", None)
        if thread:
            thread.join(timeout=3)


def test_reset_disposes_both_clients_and_clears_reload_state(adapter, monkeypatch):
    client = initialize(adapter)
    local = adapter.state.client
    remote = adapter.state.remote_client
    local_shutdown = Mock(wraps=local.shutdown)
    remote_shutdown = Mock(wraps=remote.shutdown)
    monkeypatch.setattr(local, "shutdown", local_shutdown)
    monkeypatch.setattr(remote, "shutdown", remote_shutdown)
    assert client.post("/reset").json == {"success": True}
    local_shutdown.assert_called_once()
    remote_shutdown.assert_called_once()
    assert adapter.state.client is None
    assert adapter.state.remote_client is None
    assert adapter.state.reload_thread is None


def test_reload_refreshes_even_when_definitions_are_empty(adapter, monkeypatch):
    client = initialize(adapter)
    empty = definitions()
    empty["flags"] = []
    get = Mock(return_value=GetResponse(data=empty))
    monkeypatch.setattr(posthog.client, "get", get)
    for _ in range(2):
        assert client.post("/reload_feature_flag_definitions", json={}).json == {
            "success": True,
            "ready": True,
        }
    assert get.call_count == 2


def test_timed_out_reload_cannot_publish_into_replacement_client(adapter, monkeypatch):
    client = initialize(adapter)
    old_client = adapter.state.client
    release = threading.Event()
    monkeypatch.setattr(
        posthog.client,
        "get",
        Mock(
            side_effect=lambda *args, **kwargs: (
                release.wait(2),
                GetResponse(data=definitions(2)),
            )[1]
        ),
    )
    thread = None
    try:
        assert (
            client.post(
                "/reload_feature_flag_definitions", json={"timeout_ms": 10}
            ).status_code
            == 504
        )
        thread = adapter.state.reload_thread
        initialize(adapter)
        new_client = adapter.state.client
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert old_client.feature_flags is not None
        assert new_client is not old_client
        assert new_client.feature_flags is None
        assert new_client.poller is None
    finally:
        release.set()
        if thread:
            thread.join(timeout=3)
