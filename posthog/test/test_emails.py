from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Union
from unittest import mock

import httpx
import pytest
import requests
from parameterized import parameterized

from posthog import AsyncPosthog, Posthog
from posthog.emails import AsyncEmails, EmailError, Emails, EmailStatus
from posthog.version import VERSION

SECRET_KEY = "phs_test_secret"
TEMPLATE_ID = "0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b"
SEND_URL = "https://us.posthog.com/api/projects/@current/messaging/send/email/"
MESSAGE_ID = "01a18356-c56c-0001-e494-7666bc915188"
STATUS_URL = (
    f"https://us.posthog.com/api/projects/@current/messaging/emails/{MESSAGE_ID}/"
)

Reply = Union[Exception, tuple[int, Union[dict[str, Any], bytes, None], dict[str, str]]]


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
        self.verify: list[Any] = []

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        self.requests.append(request)
        self.verify.append(kwargs.get("verify"))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = reply
        response = requests.Response()
        response.status_code = status
        response._content = (
            body
            if isinstance(body, bytes)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        response.headers.update(headers)
        response.request = request
        response.url = request.url or ""
        return response

    def close(self) -> None:
        pass


def emails_with(replies: list[Reply], **options: Any) -> tuple[Emails, FakeAdapter]:
    adapter = FakeAdapter(replies)
    session = requests.Session()
    session.mount("https://", adapter)
    return Emails(SECRET_KEY, session=session, **options), adapter


def response(status: int, body: dict[str, Any]) -> requests.Response:
    reply = requests.Response()
    reply.status_code = status
    reply._content = json.dumps(body).encode()
    return reply


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
            {"distinct_id": "user_123"},
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
        "template_id": TEMPLATE_ID,
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


def test_a_client_with_a_custom_host_never_sends_it_the_secret_key():
    assert (
        Emails(SECRET_KEY, host="https://ph.example.com/").host
        == "https://ph.example.com"
    )
    client = Posthog(
        "phc_test", host="https://ph.example.com", secret_key=SECRET_KEY, sync_mode=True
    )

    with mock.patch.object(requests.Session, "request") as request:
        with pytest.raises(EmailError) as raised:
            send(client.emails)

    assert raised.value.code == "missing_app_host"
    request.assert_not_called()


def test_client_email_options_set_the_host_timeout_and_retries():
    client = Posthog(
        "phc_test",
        host="https://ph.example.com",
        secret_key=SECRET_KEY,
        sync_mode=True,
        app_host="https://posthog.example.com/",
        email_request_timeout_seconds=3,
        email_request_max_retries=0,
        migrated_from="resend",
    )
    unavailable = response(503, {"code": "delivery_unavailable"})

    with mock.patch.object(
        requests.Session, "request", return_value=unavailable
    ) as request:
        with pytest.raises(EmailError):
            send(client.emails)

    request.assert_called_once()
    assert request.call_args.args[1].startswith("https://posthog.example.com/api/")
    assert request.call_args.kwargs["timeout"] == 3
    sent = request.call_args.kwargs["auth"](
        requests.Request("POST", "https://posthog.example.com/").prepare()
    )
    assert sent.headers["X-PostHog-Migrated-From"] == "resend"


def test_names_the_provider_the_client_moved_from_on_every_request():
    emails, adapter = emails_with([accepted(), status_reply()], migrated_from="resend")

    send(emails)
    emails.get(MESSAGE_ID)

    assert [
        request.headers["X-PostHog-Migrated-From"] for request in adapter.requests
    ] == [
        "resend",
        "resend",
    ]


@parameterized.expand([("space", "re send"), ("not_text", 1), ("empty", "")])
def test_rejects_a_provider_name_that_is_not_a_header_value(_name, migrated_from):
    with pytest.raises(ValueError):
        Posthog(
            "phc_test",
            secret_key=SECRET_KEY,
            sync_mode=True,
            migrated_from=migrated_from,
        )


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
        (422, "recipient_suppressed", "validation_error"),
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
    assert isinstance(raised.value.__cause__, requests.exceptions.ConnectionError)


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


@parameterized.expand(
    [
        ("client_option", {"max_retries": 0}, {}, 1),
        ("per_send_override", {}, {"max_retries": 0}, 1),
        ("per_send_override_above_client", {"max_retries": 0}, {"max_retries": 1}, 2),
        (
            "code_send_override",
            {},
            {
                "template_id": None,
                "template_key": "password-reset",
                "subject": "Reset",
                "html": "<p>Reset</p>",
                "max_retries": 0,
            },
            1,
        ),
    ]
)
def test_max_retries_zero_leaves_retries_to_the_caller(
    _name, client_options, send_options, requests_made
):
    emails, adapter = emails_with(
        [rejected(503, "delivery_unavailable")] * 3, **client_options
    )
    args = {"to": "user@example.com", "template_id": TEMPLATE_ID, **send_options}

    with pytest.raises(EmailError):
        emails.send(
            **{name: value for name, value in args.items() if value is not None}
        )

    assert len(adapter.requests) == requests_made


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

    with mock.patch.object(requests.Session, "request") as request:
        with pytest.raises(EmailError) as raised:
            send(emails)

    assert (raised.value.code, raised.value.name) == (
        "missing_secret_key",
        "missing_api_key",
    )
    request.assert_not_called()


def test_a_disabled_client_sends_nothing():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)
    emails = client.emails

    client.disabled = True
    with mock.patch.object(requests.Session, "request") as request:
        with pytest.raises(EmailError) as raised:
            send(emails)

    assert (raised.value.code, raised.value.name) == (
        "client_disabled",
        "application_error",
    )
    request.assert_not_called()


