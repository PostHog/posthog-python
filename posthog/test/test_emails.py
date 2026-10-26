from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Union
from unittest import mock

import httpx
import pytest
import requests
from parameterized import parameterized

from posthog import AsyncPosthog, Posthog
from posthog.emails import AsyncEmails, EmailError, Emails
from posthog.version import VERSION

SECRET_KEY = "phs_test_secret"
TEMPLATE_ID = "0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b"
SEND_URL = f"https://us.posthog.com/api/projects/@current/messaging_templates/{TEMPLATE_ID}/send/"

Reply = Union[Exception, tuple[int, Optional[dict[str, Any]], dict[str, str]]]


def accepted(message_id: str = "msg_1", **fields: Any) -> Reply:
    return (202, {"message_id": message_id, **fields}, {})


def rejected(status: int, code: Optional[str] = None, **headers: str) -> Reply:
    body = (
        {"type": "invalid_request", "code": code, "detail": "Nope."} if code else None
    )
    return (status, body, headers)


class FakeAdapter(requests.adapters.BaseAdapter):
    def __init__(self, replies: list[Reply]) -> None:
        super().__init__()
        self.replies = list(replies)
        self.requests: list[requests.PreparedRequest] = []

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = reply
        response = requests.Response()
        response.status_code = status
        response._content = b"" if body is None else json.dumps(body).encode()
        response.headers.update(headers)
        response.request = request
        response.url = request.url or ""
        return response

    def close(self) -> None:
        pass


def emails_with(replies: list[Reply], **options: Any) -> tuple[Emails, FakeAdapter]:
    emails = Emails(SECRET_KEY, **options)
    adapter = FakeAdapter(replies)
    emails._get_session().mount("https://", adapter)
    return emails, adapter


def send(emails: Emails, **overrides: Any):
    return emails.send(
        **{"to": "user@example.com", "template_id": TEMPLATE_ID, **overrides}
    )


@pytest.fixture(autouse=True)
def no_sleep():
    with (
        mock.patch("posthog.emails.time.sleep") as sleep,
        mock.patch("posthog.emails.random.uniform", return_value=1.0),
    ):
        yield sleep


@parameterized.expand(
    [
        (
            "with_distinct_id",
            {"distinct_id": "user_123"},
            {"identifiers": {"distinct_id": "user_123"}},
        ),
        ("without_distinct_id", {}, {}),
    ]
)
def test_send_posts_the_template_to_the_app_host(_name, overrides, extra_body):
    emails, adapter = emails_with([accepted("msg_1")])

    result = send(
        emails,
        template_data={
            "total": Decimal("19.5"),
            "expires_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
        },
        idempotency_key="welcome:user_123",
        **overrides,
    )

    assert result.id == "msg_1"
    assert result.replayed is False
    [request] = adapter.requests
    assert (request.method, request.url) == ("POST", SEND_URL)
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert request.headers["Idempotency-Key"] == "welcome:user_123"
    assert request.headers["User-Agent"] == f"posthog-python/{VERSION}"
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "message_data": {"total": 19.5, "expires_at": "2026-11-20T00:00:00+00:00"},
        **extra_body,
    }


@parameterized.expand(
    [
        ("default", None, "https://us.posthog.com"),
        ("us_ingestion", "https://us.i.posthog.com", "https://us.posthog.com"),
        ("eu_ingestion", "https://eu.i.posthog.com/", "https://eu.posthog.com"),
        ("legacy_app", "https://app.posthog.com", "https://us.posthog.com"),
        ("self_hosted", "https://posthog.example.com/", "https://posthog.example.com"),
    ]
)
def test_sends_go_to_the_app_host(_name, host, expected):
    assert Emails(SECRET_KEY, host=host).host == expected
    # The client turns app hosts into ingestion hosts, so the emails API maps them back.
    client_host = host or "https://us.posthog.com"
    client = Posthog(
        "phc_test", host=client_host, secret_key=SECRET_KEY, sync_mode=True
    )
    assert client.emails.host == expected


@parameterized.expand(
    [
        ("rate_limited", rejected(429, "throttled"), True),
        ("over_quota", rejected(429, "quota_limited"), False),
        ("service_unavailable", rejected(503, "delivery_unavailable"), True),
        ("same_key_in_flight", rejected(409, "idempotency_key_in_flight"), True),
        ("template_outdated", rejected(409, "template_outdated"), False),
        ("invalid_recipient", rejected(400, "invalid_recipient"), False),
        ("server_error", rejected(500), False),
        ("connection_error", requests.exceptions.ConnectionError("down"), True),
        ("read_timeout", requests.exceptions.ReadTimeout("slow"), True),
    ]
)
def test_retries_only_what_waiting_can_fix(_name, first_reply, retried):
    emails, adapter = emails_with([first_reply, accepted()])

    if retried:
        assert send(emails).id == "msg_1"
        first, second = adapter.requests
        assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    else:
        with pytest.raises(EmailError):
            send(emails)
        assert len(adapter.requests) == 1


