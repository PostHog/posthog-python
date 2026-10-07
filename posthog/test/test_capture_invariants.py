import json
from typing import Any, Optional
from unittest import mock

import pytest
from requests import Response

from posthog import AsyncPosthog, CaptureCompression, Client
from posthog.capture_send import _CAPTURE_AI_V1_PATH, _CAPTURE_V1_PATH

_UUID = "01890000-0000-7000-8000-000000000001"
_TIMESTAMP = "2026-01-02T03:04:05+00:00"


def _event_kwargs() -> dict[str, Any]:
    return {
        "distinct_id": "user-1",
        "uuid": _UUID,
        "timestamp": _TIMESTAMP,
        "properties": {
            "$process_person_profile": False,
            "$product_tour_id": "tour-1",
            "empty_list": [],
            "empty_map": {},
            "none": None,
            "nested": {"a": {"b": [1, None, {}]}},
        },
        "options": {
            "process_person_profile": None,
            "cookieless_mode": False,
            "custom_list": [],
            "custom_map": {"inner": {}},
        },
    }


def _record_wire(sent: list[tuple[str, list[dict]]]):
    def post(url, data=None, **kwargs):
        events = json.loads(data)["batch"]
        sent.append((url, events))
        response = Response()
        response.status_code = 200
        response._content = json.dumps(
            {"results": {event["uuid"]: {"result": "ok"} for event in events}}
        ).encode()
        return response

    session = mock.Mock()
    session.post.side_effect = post
    return mock.patch("posthog.capture_send._get_session", return_value=session)


def _send_sync(method_name: str, before_send) -> list[tuple[str, list[dict]]]:
    sent: list[tuple[str, list[dict]]] = []
    with _record_wire(sent):
        client = Client(
            "test-key",
            sync_mode=True,
            before_send=before_send,
            capture_compression=CaptureCompression.NONE,
        )
        assert getattr(client, method_name)("$ai_generation", **_event_kwargs())
        client.shutdown()
    return sent


async def _send_async(method_name: str, before_send) -> list[tuple[str, list[dict]]]:
    sent: list[tuple[str, list[dict]]] = []
    with _record_wire(sent):
        client = AsyncPosthog(
            "test-key",
            before_send=before_send,
            capture_compression=CaptureCompression.NONE,
        )
        assert await getattr(client, method_name)("$ai_generation", **_event_kwargs())
        await client.shutdown()
    return sent


def _pass_through(event: dict) -> Optional[dict]:
    return event


@pytest.mark.parametrize(
    ("method_name", "path"),
    [("capture", _CAPTURE_V1_PATH), ("capture_ai", _CAPTURE_AI_V1_PATH)],
)
def test_pass_through_before_send_leaves_sync_wire_unchanged(method_name, path):
    without_hook = _send_sync(method_name, None)
    with_hook = _send_sync(method_name, _pass_through)

    assert [url.endswith(path) for url, _ in without_hook] == [True]
    assert with_hook == without_hook
    assert without_hook[0][1][0]["options"]["process_person_profile"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "path"),
    [
        ("capture_immediate", _CAPTURE_V1_PATH),
        ("capture_ai_immediate", _CAPTURE_AI_V1_PATH),
    ],
)
async def test_pass_through_before_send_leaves_async_wire_unchanged(method_name, path):
    without_hook = await _send_async(method_name, None)
    with_hook = await _send_async(method_name, _pass_through)

    assert [url.endswith(path) for url, _ in without_hook] == [True]
    assert with_hook == without_hook
    assert without_hook[0][1][0]["options"]["process_person_profile"] is False