def test_a_shut_down_client_sends_no_email():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)
    emails = client.emails

    client.shutdown()

    with mock.patch.object(requests.Session, "request") as request:
        for handle in (emails, client.emails):
            with pytest.raises(EmailError) as raised:
                send(handle)
            assert raised.value.code == "client_closed"
    request.assert_not_called()
    assert emails._session is None


@parameterized.expand(
    [
        ("several_recipients", {"to": ["a@example.com", "b@example.com"]}, TypeError),
        ("empty_recipient", {"to": " "}, ValueError),
        ("template_name", {"template_id": "welcome"}, ValueError),
        ("template_data_list", {"template_data": [1, 2]}, TypeError),
        ("key_with_space", {"idempotency_key": "welcome user"}, ValueError),
        ("key_too_long", {"idempotency_key": "k" * 256}, ValueError),
        ("negative_retries", {"max_retries": -1}, ValueError),
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

    emails = AsyncEmails(
        SECRET_KEY,
        timeout=3,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), auth=("user", "pass"), timeout=None
        ),
    )

    with mock.patch("posthog.emails.asyncio.sleep") as sleep:
        result = await emails.send(
            to="user@example.com", template_id=TEMPLATE_ID, distinct_id="user_123"
        )

    assert result.id == "msg_1"
    sleep.assert_awaited_once()
    assert [str(request.url) for request in seen] == [SEND_URL, SEND_URL]
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
    assert seen[0].headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert seen[0].extensions["timeout"] == dict.fromkeys(
        ["connect", "read", "write", "pool"], 3
    )
    assert json.loads(seen[0].content)["distinct_id"] == "user_123"


