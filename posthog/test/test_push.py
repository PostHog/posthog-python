from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest import mock

import httpx
import pytest
import requests
from parameterized import parameterized

from posthog import AsyncPosthog, Posthog
from posthog.push import Push, PushError, SendPushResult
from posthog.test.test_emails import (
    SECRET_KEY,
    TEMPLATE_ID,
    FakeAdapter,
    Reply,
    accepted,
)
from posthog.version import VERSION

SEND_URL = "https://us.posthog.com/api/projects/@current/messaging/send/push/"


def push_with(replies: list[Reply], **options: Any) -> tuple[Push, FakeAdapter]:
    adapter = FakeAdapter(replies)
    session = requests.Session()
    session.mount("https://", adapter)
    return Push(SECRET_KEY, session=session, **options), adapter


def send(push: Push, **overrides: Any) -> SendPushResult:
    return push.send(
        **{"template_id": TEMPLATE_ID, "distinct_id": "user_123", **overrides}
    )


@pytest.fixture(autouse=True)
def no_sleep():
    with (
        mock.patch("posthog.emails.time.sleep") as sleep,
        mock.patch("posthog.emails.random.uniform", return_value=1.0),
    ):
        yield sleep


def test_send_posts_the_template_person_and_data_to_the_app_host():
    push, adapter = push_with([accepted(replayed=True)])

    result = send(
        push,
        template_id=TEMPLATE_ID.upper(),
        template_data={
            "order_id": "1042",
            "shipped_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
        },
        idempotency_key="order-shipped:1042",
    )

    assert result == SendPushResult(id="msg_1", replayed=True)
    (request,) = adapter.requests
    assert request.url == SEND_URL
    assert request.method == "POST"
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert request.headers["Idempotency-Key"] == "order-shipped:1042"
    assert request.headers["User-Agent"] == f"posthog-python/{VERSION}"
    assert json.loads(request.body) == {
        "template_id": TEMPLATE_ID,
        "distinct_id": "user_123",
        "message_data": {"order_id": "1042", "shipped_at": "2026-11-20T00:00:00+00:00"},
    }


def test_retries_an_unavailable_pipeline_with_the_same_generated_key():
    push, adapter = push_with([(503, {"code": "delivery_unavailable"}, {}), accepted()])

    assert send(push).id == "msg_1"

    keys = [request.headers["Idempotency-Key"] for request in adapter.requests]
    assert len(keys) == 2 and keys[0] == keys[1]


def test_a_server_refusal_raises_a_push_error_with_its_details():
    push, _ = push_with(
        [
            (
                400,
                {
                    "code": "transactional_push_paused",
                    "detail": "The workflow is turned off.",
                    "fix": "Turn it on.",
                    "attr": "template_id",
                },
                {},
            )
        ]
    )

    with pytest.raises(PushError) as raised:
        send(push)

    assert raised.value.name == "validation_error"
    assert raised.value.code == "transactional_push_paused"
    assert raised.value.message == "The workflow is turned off."
    assert raised.value.status_code == 400
    assert raised.value.fix == "Turn it on."
    assert raised.value.field == "template_id"


@parameterized.expand(
    [
        ("template_name", {"template_id": "order-shipped"}, ValueError),
        ("empty_distinct_id", {"distinct_id": ""}, ValueError),
        ("long_distinct_id", {"distinct_id": "a" * 401}, ValueError),
        ("template_data_list", {"template_data": ["1042"]}, TypeError),
        ("key_with_space", {"idempotency_key": "order 1042"}, ValueError),
        ("negative_retries", {"max_retries": -1}, ValueError),
    ]
)
def test_rejects_invalid_arguments_before_sending(_name, overrides, error):
    push, adapter = push_with([])

    with pytest.raises(error):
        send(push, **overrides)

    assert adapter.requests == []


def test_client_push_goes_where_emails_go_and_refuses_after_shutdown():
    client = Posthog(
        "phc_test",
        secret_key=SECRET_KEY,
        host="https://ph.example.com",
        app_host="https://posthog.example.com",
        email_request_max_retries=0,
        sync_mode=True,
    )
    with mock.patch.object(
        requests.Session,
        "request",
        return_value=_response(503, {"code": "delivery_unavailable"}),
    ) as request:
        with pytest.raises(PushError) as raised:
            send(client.push)
    assert raised.value.code == "delivery_unavailable"
    assert request.call_count == 1
    assert (
        request.call_args.args[1]
        == "https://posthog.example.com/api/projects/@current/messaging/send/push/"
    )

    client.shutdown()

    with pytest.raises(PushError) as raised:
        send(client.push)
    assert raised.value.code == "client_closed"
    assert "push notification" in raised.value.message


def test_a_proxy_host_without_app_host_never_gets_the_secret_key():
    client = Posthog(
        "phc_test", secret_key=SECRET_KEY, host="https://ph.example.com", sync_mode=True
    )

    with mock.patch.object(requests.Session, "request") as request:
        with pytest.raises(PushError) as raised:
            send(client.push)

    assert raised.value.code == "missing_app_host"
    request.assert_not_called()


@pytest.mark.asyncio
async def test_async_client_push_retries_with_the_same_key():
    seen: list[httpx.Request] = []
    replies = [
        httpx.Response(503, json={"code": "delivery_unavailable"}),
        httpx.Response(202, json={"message_id": "msg_1", "replayed": False}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY)
    client.emails._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with mock.patch("posthog.emails.asyncio.sleep"):
        result = await client.push.send(template_id=TEMPLATE_ID, distinct_id="user_123")

    assert result == SendPushResult(id="msg_1")
    assert [str(request.url) for request in seen] == [SEND_URL, SEND_URL]
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
    await client.shutdown()


def _response(status: int, body: dict[str, Any]) -> requests.Response:
    reply = requests.Response()
    reply.status_code = status
    reply._content = json.dumps(body).encode()
    return reply
