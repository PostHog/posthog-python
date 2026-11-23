import asyncio
import json
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Union
from unittest import mock

import pytest
from requests import Response

from posthog import AsyncPosthog, CaptureCompression, Client
from posthog.capture_send import (
    _CAPTURE_AI_V1_PATH,
    _CAPTURE_V1_PATH,
    _HEADER_REQUEST_TIMESTAMP,
)

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


def _ok_response(events: list[dict]) -> Response:
    response = Response()
    response.status_code = 200
    response._content = json.dumps(
        {"results": {event["uuid"]: {"result": "ok"} for event in events}}
    ).encode()
    return response


def _record_wire(sent: list[tuple[str, list[dict]]]):
    def post(url, data=None, **kwargs):
        events = json.loads(data)["batch"]
        sent.append((url, events))
        return _ok_response(events)

    session = mock.Mock()
    session.post.side_effect = post
    return mock.patch("posthog.capture_send._get_capture_session", return_value=session)


def _record_requests(requests: list[dict[str, Any]]):
    def post(url, data=None, headers=None, **kwargs):
        body = json.loads(data)
        requests.append({"body": body, "headers": headers})
        return _ok_response(body["batch"])

    session = mock.Mock()
    session.post.side_effect = post
    return mock.patch("posthog.capture_send._get_capture_session", return_value=session)


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


_WHOLE_SECOND = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_WHOLE_SECOND_WIRE = "2026-01-02T03:04:05.000000+00:00"


@contextmanager
def _whole_second_clock() -> Iterator[None]:
    """Freeze ``datetime.now`` at a whole second where capture reads the clock."""
    with ExitStack() as stack:
        for module in ("client", "async_client", "capture_send"):
            stack.enter_context(
                mock.patch(
                    f"posthog.{module}.datetime",
                    wraps=datetime,
                    **{"now.return_value": _WHOLE_SECOND},
                )
            )
        yield


def _capture_on_path(
    path: str, timestamp: Union[str, datetime, None]
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"distinct_id": "user-1", "timestamp": timestamp}
    with _record_requests(requests), _whole_second_clock():
        if path == "async":

            async def send() -> None:
                client = AsyncPosthog(
                    "test-key", capture_compression=CaptureCompression.NONE
                )
                assert client.capture("event", **kwargs)
                await client.shutdown()

            asyncio.run(send())
        else:
            client = Client(
                "test-key",
                sync_mode=path == "sync_mode",
                capture_compression=CaptureCompression.NONE,
            )
            assert client.capture("event", **kwargs)
            client.shutdown()
    return requests


@pytest.mark.parametrize("path", ["sync_mode", "sync", "async"])
@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (None, _WHOLE_SECOND_WIRE),
        ("not-an-iso-timestamp", _WHOLE_SECOND_WIRE),
        (_WHOLE_SECOND, _WHOLE_SECOND_WIRE),
        ("2026-01-02T03:04:05Z", _WHOLE_SECOND_WIRE),
        ("2026-01-02T03:04:05.000Z", _WHOLE_SECOND_WIRE),
        ("2026-01-02T03:04:05.120Z", "2026-01-02T03:04:05.120000+00:00"),
        ("2026-01-02T03:04:05.123456Z", "2026-01-02T03:04:05.123456+00:00"),
    ],
)
def test_wire_timestamps_keep_fractional_digits_on_whole_seconds(
    path, timestamp, expected
):
    requests = _capture_on_path(path, timestamp)

    assert len(requests) == 1
    body, headers = requests[0]["body"], requests[0]["headers"]
    assert [event["timestamp"] for event in body["batch"]] == [expected]
    assert body["created_at"] == _WHOLE_SECOND_WIRE
    assert headers[_HEADER_REQUEST_TIMESTAMP] == _WHOLE_SECOND_WIRE