@pytest.mark.asyncio
async def test_async_disabled_client_sends_nothing():
    handler = mock.Mock(side_effect=AssertionError("a disabled client sent a request"))
    emails = AsyncEmails(
        SECRET_KEY,
        disabled=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(EmailError) as raised:
        await emails.send(to="user@example.com", template_id=TEMPLATE_ID)

    assert raised.value.code == "client_disabled"
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_async_shutdown_waits_for_a_send_in_flight_and_then_refuses_sends():
    started, release = asyncio.Event(), asyncio.Event()
    replies = [
        httpx.Response(503, json={"code": "delivery_unavailable"}),
        httpx.Response(202, json={"message_id": "msg_1"}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return replies.pop(0)

    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY)
    emails = client.emails
    emails._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with mock.patch("posthog.emails._retry_delay", return_value=0.0):
        sending = asyncio.create_task(
            emails.send(to="user@example.com", template_id=TEMPLATE_ID)
        )
        await started.wait()
        shutting_down = asyncio.create_task(client.shutdown())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not shutting_down.done()
        release.set()

        assert (await sending).id == "msg_1"
        await shutting_down

    assert emails._client is None
    with pytest.raises(EmailError) as raised:
        await emails.send(to="user@example.com", template_id=TEMPLATE_ID)
    assert raised.value.code == "client_closed"


def status_reply(**fields: Any) -> Reply:
    body = {
        "id": MESSAGE_ID,
        "status": "bounced",
        "created_at": "2026-10-28T16:27:44.364000Z",
        "status_at": "2026-10-28T16:30:02.100000Z",
        "reason": "hard",
        **fields,
    }
    return (200, body, {})


def test_get_reads_the_status_of_a_sent_email_from_the_app_host():
    emails, adapter = emails_with([status_reply()])

    status = emails.get(MESSAGE_ID.upper())

    assert status == EmailStatus(
        id=MESSAGE_ID,
        status="bounced",
        created_at=datetime(2026, 10, 28, 16, 27, 44, 364000, tzinfo=timezone.utc),
        status_at=datetime(2026, 10, 28, 16, 30, 2, 100000, tzinfo=timezone.utc),
        reason="hard",
    )
    [request] = adapter.requests
    assert (request.method, request.url, request.body) == ("GET", STATUS_URL, None)
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert "Idempotency-Key" not in request.headers


@parameterized.expand(
    [
        (
            "unknown_id_without_a_retry",
            [rejected(404, "not_found")],
            MESSAGE_ID,
            "not_found",
            1,
        ),
        (
            "reply_without_a_status",
            [status_reply(status=None)],
            MESSAGE_ID,
            "api_error",
            1,
        ),
    ]
)
def test_get_reports_a_missing_status(_name, replies, message_id, code, requests_made):
    emails, adapter = emails_with(replies)

    with pytest.raises(EmailError) as raised:
        emails.get(message_id)

    assert raised.value.code == code
    assert len(adapter.requests) == requests_made


def test_get_rejects_an_id_that_is_not_a_message_id_before_a_request():
    emails, adapter = emails_with([])

    with pytest.raises(ValueError):
        emails.get("msg_1")

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_async_get_retries_an_unavailable_service():
    seen: list[httpx.Request] = []
    replies = [
        httpx.Response(503, json={"code": "delivery_unavailable"}),
        httpx.Response(
            200, json={**status_reply()[1], "status": "sent", "reason": None}
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    emails = AsyncEmails(
        SECRET_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with mock.patch("posthog.emails.asyncio.sleep"):
        status = await emails.get(MESSAGE_ID)

    assert (status.status, status.reason) == ("sent", None)
    assert [(request.method, str(request.url)) for request in seen] == [
        ("GET", STATUS_URL)
    ] * 2


HTML = "<p>Reset it here.</p>"
INVITE = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
INVITE_BASE64 = "QkVHSU46VkNBTEVOREFSDQpFTkQ6VkNBTEVOREFSDQo="


def send_code(emails: Emails, **overrides: Any):
    return emails.send(
        **{
            "to": "user@example.com",
            "template_key": "password-reset",
            "subject": "Reset your password",
            **overrides,
        }
    )


@parameterized.expand(
    [
        ("html_only", {"html": HTML}, {"html": HTML}),
        ("text_only", {"text": "Reset it here."}, {"text": "Reset it here."}),
        (
            "every_field",
            {
                "html": HTML,
                "text": "Reset it here.",
                "from_": "Acme <security@example.com>",
                "reply_to": "help@example.com",
                "cc": "manager@example.com",
                "bcc": ["audit@example.com", "archive@example.com"],
                "capture_engagement": False,
                "distinct_id": "user_123",
            },
            {
                "html": HTML,
                "text": "Reset it here.",
                "from": "Acme <security@example.com>",
                "reply_to": ["help@example.com"],
                "cc": ["manager@example.com"],
                "bcc": ["audit@example.com", "archive@example.com"],
                "capture_engagement": False,
                "distinct_id": "user_123",
            },
        ),
        (
            "several_reply_to",
            {"html": HTML, "reply_to": ["a@example.com", "b@example.com"]},
            {"html": HTML, "reply_to": ["a@example.com", "b@example.com"]},
        ),
        (
            "empty_lists_left_out",
            {"html": HTML, "reply_to": [], "cc": [], "bcc": [], "attachments": []},
            {"html": HTML},
        ),
        (
            "none_as_left_out",
            {
                "html": HTML,
                "text": None,
                "from_": None,
                "reply_to": None,
                "cc": None,
                "bcc": None,
                "attachments": None,
                "capture_engagement": None,
                "distinct_id": None,
            },
            {"html": HTML},
        ),
    ]
)
def test_rendered_content_is_posted_as_is(_name, overrides, extra_body):
    emails, adapter = emails_with([accepted("msg_1")])

    result = send_code(emails, idempotency_key="reset:user_123", **overrides)

    assert result.id == "msg_1"
    [request] = adapter.requests
    assert (request.method, request.url) == ("POST", SEND_URL)
    assert request.headers["Idempotency-Key"] == "reset:user_123"
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "template_key": "password-reset",
        "subject": "Reset your password",
        "capture_engagement": True,
        **extra_body,
    }


@pytest.mark.asyncio
async def test_async_client_posts_rendered_content_and_names_the_provider():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"message_id": "msg_1", "replayed": True})

    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY, migrated_from="resend")
    client.emails._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await client.emails.send(
        to="user@example.com",
        template_key="password-reset",
        subject="Reset your password",
        html=HTML,
        text="Reset it here.",
        from_="Acme <security@example.com>",
        reply_to="help@example.com",
        cc="manager@example.com",
        bcc=["audit@example.com"],
        capture_engagement=False,
        distinct_id="user_123",
        idempotency_key="reset:user_123",
    )
    await client.shutdown()

    assert (result.id, result.replayed) == ("msg_1", True)
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", SEND_URL)
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert request.headers["Idempotency-Key"] == "reset:user_123"
    assert request.headers["X-PostHog-Migrated-From"] == "resend"
    assert json.loads(request.content) == {
        "to": "user@example.com",
        "template_key": "password-reset",
        "subject": "Reset your password",
        "html": HTML,
        "text": "Reset it here.",
        "from": "Acme <security@example.com>",
        "reply_to": ["help@example.com"],
        "cc": ["manager@example.com"],
        "bcc": ["audit@example.com"],
        "capture_engagement": False,
        "distinct_id": "user_123",
    }


@parameterized.expand(
    [
        ("both_forms", {"template_id": TEMPLATE_ID, "html": HTML}, TypeError),
        ("no_form", {"template_key": None}, TypeError),
        ("no_content", {}, TypeError),
        ("no_subject", {"subject": None, "html": HTML}, TypeError),
        ("subject_not_text", {"subject": 1, "html": HTML}, TypeError),
        ("from_not_text", {"html": HTML, "from_": 1}, TypeError),
        ("from_keyword", {"html": HTML, "from": "a@example.com"}, TypeError),
        (
            "key_with_space",
            {"template_key": "reset password", "html": HTML},
            ValueError,
        ),
        (
            "key_with_trailing_newline",
            {"template_key": "welcome\n", "html": HTML},
            ValueError,
        ),
        ("key_too_long", {"template_key": "k" * 401, "html": HTML}, ValueError),
        ("empty_html", {"html": ""}, ValueError),
        ("empty_html_and_text", {"html": "", "text": ""}, ValueError),
        ("blank_reply_to", {"html": HTML, "reply_to": " "}, ValueError),
        ("reply_to_not_text", {"html": HTML, "reply_to": 123}, TypeError),
        ("blank_cc", {"html": HTML, "cc": " "}, ValueError),
        ("bcc_not_text", {"html": HTML, "bcc": [123]}, TypeError),
        (
            "template_data_on_code_form",
            {"html": HTML, "template_data": {"first_name": "Jane"}},
            TypeError,
        ),
        (
            "capture_engagement_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "capture_engagement": False,
            },
            TypeError,
        ),
        (
            "cc_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "cc": "a@example.com",
            },
            TypeError,
        ),
        (
            "bcc_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "bcc": ["a@example.com"],
            },
            TypeError,
        ),
        (
            "attachments_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "attachments": [{"filename": "a.txt", "content": b"hi"}],
            },
            TypeError,
        ),
    ]
)
def test_rejects_a_code_send_that_mixes_or_misses_fields(_name, overrides, error):
    emails, adapter = emails_with([])
    args = {
        "to": "user@example.com",
        "template_key": "password-reset",
        "subject": "Reset your password",
        **overrides,
    }

    with pytest.raises(error):
        emails.send(
            **{name: value for name, value in args.items() if value is not None}
        )

    assert adapter.requests == []


