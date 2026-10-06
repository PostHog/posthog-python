"""Intercept capture uploads at the batch submitter for client-level tests.

Patching the submitter (not the HTTP layer) lets tests assert on the event
dicts the SDK built, before the wire encoding in ``capture_v1``. Wire shape is
covered by ``test_capture_v1``.
"""

import json
from unittest import mock

from requests import Response

_SUBMITTER = "_send_v1_batch"


def offline_v1_post(url: str, data=None, **kwargs) -> Response:
    """Stand-in for ``requests.Session.post`` that accepts every v1 event.

    For subprocess tests with no server. Prints the uncompressed request body,
    because the SDK never logs payloads, and answers ``ok`` for each event.
    """
    print(f"capture request body: {data}", flush=True)  # noqa: T201
    events = json.loads(data)["batch"]
    response = Response()
    response.status_code = 200
    response._content = json.dumps(
        {"results": {event["uuid"]: {"result": "ok"} for event in events}}
    ).encode()
    return response


def patch_capture_send(site: str = "client", **kwargs) -> "mock._patch":
    """Patch the submitter where ``posthog.<site>`` imported it.

    ``site="client"`` sees ``sync_mode`` uploads; ``site="consumer"`` sees
    background consumer uploads.
    """
    return mock.patch(f"posthog.{site}.{_SUBMITTER}", **kwargs)


def patch_async_capture_send(**kwargs) -> "mock._patch":
    """Patch the submitter the ``AsyncPosthog`` consumer awaits."""
    return mock.patch("posthog._async_consumer.async_send_v1_batch", **kwargs)


def sent_batch(send_mock: mock.Mock, call_index: int = -1) -> list[dict]:
    """Return the event batch from one recorded upload (default: the last)."""
    call = send_mock.call_args_list[call_index]
    return call.args[2] if len(call.args) > 2 else call.kwargs["batch"]


def sent_events(send_mock: mock.Mock) -> list[dict]:
    """Return every event uploaded through ``send_mock``, in send order."""
    return [
        event
        for index in range(len(send_mock.call_args_list))
        for event in sent_batch(send_mock, index)
    ]
