import json
import re
from pathlib import Path
from unittest import mock

import pytest
import requests
from freezegun import freeze_time

from posthog.client import Client

_SNAPSHOT_DIRECTORY = Path(__file__).with_name("snapshots")
_TEST_FILE_SUFFIX = "posthog/test/test_server_payload_snapshots.py"
_FIXED_TIME = "2026-01-02T03:04:05+00:00"
_USER_AGENT_PATTERN = re.compile(r"posthog-python/[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_RUNTIME_CONTEXT = {
    "$os": "<OS>",
    "$os_distro": "<OS_DISTRO>",
    "$os_version": "<OS_VERSION>",
    "$python_runtime": "<PYTHON_RUNTIME>",
    "$python_version": "<PYTHON_VERSION>",
}


def _successful_response() -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = b"{}"
    return response


def _has_test_file_suffix(value: str) -> bool:
    return value.replace("\\", "/").endswith(_TEST_FILE_SUFFIX)


def _normalize_snapshot_value(value):
    if isinstance(value, list):
        return [_normalize_snapshot_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized = {}
    for key, item in value.items():
        if key == "$lib_version":
            normalized[key] = "<SDK_VERSION>"
        elif (
            key == "User-Agent"
            and isinstance(item, str)
            and _USER_AGENT_PATTERN.fullmatch(item)
        ):
            normalized[key] = "posthog-python/<SDK_VERSION>"
        elif (
            key == "abs_path" and isinstance(item, str) and _has_test_file_suffix(item)
        ):
            normalized[key] = f"<PROJECT_ROOT>/{_TEST_FILE_SUFFIX}"
        elif (
            key == "filename" and isinstance(item, str) and _has_test_file_suffix(item)
        ):
            normalized[key] = _TEST_FILE_SUFFIX
        else:
            normalized[key] = _normalize_snapshot_value(item)
    return normalized


def _normalize_exception_line_numbers(request):
    exceptions = request["body"]["batch"][0]["properties"]["$exception_list"]
    runtime_frames = exceptions[0]["stacktrace"]["frames"]
    cause_frames = exceptions[1]["stacktrace"]["frames"]
    line_numbers = [
        runtime_frames[0]["lineno"],
        runtime_frames[1]["lineno"],
        cause_frames[0]["lineno"],
    ]

    assert all(
        type(line_number) is int and line_number > 0 for line_number in line_numbers
    )
    assert line_numbers[2] < line_numbers[1] < line_numbers[0]

    for frame in [*runtime_frames, *cause_frames]:
        frame["lineno"] = "<LINE_NUMBER>"
    return request


def _transport_request(call):
    return _normalize_snapshot_value(
        {
            "body": json.loads(call.kwargs["data"]),
            "headers": dict(call.kwargs["headers"]),
            "timeout": call.kwargs["timeout"],
            "url": call.args[0],
        }
    )


def _assert_json_snapshot(name: str, value) -> None:
    actual = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    expected = (_SNAPSHOT_DIRECTORY / f"{name}.json").read_text()
    assert actual == expected


def _legacy_event_family_request():
    session = mock.MagicMock()
    session.post.return_value = _successful_response()

    with (
        freeze_time(_FIXED_TIME),
        mock.patch("posthog.request._get_session", return_value=session),
        mock.patch("posthog.client.system_context", return_value=_RUNTIME_CONTEXT),
    ):
        client = Client(
            "phc_snapshot_project",
            host="https://example.posthog.test",
            capture_mode="v0",
            flush_at=100,
            flush_interval=100,
        )
        try:
            client.capture(
                "order completed",
                distinct_id="user-123",
                properties={
                    "amount": 29.5,
                    "items": [
                        {"quantity": 1, "sku": "SKU-1"},
                        {"quantity": 2, "sku": "SKU-2"},
                    ],
                },
                groups={"company": "company-456"},
                timestamp=_FIXED_TIME,
                uuid="00000000-0000-4000-8000-000000000001",
            )
            client.set(
                distinct_id="user-123",
                properties={"email": "person@example.com", "plan": "pro"},
                timestamp=_FIXED_TIME,
                uuid="00000000-0000-4000-8000-000000000002",
            )
            client.alias(
                previous_id="anonymous-789",
                distinct_id="user-123",
                timestamp=_FIXED_TIME,
                uuid="00000000-0000-4000-8000-000000000003",
            )
            client.group_identify(
                "company",
                "company-456",
                properties={"employees": 42, "name": "Example Corp"},
                distinct_id="user-123",
                timestamp=_FIXED_TIME,
                uuid="00000000-0000-4000-8000-000000000004",
            )
            client.flush()
        finally:
            client.shutdown()

    session.post.assert_called_once()
    return _transport_request(session.post.call_args)


def _raise_snapshot_exception() -> None:
    try:
        raise ValueError("invalid order total")
    except ValueError as cause:
        raise RuntimeError("checkout failed") from cause


def _exception_request():
    session = mock.MagicMock()
    session.post.return_value = _successful_response()

    with (
        freeze_time(_FIXED_TIME),
        mock.patch("posthog.request._get_session", return_value=session),
        mock.patch("posthog.client.system_context", return_value=_RUNTIME_CONTEXT),
        mock.patch("posthog.client._get_current_otel_span_properties", return_value={}),
    ):
        client = Client(
            "phc_snapshot_project",
            host="https://example.posthog.test",
            capture_mode="v0",
            sync_mode=True,
            project_root=str(Path(__file__).parents[2]),
            capture_exception_code_variables=False,
        )
        try:
            try:
                _raise_snapshot_exception()
            except RuntimeError as error:
                client.capture_exception(
                    error,
                    distinct_id="user-123",
                    properties={"component": "checkout", "severity": "high"},
                    groups={"company": "company-456"},
                    timestamp=_FIXED_TIME,
                    uuid="00000000-0000-4000-8000-000000000005",
                )
        finally:
            client.shutdown()

    session.post.assert_called_once()
    return _normalize_exception_line_numbers(_transport_request(session.post.call_args))


def _flags_request():
    session = mock.MagicMock()
    session.post.return_value = _successful_response()

    with (
        freeze_time(_FIXED_TIME),
        mock.patch("posthog.request._get_flags_session", return_value=session),
    ):
        client = Client(
            "phc_snapshot_project",
            host="https://example.posthog.test",
            send=False,
        )
        try:
            client.get_flags_decision(
                "user-123",
                groups={"company": "company-456"},
                person_properties={"email": "person@example.com", "plan": "pro"},
                group_properties={
                    "company": {"employees": 42, "industry": "technology"}
                },
                disable_geoip=False,
                flag_keys_to_evaluate=["checkout-redesign", "new-billing"],
                device_id="device-789",
            )
        finally:
            client.shutdown()

    session.post.assert_called_once()
    return _transport_request(session.post.call_args)


def test_normalizes_release_user_agent_version():
    assert _normalize_snapshot_value({"User-Agent": "posthog-python/7.39.1"}) == {
        "User-Agent": "posthog-python/<SDK_VERSION>"
    }


@pytest.mark.parametrize(
    "user_agent",
    [
        "posthog-pythons/7.39.1",
        "posthog_python/7.39.1",
        "posthog-python/",
        "posthog-python/7.39.1;",
        "posthog-python/(7.39.1)",
        "Mozilla/5.0",
    ],
)
def test_does_not_normalize_unexpected_user_agent(user_agent):
    assert _normalize_snapshot_value({"User-Agent": user_agent}) == {
        "User-Agent": user_agent
    }


def test_legacy_capture_identify_alias_and_group_identify_request_snapshot():
    _assert_json_snapshot("legacy_event_family", _legacy_event_family_request())


def test_complete_exception_request_snapshot():
    _assert_json_snapshot("exception_event", _exception_request())


def test_complete_flags_request_snapshot():
    _assert_json_snapshot("flags_request", _flags_request())