def test_a_refused_send_names_the_field_from_attr():
    refused = {
        "type": "validation_error",
        "code": "invalid_input",
        "detail": "PostHog doesn't take this field, so it doesn't send the email without it.",
        "attr": "cc",
    }
    emails, adapter = emails_with([(400, refused, {}), accepted()])

    with pytest.raises(EmailError) as raised:
        send_code(emails, html=HTML, cc="manager@example.com")

    error = raised.value
    assert (error.name, error.code, error.field) == (
        "validation_error",
        "invalid_input",
        "cc",
    )
    assert len(adapter.requests) == 1


UPLOADS_URL = (
    "https://us.posthog.com/api/projects/@current/messaging/attachments/uploads/"
)
BUCKET_URL = "https://files.example.com/team/1/"
SIGNATURE = "?X-Amz-Expires=900&X-Amz-Signature=abc123"
INVITE_SHA256 = "215efae54e34ac5657da47b49c8d1bd8ea12649ee1efa838c9a47d7233f18afb"
LOGO = b"\x89PNG\r\n\x1a\n"
LOGO_SHA256 = "4c4b6a3be1314ab86138bef4314dde022e600960d8689a2c8f8631802d20dab6"
SIGNED_HEADERS = {
    "content-length": "32",
    "x-amz-checksum-sha256": "IV765U40rFZX2ke0nI0b2OoSZJ7h76g4yaR9cjPxivs=",
}