@parameterized.expand(
    [
        (400, "invalid_recipient", "validation_error"),
        (401, "authentication_failed", "missing_api_key"),
        (403, "permission_denied", "invalid_permission"),
        (404, "not_found", "not_found"),
        (409, "idempotency_key_in_flight", "concurrent_idempotent_requests"),
        (409, "template_outdated", "validation_error"),
        (422, "idempotency_key_reused", "invalid_idempotent_request"),
        (429, "throttled", "rate_limit_exceeded"),
        (429, "quota_limited", "monthly_quota_exceeded"),
        (503, "delivery_unavailable", "service_unavailable"),
        (500, None, "application_error"),
    ]
)
def test_errors_keep_posthogs_code_and_resends_name(status, code, name):
    emails, _ = emails_with([rejected(status, code)], max_retries=0)

    with pytest.raises(EmailError) as raised:
        send(emails)

    assert (raised.value.status_code, raised.value.code, raised.value.name) == (
        status,
        code or "api_error",
        name,
    )
    assert SECRET_KEY not in str(raised.value)


def test_network_failure_after_retries_is_service_unavailable():
    failure = requests.exceptions.ConnectionError("down")
    emails, adapter = emails_with([failure, failure, failure])

    with pytest.raises(EmailError) as raised:
        send(emails)

    assert len(adapter.requests) == 3
    assert (raised.value.code, raised.value.name) == (
        "network_error",
        "service_unavailable",
    )


def test_waits_for_retry_after_and_gives_up_on_a_longer_one(no_sleep):
    emails, adapter = emails_with(
        [rejected(429, "throttled", **{"Retry-After": "5"}), accepted()]
    )
    send(emails)
    no_sleep.assert_called_once_with(5.0)

    emails, adapter = emails_with(
        [rejected(429, "throttled", **{"Retry-After": "120"})]
    )
    with pytest.raises(EmailError) as raised:
        send(emails)
    assert raised.value.retry_after == 120.0
    assert len(adapter.requests) == 1


def test_max_retries_zero_leaves_retries_to_the_caller():
    emails, adapter = emails_with(
        [rejected(503, "delivery_unavailable")], max_retries=0
    )

    with pytest.raises(EmailError):
        send(emails)

    assert len(adapter.requests) == 1


def test_an_acceptance_without_a_message_id_is_not_retried():
    emails, adapter = emails_with([(202, {}, {}), accepted()])

    with pytest.raises(EmailError) as raised:
        send(emails)

    assert raised.value.code == "api_error"
    assert len(adapter.requests) == 1


def test_replays_and_warnings_reach_the_caller(caplog):
    warning = {
        "code": "template_superseded",
        "detail": "Newer content.",
        "fix": "Upload.",
    }
    emails, _ = emails_with([accepted("msg_1", replayed=True, warnings=[warning])])

    with caplog.at_level(logging.WARNING, logger="posthog"):
        result = send(emails)

    assert result.replayed is True
    assert [(w.code, w.fix) for w in result.warnings] == [
        ("template_superseded", "Upload.")
    ]
    assert "template_superseded" in caplog.text


def test_missing_secret_key_fails_before_sending():
    emails = Emails(None)

    with pytest.raises(EmailError) as raised:
        send(emails)

    assert (raised.value.code, raised.value.name) == (
        "missing_secret_key",
        "missing_api_key",
    )
    assert emails._session is None


def test_a_disabled_client_sends_nothing():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)
    emails = client.emails
    adapter = FakeAdapter([])
    emails._get_session().mount("https://", adapter)

    client.disabled = True
    result = send(client.emails)

    assert result.id is None
    assert adapter.requests == []


@parameterized.expand(
    [
        ("several_recipients", {"to": ["a@example.com", "b@example.com"]}, TypeError),
        ("empty_recipient", {"to": " "}, ValueError),
        ("template_name", {"template_id": "welcome"}, ValueError),
        ("template_data_list", {"template_data": [1, 2]}, TypeError),
        ("key_with_space", {"idempotency_key": "welcome user"}, ValueError),
        ("key_too_long", {"idempotency_key": "k" * 256}, ValueError),
    ]
)
def test_rejects_invalid_arguments_before_sending(_name, overrides, error):
    emails, adapter = emails_with([])

    with pytest.raises(error):
        send(emails, **overrides)

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_async_send_retries_with_the_same_key():
    seen: list[httpx.Request] = []
    replies = [
        httpx.Response(503, json={"code": "delivery_unavailable"}),
        httpx.Response(202, json={"message_id": "msg_1"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    emails = AsyncPosthog("phc_test", secret_key=SECRET_KEY).emails
    emails._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with mock.patch("posthog.emails.asyncio.sleep") as sleep:
        result = await emails.send(
            to="user@example.com", template_id=TEMPLATE_ID, distinct_id="user_123"
        )

    assert result.id == "msg_1"
    sleep.assert_awaited_once()
    assert [str(request.url) for request in seen] == [SEND_URL, SEND_URL]
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
    assert seen[0].headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert json.loads(seen[0].content)["identifiers"] == {"distinct_id": "user_123"}
    await emails.aclose()


@pytest.mark.asyncio
async def test_async_disabled_client_sends_nothing():
    emails = AsyncEmails(SECRET_KEY, disabled=True)

    result = await emails.send(to="user@example.com", template_id=TEMPLATE_ID)

    assert result.id is None
    assert emails._client is None