def present(sha256: str) -> dict[str, Any]:
    return {"sha256": sha256, "status": "present", "expires_at": "2026-12-16T00:00:00Z"}


def upload(sha256: str) -> dict[str, Any]:
    return {
        "sha256": sha256,
        "status": "upload",
        "url": BUCKET_URL + sha256 + SIGNATURE,
        "method": "PUT",
        "headers": SIGNED_HEADERS,
        "url_expires_at": "2026-11-09T12:15:00Z",
    }


def slots(*files: dict[str, Any]) -> Reply:
    return (200, {"files": list(files)}, {})


def stored() -> Reply:
    return (200, None, {})


def bucket_refused(status: int, code: str, message: str) -> Reply:
    return (
        status,
        f"<?xml version='1.0'?><Error><Code>{code}</Code><Message>{message}</Message></Error>".encode(),
        {},
    )


INVITE_AND_LOGO = [
    {
        "filename": "invite.ics",
        "content": INVITE,
        "content_type": "text/calendar; method=REQUEST",
    },
    {"filename": "logo.png", "content": "iVBORw0KGgo=", "content_id": "logo"},
]


def test_uploads_the_files_posthog_lacks_then_sends_their_hashes():
    emails, adapter = emails_with(
        [slots(upload(INVITE_SHA256), present(LOGO_SHA256)), stored(), accepted()]
    )

    result = send_code(emails, html=HTML, attachments=INVITE_AND_LOGO)

    assert result.id == "msg_1"
    ask, put, sent = adapter.requests
    assert (ask.method, ask.url) == ("POST", UPLOADS_URL)
    assert ask.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert json.loads(ask.body) == {
        "files": [
            {"sha256": INVITE_SHA256, "size": 32},
            {"sha256": LOGO_SHA256, "size": 8},
        ]
    }
    assert (put.method, put.url, put.body) == (
        "PUT",
        BUCKET_URL + INVITE_SHA256 + SIGNATURE,
        INVITE,
    )
    assert "Authorization" not in put.headers
    assert {name: put.headers[name] for name in SIGNED_HEADERS} == SIGNED_HEADERS
    assert (sent.method, sent.url) == ("POST", SEND_URL)
    assert json.loads(sent.body)["attachments"] == [
        {
            "sha256": INVITE_SHA256,
            "filename": "invite.ics",
            "content_type": "text/calendar; method=REQUEST",
        },
        {"sha256": LOGO_SHA256, "filename": "logo.png", "content_id": "logo"},
    ]


def test_sends_without_uploading_when_posthog_has_every_file():
    emails, adapter = emails_with([slots(present(INVITE_SHA256)), accepted()])

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": INVITE}]
    )

    assert [(request.method, request.url) for request in adapter.requests] == [
        ("POST", UPLOADS_URL),
        ("POST", SEND_URL),
    ]


def test_the_upload_carries_only_the_signed_url_and_headers_whatever_the_session_holds():
    adapter = FakeAdapter([slots(upload(INVITE_SHA256)), stored(), accepted()])
    session = requests.Session()
    session.mount("https://", adapter)
    session.params = {"api_key": "session-default"}
    session.headers.update(
        {"Authorization": "Bearer session-default", "Content-Type": "text/plain"}
    )
    emails = Emails(SECRET_KEY, session=session)

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": INVITE}]
    )

    put = adapter.requests[1]
    assert put.url == BUCKET_URL + INVITE_SHA256 + SIGNATURE
    assert {
        name.lower(): value for name, value in put.headers.items()
    } == SIGNED_HEADERS


@parameterized.expand(
    [
        ("bytearray", bytearray(INVITE)),
        ("memoryview", memoryview(INVITE)),
        (
            "strided_memoryview",
            memoryview(bytes(b for byte in INVITE for b in (byte, 0)))[::2],
        ),
        ("base64_string", INVITE_BASE64),
    ]
)
def test_file_content_from_bytes_or_base64_uploads_the_same_bytes(_name, content):
    emails, adapter = emails_with([slots(upload(INVITE_SHA256)), stored(), accepted()])

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": content}]
    )

    ask, put, _sent = adapter.requests
    assert json.loads(ask.body) == {"files": [{"sha256": INVITE_SHA256, "size": 32}]}
    assert put.body == INVITE


@parameterized.expand(
    [
        (
            "signed_checksum_mismatch",
            bucket_refused(
                400, "BadDigest", "The Content-SHA256 you specified did not match."
            ),
            400,
            "BadDigest",
        ),
        (
            "expired_url",
            bucket_refused(403, "AccessDenied", "Request has expired"),
            403,
            "AccessDenied",
        ),
    ]
)
def test_a_refused_upload_sends_no_email_and_surfaces_the_storage_answer(
    _name, refusal, status, storage_code
):
    emails, adapter = emails_with([slots(upload(INVITE_SHA256)), refusal])

    with pytest.raises(EmailError) as raised:
        send_code(
            emails,
            html=HTML,
            attachments=[{"filename": "invite.ics", "content": INVITE}],
        )

    error = raised.value
    assert (error.code, error.status_code, error.field) == (
        "upload_failed",
        status,
        "attachments__0",
    )
    assert storage_code in error.message
    assert [request.method for request in adapter.requests] == ["POST", "PUT"]


def test_the_upload_trusts_the_certificates_the_environment_names(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corporate-ca.pem")
    emails, adapter = emails_with([slots(upload(INVITE_SHA256)), stored(), accepted()])

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": INVITE}]
    )

    assert adapter.verify == ["/etc/ssl/corporate-ca.pem"] * 3


def test_an_unreachable_storage_never_shows_the_signed_url_in_the_traceback():
    unreachable = requests.exceptions.ConnectionError(
        f"Max retries exceeded with url: {BUCKET_URL}{INVITE_SHA256}{SIGNATURE}"
    )
    emails, adapter = emails_with(
        [slots(upload(INVITE_SHA256)), unreachable, unreachable, unreachable]
    )

    with pytest.raises(EmailError) as raised:
        send_code(
            emails,
            html=HTML,
            attachments=[{"filename": "invite.ics", "content": INVITE}],
        )

    assert raised.value.code == "network_error"
    assert "X-Amz-Signature" not in "".join(traceback.format_exception(raised.value))
    assert [request.method for request in adapter.requests] == ["POST"] + ["PUT"] * 3


@pytest.mark.asyncio
async def test_async_unreachable_storage_never_shows_the_signed_url_in_the_traceback():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ConnectError(f"Connection refused: {request.url}")
        return httpx.Response(200, json={"files": [upload(INVITE_SHA256)]})

    emails = AsyncEmails(
        SECRET_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(EmailError) as raised:
        await emails.send(
            to="user@example.com",
            template_key="invite",
            subject="You're invited",
            html=HTML,
            attachments=[{"filename": "invite.ics", "content": INVITE}],
        )

    assert raised.value.code == "network_error"
    assert "X-Amz-Signature" not in "".join(traceback.format_exception(raised.value))


def test_an_unavailable_storage_is_retried_before_the_send():
    emails, adapter = emails_with(
        [
            slots(upload(INVITE_SHA256)),
            bucket_refused(503, "SlowDown", "Please reduce your request rate."),
            stored(),
            accepted(),
        ]
    )

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": INVITE}]
    )

    assert [request.method for request in adapter.requests] == [
        "POST",
        "PUT",
        "PUT",
        "POST",
    ]


def test_a_refused_upload_request_uploads_and_sends_nothing():
    refused = {
        "type": "invalid_request",
        "code": "field_not_accepted",
        "detail": "This project can't send files yet, so PostHog didn't prepare the uploads.",
        "attr": "files",
        "why": "Sending files isn't available to this project yet.",
        "fix": "Send the email without files.",
    }
    emails, adapter = emails_with([(400, refused, {})])

    with pytest.raises(EmailError) as raised:
        send_code(
            emails,
            html=HTML,
            attachments=[{"filename": "invite.ics", "content": INVITE}],
        )

    error = raised.value
    assert (error.code, error.field, error.why) == (
        "field_not_accepted",
        "files",
        "Sending files isn't available to this project yet.",
    )
    assert [request.url for request in adapter.requests] == [UPLOADS_URL]


def test_a_project_over_its_daily_upload_bytes_is_told_without_retrying():
    emails, adapter = emails_with([rejected(429, "upload_quota_exceeded"), slots()])

    with pytest.raises(EmailError) as raised:
        send_code(
            emails,
            html=HTML,
            attachments=[{"filename": "invite.ics", "content": INVITE}],
        )

    assert (raised.value.code, raised.value.name) == (
        "upload_quota_exceeded",
        "daily_quota_exceeded",
    )
    assert len(adapter.requests) == 1


def test_a_retried_send_reuses_its_uploads_and_its_idempotency_key():
    emails, adapter = emails_with(
        [
            slots(upload(INVITE_SHA256)),
            stored(),
            rejected(503, "delivery_unavailable"),
            accepted(),
        ]
    )

    send_code(
        emails,
        html=HTML,
        idempotency_key="invite:user_123",
        attachments=[{"filename": "invite.ics", "content": INVITE}],
    )

    _ask, _put, first, retry = adapter.requests
    assert (retry.url, retry.body) == (SEND_URL, first.body)
    assert retry.headers["Idempotency-Key"] == "invite:user_123"


@parameterized.expand(
    [
        (
            "eleven_files",
            [{"filename": f"{i}.txt", "content": b"hi"} for i in range(11)],
            "too_many_attachments",
        ),
        (
            "one_file_too_large_beside_the_body",
            [{"filename": "scan.pdf", "content": bytes(29_182_875)}],
            "message_too_large",
        ),
        (
            "files_too_large_together",
            [{"filename": f"{i}.pdf", "content": bytes(3_000_000)} for i in range(10)],
            "message_too_large",
        ),
    ]
)
def test_refuses_an_email_over_the_limits_before_any_request(_name, attachments, code):
    emails, adapter = emails_with([])

    with pytest.raises(EmailError) as raised:
        send_code(emails, html="Hi!", attachments=attachments)

    error = raised.value
    assert (error.name, error.code, error.field) == (
        "validation_error",
        code,
        "attachments",
    )
    assert error.fix
    assert adapter.requests == []


LARGEST_SCAN_SHA256 = "da8348171897010f0a7968e94886e059512d508ec8f543ec2af1d353cae83f60"


def test_sends_the_largest_file_that_fits_beside_the_body():
    emails, adapter = emails_with([slots(present(LARGEST_SCAN_SHA256)), accepted()])

    send_code(
        emails,
        html="Hi!",
        attachments=[{"filename": "scan.pdf", "content": bytes(29_182_872)}],
    )

    assert json.loads(adapter.requests[0].body)["files"][0]["size"] == 29_182_872


@pytest.mark.asyncio
async def test_async_send_uploads_without_the_secret_key_then_sends_the_hashes():
    seen: list[httpx.Request] = []
    replies = [
        httpx.Response(200, json={"files": [upload(INVITE_SHA256)]}),
        httpx.Response(200),
        httpx.Response(202, json={"message_id": "msg_1"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    emails = AsyncEmails(
        SECRET_KEY,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            params={"api_key": "client-default"},
            headers={
                "Authorization": "Bearer client-default",
                "Content-Type": "text/plain",
            },
        ),
    )

    result = await emails.send(
        to="user@example.com",
        template_key="invite",
        subject="You're invited",
        html=HTML,
        attachments=[{"filename": "invite.ics", "content": INVITE}],
    )

    assert result.id == "msg_1"
    ask, put, sent = seen
    assert (ask.method, ask.url.path) == (
        "POST",
        "/api/projects/@current/messaging/attachments/uploads/",
    )
    assert ask.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert (put.method, str(put.url), put.content) == (
        "PUT",
        BUCKET_URL + INVITE_SHA256 + SIGNATURE,
        INVITE,
    )
    assert {
        name: value for name, value in put.headers.items() if name != "host"
    } == SIGNED_HEADERS
    assert json.loads(sent.content)["attachments"] == [
        {"sha256": INVITE_SHA256, "filename": "invite.ics"}
    ]


@pytest.mark.asyncio
async def test_async_refused_upload_sends_no_email():
    seen: list[httpx.Request] = []
    replies = [
        httpx.Response(200, json={"files": [upload(INVITE_SHA256)]}),
        httpx.Response(
            400, content=b"<Error><Code>BadDigest</Code><Message>No.</Message></Error>"
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    emails = AsyncEmails(
        SECRET_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(EmailError) as raised:
        await emails.send(
            to="user@example.com",
            template_key="invite",
            subject="You're invited",
            html=HTML,
            attachments=[{"filename": "invite.ics", "content": INVITE}],
        )

    assert raised.value.code == "upload_failed"
    assert [request.method for request in seen] == ["POST", "PUT"]


SECRET_CONTENT = b"secret-file-content"


@parameterized.expand(
    [
        (
            "one_file_not_a_list",
            {"filename": "a.txt", "content": SECRET_CONTENT},
            "attachments",
        ),
        ("bytes_not_a_list", SECRET_CONTENT, "attachments"),
        ("file_not_a_mapping", [SECRET_CONTENT], "attachments__0"),
        (
            "list_of_ints",
            [{"filename": "a.txt", "content": list(SECRET_CONTENT)}],
            "attachments__0__content",
        ),
        ("no_content", [{"filename": "a.txt"}], "attachments__0__content"),
        (
            "empty_content",
            [{"filename": "a.txt", "content": b""}],
            "attachments__0__content",
        ),
        (
            "text_not_base64",
            [{"filename": "a.txt", "content": "secret-file-content"}],
            "attachments__0__content",
        ),
        (
            "non_ascii_text",
            [{"filename": "a.txt", "content": "secret-file-content é"}],
            "attachments__0__content",
        ),
        ("no_filename", [{"content": SECRET_CONTENT}], "attachments__0__filename"),
        (
            "empty_filename",
            [{"filename": "", "content": SECRET_CONTENT}],
            "attachments__0__filename",
        ),
        (
            "path",
            [{"filename": "a.txt", "content": SECRET_CONTENT, "path": "/tmp/a.txt"}],
            "attachments__0__path",
        ),
        (
            "unknown_field",
            [
                {
                    "filename": "a.txt",
                    "content": SECRET_CONTENT,
                    "contentType": "text/plain",
                }
            ],
            "attachments__0__contentType",
        ),
        (
            "none_key_before_an_unknown_field",
            [
                {
                    None: "ignored",
                    "filename": "a.txt",
                    "content": SECRET_CONTENT,
                    "contentType": "text/plain",
                }
            ],
            "attachments__0__None",
        ),
        (
            "content_type_not_text",
            [
                {"filename": "a.txt", "content": "aGk="},
                {"filename": "b.txt", "content": SECRET_CONTENT, "content_type": 1},
            ],
            "attachments__1__content_type",
        ),
    ]
)
def test_rejects_an_attachment_it_cannot_send_without_repeating_its_content(
    _name, attachments, field
):
    emails, adapter = emails_with([])

    with pytest.raises(EmailError) as raised:
        send_code(emails, html=HTML, attachments=attachments)

    error = raised.value
    assert (error.name, error.code, error.field) == (
        "validation_error",
        "invalid_attachment",
        field,
    )
    assert "secret-file-content" not in str(error)
    assert "115, 101" not in str(error)
    assert adapter.requests == []
