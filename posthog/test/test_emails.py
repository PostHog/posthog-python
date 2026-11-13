from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import ssl
import threading
import time
import traceback
import tracemalloc
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Union
from unittest import mock
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

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
                "capture_engagement": False,
                "distinct_id": "user_123",
            },
            {
                "html": HTML,
                "text": "Reset it here.",
                "from": "Acme <security@example.com>",
                "reply_to": ["help@example.com"],
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
            "empty_reply_to_left_out",
            {"html": HTML, "reply_to": []},
            {"html": HTML},
        ),
        (
            "none_as_left_out",
            {
                "html": HTML,
                "text": None,
                "from_": None,
                "reply_to": None,
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
        **extra_body,
    }


class _CaptureServer(HTTPServer):
    received: list[Any]


class _CaptureHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append(json.loads(body))
        reply = json.dumps({"message_id": "msg_1", "replayed": False}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def capture_server() -> Iterator[_CaptureServer]:
    server = _CaptureServer(("127.0.0.1", 0), _CaptureHandler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    "overrides, extra_body",
    [
        pytest.param({}, {}, id="no_tracking_or_sensitive_choice"),
        pytest.param(
            {"tracking": None, "capture_engagement": None, "sensitive": None},
            {},
            id="none_as_left_out",
        ),
        pytest.param({"sensitive": True}, {"sensitive": True}, id="marked_sensitive"),
        pytest.param(
            {"sensitive": False}, {"sensitive": False}, id="marked_not_sensitive"
        ),
        pytest.param({"tracking": False}, {"tracking": False}, id="all_tracking_off"),
        pytest.param({"tracking": True}, {"tracking": True}, id="all_tracking_on"),
        pytest.param(
            {"tracking": {"opens": True, "clicks": True, "engagement": False}},
            {"tracking": {"opens": True, "clicks": True, "engagement": False}},
            id="each_kind_of_tracking_chosen",
        ),
        pytest.param(
            {"capture_engagement": False},
            {"capture_engagement": False},
            id="deprecated_capture_engagement",
        ),
    ],
)
def test_a_code_send_delivers_its_tracking_and_sensitive_choice(
    capture_server, overrides, extra_body
):
    host = f"http://127.0.0.1:{capture_server.server_port}"
    with Emails(SECRET_KEY, host=host) as emails:
        result = send_code(emails, html=HTML, **overrides)

    assert result.id == "msg_1"
    assert capture_server.received == [
        {
            "to": "user@example.com",
            "template_key": "password-reset",
            "subject": "Reset your password",
            "html": HTML,
            **extra_body,
        }
    ]


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
        (
            "tracking_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "tracking": False,
            },
            TypeError,
        ),
        (
            "sensitive_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "sensitive": False,
            },
            TypeError,
        ),
        ("tracking_not_bool_or_dict", {"html": HTML, "tracking": "off"}, TypeError),
        ("sensitive_not_bool", {"html": HTML, "sensitive": "yes"}, TypeError),
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


# Scheduled sends: schedule, move, cancel and list.

CANCEL_URL = f"{STATUS_URL}cancel/"
LIST_URL = "https://us.posthog.com/api/projects/@current/messaging/emails/"
LATER = datetime(2026, 12, 1, 9, 0, tzinfo=timezone(timedelta(hours=2)))


@parameterized.expand(
    [
        ("aware_datetime_goes_as_utc", LATER, "2026-12-01T07:00:00Z"),
        ("utc_string_as_given", "2026-12-01T07:00:00Z", "2026-12-01T07:00:00Z"),
        (
            "offset_string_as_given",
            "2026-12-01T09:00:00.250+02:00",
            "2026-12-01T09:00:00.250+02:00",
        ),
        ("space_separated_as_given", "2026-12-01 07:00Z", "2026-12-01 07:00Z"),
    ]
)
def test_send_schedules_both_forms_for_a_time_with_an_offset(_name, value, wire):
    emails, adapter = emails_with(
        [
            accepted(status="scheduled", scheduled_at="2026-12-01T07:00:00Z"),
            accepted(status="scheduled", scheduled_at="2026-12-01T07:00:00Z"),
        ]
    )

    template_result = send(emails, scheduled_at=value)
    code_result = send_code(emails, html=HTML, scheduled_at=value)

    for result in (template_result, code_result):
        assert result.status == "scheduled"
        assert result.scheduled_at == datetime(2026, 12, 1, 7, tzinfo=timezone.utc)
        assert result.warnings == ()
    assert [
        json.loads(request.body)["scheduled_at"] for request in adapter.requests
    ] == [
        wire,
        wire,
    ]


@parameterized.expand(
    [
        ("left_out", lambda emails: send(emails)),
        ("passed_as_none", lambda emails: send(emails, scheduled_at=None)),
        (
            "code_form_as_none",
            lambda emails: send_code(emails, html=HTML, scheduled_at=None),
        ),
    ]
)
def test_a_send_now_carries_no_scheduled_at_and_reads_queued(_name, call):
    emails, adapter = emails_with([accepted()])

    result = call(emails)

    assert (result.status, result.scheduled_at, result.warnings) == ("queued", None, ())
    assert "scheduled_at" not in json.loads(adapter.requests[0].body)


@parameterized.expand(
    [
        ("template_form", lambda emails: send(emails, scheduled_at=LATER)),
        (
            "code_form",
            lambda emails: send_code(emails, html=HTML, scheduled_at=LATER),
        ),
    ]
)
def test_a_server_without_scheduled_sends_warns_that_it_sent_now(_name, call):
    # A PostHog that predates scheduled sends drops scheduled_at, sends at once and answers no status.
    emails, _ = emails_with([accepted()])

    result = call(emails)

    assert (result.status, result.scheduled_at) == ("queued", None)
    assert [w.code for w in result.warnings] == ["scheduling_unsupported"]


def test_a_time_at_or_before_now_reads_as_the_servers_queued_answer():
    emails, _ = emails_with([accepted(status="queued", scheduled_at=None)])

    result = send(emails, scheduled_at="2020-01-01T00:00:00Z")

    assert (result.status, result.scheduled_at, result.warnings) == ("queued", None, ())


@parameterized.expand(
    [
        ("naive_datetime", datetime(2026, 12, 1, 9), ValueError),
        ("string_without_offset", "2026-12-01T09:00:00", ValueError),
        ("date_only_string", "2026-12-01", ValueError),
        ("natural_language", "in 1 min", ValueError),
        ("not_a_date", "2026-13-01T09:00:00Z", ValueError),
        ("empty_string", "", ValueError),
        ("unix_timestamp", 1796115600, TypeError),
        ("date", date(2026, 12, 1), TypeError),
    ]
)
def test_refuses_a_time_without_a_timezone_before_any_request(_name, value, error):
    emails, adapter = emails_with([])

    for call in (
        lambda: send(emails, scheduled_at=value),
        lambda: send_code(emails, html=HTML, scheduled_at=value),
        lambda: emails.update(MESSAGE_ID, scheduled_at=value),
    ):
        with pytest.raises(error, match="scheduled_at"):
            call()

    assert adapter.requests == []


def test_update_moves_a_scheduled_email_and_returns_its_new_time():
    emails, adapter = emails_with(
        [
            (
                200,
                {
                    "id": MESSAGE_ID,
                    "status": "scheduled",
                    "scheduled_at": "2026-12-01T07:00:00Z",
                },
                {},
            )
        ]
    )

    result = emails.update(MESSAGE_ID, scheduled_at=LATER)

    assert (result.id, result.status, result.scheduled_at) == (
        MESSAGE_ID,
        "scheduled",
        datetime(2026, 12, 1, 7, tzinfo=timezone.utc),
    )
    [request] = adapter.requests
    assert (request.method, request.url) == ("PATCH", STATUS_URL)
    assert json.loads(request.body) == {"scheduled_at": "2026-12-01T07:00:00Z"}
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert "Idempotency-Key" not in request.headers


def test_cancel_stops_a_scheduled_email():
    emails, adapter = emails_with([(200, {"id": MESSAGE_ID, "status": "canceled"}, {})])

    result = emails.cancel(MESSAGE_ID)

    assert (result.id, result.status) == (MESSAGE_ID, "canceled")
    [request] = adapter.requests
    assert (request.method, request.url, request.body) == ("POST", CANCEL_URL, None)
    assert "Idempotency-Key" not in request.headers


def list_reply(next_cursor: Optional[str] = None) -> Reply:
    return (
        200,
        {
            "results": [
                {
                    "id": MESSAGE_ID,
                    "to": "user@example.com",
                    "scheduled_at": "2026-12-01T07:00:00Z",
                    "created_at": "2026-11-12T10:00:00.123000Z",
                }
            ],
            "next": next_cursor,
        },
        {},
    )


@parameterized.expand(
    [
        ("first_page", {}, "?status=scheduled", "cursor_2"),
        (
            "next_page",
            {"cursor": "cursor_2", "limit": 50},
            "?status=scheduled&cursor=cursor_2&limit=50",
            None,
        ),
    ]
)
def test_list_reads_scheduled_emails_a_page_at_a_time(_name, args, query, next_cursor):
    emails, adapter = emails_with([list_reply(next_cursor)])

    page = emails.list(**args)

    [request] = adapter.requests
    assert (request.method, request.url) == ("GET", LIST_URL + query)
    [email] = page.results
    assert (email.id, email.to, email.scheduled_at, email.created_at) == (
        MESSAGE_ID,
        "user@example.com",
        datetime(2026, 12, 1, 7, tzinfo=timezone.utc),
        datetime(2026, 11, 12, 10, 0, 0, 123000, tzinfo=timezone.utc),
    )
    assert page.next == next_cursor


@parameterized.expand(
    [
        ("zero", {"limit": 0}, ValueError),
        ("over_a_page", {"limit": 101}, ValueError),
        ("not_an_int", {"limit": "10"}, TypeError),
        ("a_bool", {"limit": True}, TypeError),
        ("empty_cursor", {"cursor": ""}, ValueError),
        ("empty_status", {"status": ""}, ValueError),
        ("another_status", {"status": "sent"}, ValueError),
    ]
)
def test_list_refuses_bad_arguments_before_any_request(_name, args, error):
    emails, adapter = emails_with([])

    with pytest.raises(error):
        emails.list(**args)

    assert adapter.requests == []


@parameterized.expand(
    [
        ("update", lambda emails, id: emails.update(id, scheduled_at=LATER)),
        ("cancel", lambda emails, id: emails.cancel(id)),
    ]
)
def test_update_and_cancel_refuse_an_id_that_is_not_a_message_id(_name, call):
    emails, adapter = emails_with([])

    with pytest.raises(ValueError, match="message_id"):
        call(emails, "msg_1")

    assert adapter.requests == []


def test_update_needs_a_time():
    emails, adapter = emails_with([])

    with pytest.raises(TypeError, match="scheduled_at"):
        emails.update(MESSAGE_ID, scheduled_at=None)

    assert adapter.requests == []


MANAGE_CALLS = [
    (
        "update",
        lambda emails: emails.update(MESSAGE_ID, scheduled_at=LATER),
        (
            200,
            {
                "id": MESSAGE_ID,
                "status": "scheduled",
                "scheduled_at": "2026-12-01T07:00:00Z",
            },
            {},
        ),
    ),
    (
        "cancel",
        lambda emails: emails.cancel(MESSAGE_ID),
        (200, {"id": MESSAGE_ID, "status": "canceled"}, {}),
    ),
]


@parameterized.expand(
    [
        (f"{name}_{case}", call, success, first_reply, retried)
        for name, call, success in MANAGE_CALLS
        for case, first_reply, retried in [
            ("rate_limited", rejected(429, "throttled"), True),
            ("service_unavailable", rejected(503, "delivery_unavailable"), True),
            ("connection_error", requests.exceptions.ConnectionError("down"), True),
            ("already_sending", rejected(409, "already_sending"), False),
            ("canceled", rejected(409, "canceled"), False),
            ("not_found", rejected(404, "not_found"), False),
            ("too_far", rejected(400, "invalid_scheduled_at"), False),
            ("over_quota", rejected(429, "quota_limited"), False),
        ]
    ]
)
def test_update_and_cancel_retry_like_a_send(
    _name, call, success, first_reply, retried
):
    emails, adapter = emails_with([first_reply, success])

    if retried:
        assert call(emails).id == MESSAGE_ID
        assert len(adapter.requests) == 2
    else:
        with pytest.raises(EmailError):
            call(emails)
        assert len(adapter.requests) == 1


@parameterized.expand(
    [
        ("move_while_sending", MANAGE_CALLS[0][1], "already_sending"),
        ("move_after_a_cancel", MANAGE_CALLS[0][1], "canceled"),
        ("cancel_while_sending", MANAGE_CALLS[1][1], "already_sending"),
    ]
)
def test_a_cancel_or_move_too_late_is_a_validation_error(_name, call, code):
    emails, adapter = emails_with([rejected(409, code)])

    with pytest.raises(EmailError) as raised:
        call(emails)

    # As with template_outdated, a retry can't resolve it, so posthog-node names it this way too.
    assert (raised.value.status_code, raised.value.code, raised.value.name) == (
        409,
        code,
        "validation_error",
    )
    assert len(adapter.requests) == 1


SOONER = datetime(2026, 11, 30, 9, 0, tzinfo=timezone.utc)
MOVED_LATER = MANAGE_CALLS[0][2]
OTHER_MESSAGE_ID = "01a18356-c56c-0001-e494-7666bc915199"
NO_ANSWERS = [
    ("connection_error", requests.exceptions.ConnectionError("down")),
    ("timeout", requests.exceptions.ReadTimeout("slow")),
    # A gateway or server error confirms nothing: the move may still be running upstream.
    ("gateway_timeout", (504, None, {})),
    ("bad_gateway", (502, None, {})),
    ("unavailable", rejected(503, "delivery_unavailable")),
    ("server_error", rejected(500, "server_error")),
]


def assert_move_unconfirmed(error: EmailError) -> None:
    assert (error.name, error.code, error.status_code) == (
        "application_error",
        "move_unconfirmed",
        None,
    )
    assert (
        "wasn't confirmed" in error.message and "Nothing was changed" in error.message
    )
    assert error.fix is not None and "cancel it" in error.fix


@parameterized.expand(NO_ANSWERS)
def test_a_move_after_one_that_got_no_answer_is_refused(_name, no_answer):
    # PostHog may still apply the unanswered move after a newer one, which would undo the newer one.
    emails, adapter = emails_with([no_answer], max_retries=0)
    with pytest.raises(EmailError):
        emails.update(MESSAGE_ID, scheduled_at=LATER)

    with pytest.raises(EmailError) as raised:
        emails.update(MESSAGE_ID.upper(), scheduled_at=SOONER)

    assert_move_unconfirmed(raised.value)
    assert len(adapter.requests) == 1


def test_a_move_is_refused_even_when_a_retry_of_the_unanswered_one_got_an_answer():
    emails, adapter = emails_with(
        [requests.exceptions.ReadTimeout("slow"), MOVED_LATER], max_retries=1
    )
    emails.update(MESSAGE_ID, scheduled_at=LATER)

    with pytest.raises(EmailError) as raised:
        emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert_move_unconfirmed(raised.value)
    assert len(adapter.requests) == 2


@parameterized.expand(
    [
        ("same_datetime", LATER, LATER),
        ("string_then_datetime", "2026-12-01T09:00:00+02:00", LATER),
        ("datetime_then_string", LATER, "2026-12-01T07:00:00.000+00:00"),
        ("utc_datetime", LATER, datetime(2026, 12, 1, 7, tzinfo=timezone.utc)),
    ]
)
def test_a_move_to_the_same_time_as_the_unanswered_one_still_goes_out(
    _name, unanswered_time, same_time
):
    emails, adapter = emails_with(
        [requests.exceptions.ReadTimeout("slow"), MOVED_LATER], max_retries=0
    )
    with pytest.raises(EmailError):
        emails.update(MESSAGE_ID, scheduled_at=unanswered_time)

    assert emails.update(MESSAGE_ID, scheduled_at=same_time).id == MESSAGE_ID
    assert len(adapter.requests) == 2


@parameterized.expand(
    [
        ("too_far", rejected(400, "invalid_scheduled_at")),
        ("rate_limited", rejected(429, "throttled")),
        ("not_found", rejected(404, "not_found")),
    ]
)
def test_a_move_after_one_that_was_refused_still_goes_out(_name, refusal):
    # PostHog answered, so the earlier move can't land later.
    emails, adapter = emails_with([refusal, MOVED_LATER], max_retries=0)
    with pytest.raises(EmailError):
        emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert emails.update(MESSAGE_ID, scheduled_at=LATER).id == MESSAGE_ID
    assert len(adapter.requests) == 2


def test_a_move_of_another_email_still_goes_out():
    emails, adapter = emails_with(
        [
            requests.exceptions.ReadTimeout("slow"),
            (
                200,
                {
                    "id": OTHER_MESSAGE_ID,
                    "status": "scheduled",
                    "scheduled_at": "2026-11-30T09:00:00Z",
                },
                {},
            ),
        ],
        max_retries=0,
    )
    with pytest.raises(EmailError):
        emails.update(MESSAGE_ID, scheduled_at=LATER)

    assert emails.update(OTHER_MESSAGE_ID, scheduled_at=SOONER).id == OTHER_MESSAGE_ID
    assert len(adapter.requests) == 2


def test_a_move_called_while_an_earlier_one_waits_is_sent_after_it():
    first_sent, release = threading.Event(), threading.Event()

    class SlowAdapter(FakeAdapter):
        def send(self, request: requests.PreparedRequest, **kwargs: Any):
            if not self.requests:
                first_sent.set()
                release.wait(5)
            return super().send(request, **kwargs)

    adapter = SlowAdapter([requests.exceptions.ReadTimeout("slow"), MOVED_LATER])
    session = requests.Session()
    session.mount("https://", adapter)
    emails = Emails(SECRET_KEY, session=session, max_retries=0)
    outcomes: dict[str, Any] = {}

    def move(name: str, scheduled_at: datetime) -> None:
        try:
            outcomes[name] = emails.update(MESSAGE_ID, scheduled_at=scheduled_at)
        except EmailError as exc:
            outcomes[name] = exc

    first = threading.Thread(target=move, args=("first", LATER))
    first.start()
    assert first_sent.wait(5)
    second = threading.Thread(target=move, args=("second", SOONER))
    second.start()
    second.join(
        0.2
    )  # Without ordering, the second move goes out now, alongside the first.
    release.set()
    first.join(5)
    second.join(5)

    assert isinstance(outcomes["first"], EmailError)
    assert_move_unconfirmed(outcomes["second"])
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_async_a_move_after_one_that_got_no_answer_is_refused():
    first_sent, release = asyncio.Event(), asyncio.Event()
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            first_sent.set()
            await release.wait()
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=MOVED_LATER[1])

    emails = AsyncEmails(
        SECRET_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    first = asyncio.ensure_future(emails.update(MESSAGE_ID, scheduled_at=LATER))
    await first_sent.wait()
    queued = asyncio.ensure_future(emails.update(MESSAGE_ID, scheduled_at=SOONER))
    for _ in range(5):  # Without ordering, the queued move goes out now.
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(EmailError):
        await first
    with pytest.raises(EmailError) as queued_error:
        await queued
    with pytest.raises(EmailError) as later_error:
        await emails.update(MESSAGE_ID.upper(), scheduled_at=SOONER)

    assert_move_unconfirmed(queued_error.value)
    assert_move_unconfirmed(later_error.value)
    assert len(seen) == 1
    assert (await emails.update(MESSAGE_ID, scheduled_at=LATER)).id == MESSAGE_ID
    assert len(seen) == 2


def test_a_canceled_email_forgets_its_unconfirmed_move():
    # A late move can't undo a cancel, so the next move goes out and PostHog says the email is canceled.
    emails, adapter = emails_with(
        [
            requests.exceptions.ReadTimeout("slow"),
            (200, {"id": MESSAGE_ID, "status": "canceled"}, {}),
            rejected(409, "canceled"),
        ],
        max_retries=0,
    )
    with pytest.raises(EmailError):
        emails.update(MESSAGE_ID, scheduled_at=LATER)
    emails.cancel(MESSAGE_ID.upper())

    with pytest.raises(EmailError) as raised:
        emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert (raised.value.code, raised.value.status_code) == ("canceled", 409)
    assert len(adapter.requests) == 3


def test_a_failed_cancel_keeps_the_unconfirmed_move():
    emails, adapter = emails_with(
        [requests.exceptions.ReadTimeout("slow"), rejected(409, "already_sending")],
        max_retries=0,
    )
    with pytest.raises(EmailError):
        emails.update(MESSAGE_ID, scheduled_at=LATER)
    with pytest.raises(EmailError):
        emails.cancel(MESSAGE_ID)

    with pytest.raises(EmailError) as raised:
        emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert_move_unconfirmed(raised.value)
    assert len(adapter.requests) == 2


@pytest.mark.asyncio
async def test_async_a_canceled_email_forgets_its_unconfirmed_move():
    emails, seen = async_emails_with(
        [
            httpx.Response(504),
            httpx.Response(200, json={"id": MESSAGE_ID, "status": "canceled"}),
            httpx.Response(409, json={"code": "canceled"}),
        ]
    )
    emails.max_retries = 0
    with pytest.raises(EmailError):
        await emails.update(MESSAGE_ID, scheduled_at=LATER)
    await emails.cancel(MESSAGE_ID)

    with pytest.raises(EmailError) as raised:
        await emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert (raised.value.code, raised.value.status_code) == ("canceled", 409)
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_async_a_move_after_a_gateway_timeout_is_refused():
    emails, seen = async_emails_with([httpx.Response(504)])
    emails.max_retries = 0
    with pytest.raises(EmailError):
        await emails.update(MESSAGE_ID, scheduled_at=LATER)

    with pytest.raises(EmailError) as raised:
        await emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert_move_unconfirmed(raised.value)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_async_a_move_after_one_canceled_while_it_waited_is_refused():
    # Such as under asyncio.wait_for: the canceled move may still reach PostHog.
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            await asyncio.Event().wait()
        return httpx.Response(200, json=MOVED_LATER[1])

    emails = AsyncEmails(
        SECRET_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(emails.update(MESSAGE_ID, scheduled_at=LATER), 0.05)

    with pytest.raises(EmailError) as raised:
        await emails.update(MESSAGE_ID, scheduled_at=SOONER)

    assert_move_unconfirmed(raised.value)
    assert len(seen) == 1


@parameterized.expand(
    [
        ("service_unavailable", rejected(503, "delivery_unavailable")),
        ("rate_limited", rejected(429, "throttled")),
        ("connection_error", requests.exceptions.ConnectionError("down")),
    ]
)
def test_list_never_retries(_name, first_reply):
    emails, adapter = emails_with([first_reply, list_reply()])

    with pytest.raises(EmailError):
        emails.list()

    assert len(adapter.requests) == 1


@parameterized.expand(
    [
        (
            "update",
            lambda emails: emails.update(MESSAGE_ID, scheduled_at=LATER),
            "missing_secret_key",
        ),
        ("cancel", lambda emails: emails.cancel(MESSAGE_ID), "missing_secret_key"),
        ("list", lambda emails: emails.list(), "missing_secret_key"),
    ]
)
def test_manage_calls_need_the_secret_key_and_an_enabled_client(_name, call, code):
    adapter = FakeAdapter([])
    session = requests.Session()
    session.mount("https://", adapter)

    with pytest.raises(EmailError) as missing:
        call(Emails(None, session=session))
    with pytest.raises(EmailError) as disabled:
        call(Emails(SECRET_KEY, session=session, disabled=True))

    assert (missing.value.code, disabled.value.code) == (code, "client_disabled")
    assert adapter.requests == []


@parameterized.expand(
    [
        *[
            (
                f"row_without_{field}",
                {
                    "results": [
                        {
                            key: value
                            for key, value in list_reply()[1]["results"][0].items()
                            if key != field
                        }
                    ],
                    "next": None,
                },
            )
            for field in ("id", "to", "scheduled_at", "created_at")
        ],
        ("next_not_a_string", {**list_reply()[1], "next": 123}),
        ("next_empty", {**list_reply()[1], "next": ""}),
        ("next_left_out", {"results": list_reply()[1]["results"]}),
        ("results_left_out", {"next": None}),
        ("results_not_a_list", {"results": {}, "next": None}),
    ]
)
def test_a_malformed_list_page_raises_instead_of_dropping_emails(_name, body):
    # A page that loses its next cursor would end a caller's loop early and hide emails that still go out.
    emails, _ = emails_with([(200, body, {})])

    with pytest.raises(EmailError) as raised:
        emails.list()

    assert raised.value.code == "api_error"


@parameterized.expand(
    [
        ("no_status", {"id": MESSAGE_ID}),
        ("still_scheduled", {"id": MESSAGE_ID, "status": "scheduled"}),
        ("no_id", {"status": "canceled"}),
    ]
)
def test_a_cancel_reply_without_a_canceled_email_raises(_name, body):
    emails, _ = emails_with([(200, body, {})])

    with pytest.raises(EmailError) as raised:
        emails.cancel(MESSAGE_ID)

    assert raised.value.code == "api_error"


MOVED = {
    "id": MESSAGE_ID,
    "status": "scheduled",
    "scheduled_at": "2026-12-01T07:00:00Z",
}


@parameterized.expand(
    [
        *[
            (
                f"no_{field}",
                {key: value for key, value in MOVED.items() if key != field},
            )
            for field in MOVED
        ],
        ("scheduled_at_not_a_time", {**MOVED, "scheduled_at": "tomorrow"}),
    ]
)
def test_an_update_reply_without_the_moved_email_raises(_name, body):
    # Returning a result without its send time would tell the caller the move landed when nobody knows when it goes out.
    emails, _ = emails_with([(200, body, {})])

    with pytest.raises(EmailError) as raised:
        emails.update(MESSAGE_ID, scheduled_at=LATER)

    assert raised.value.code == "api_error"


def async_emails_with(replies: list[httpx.Response]):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    emails = AsyncEmails(
        SECRET_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return emails, seen


def unavailable() -> httpx.Response:
    return httpx.Response(503, json={"code": "delivery_unavailable"})


@pytest.mark.asyncio
async def test_async_send_schedules_an_email():
    emails, seen = async_emails_with(
        [
            httpx.Response(
                202,
                json={
                    "message_id": MESSAGE_ID,
                    "status": "scheduled",
                    "scheduled_at": "2026-12-01T07:00:00Z",
                },
            )
        ]
    )

    sent = await emails.send(
        to="user@example.com", template_id=TEMPLATE_ID, scheduled_at=LATER
    )

    assert (sent.status, sent.scheduled_at, sent.warnings) == (
        "scheduled",
        datetime(2026, 12, 1, 7, tzinfo=timezone.utc),
        (),
    )
    assert [(request.method, str(request.url)) for request in seen] == [
        ("POST", SEND_URL)
    ]
    assert json.loads(seen[0].content)["scheduled_at"] == "2026-12-01T07:00:00Z"


@pytest.mark.asyncio
async def test_async_update_retries_an_unavailable_service_and_moves_the_email():
    emails, seen = async_emails_with(
        [
            unavailable(),
            httpx.Response(
                200,
                json={
                    "id": MESSAGE_ID,
                    "status": "scheduled",
                    "scheduled_at": "2026-12-02T07:00:00Z",
                },
            ),
        ]
    )

    with mock.patch("posthog.emails.asyncio.sleep"):
        moved = await emails.update(
            MESSAGE_ID, scheduled_at="2026-12-02T09:00:00+02:00"
        )

    assert moved.scheduled_at == datetime(2026, 12, 2, 7, tzinfo=timezone.utc)
    assert [(request.method, str(request.url)) for request in seen] == [
        ("PATCH", STATUS_URL),
        ("PATCH", STATUS_URL),
    ]
    assert json.loads(seen[0].content) == {"scheduled_at": "2026-12-02T09:00:00+02:00"}
    assert "Idempotency-Key" not in seen[0].headers


@pytest.mark.asyncio
async def test_async_cancel_retries_an_unavailable_service_and_cancels_the_email():
    emails, seen = async_emails_with(
        [
            unavailable(),
            httpx.Response(200, json={"id": MESSAGE_ID, "status": "canceled"}),
        ]
    )

    with mock.patch("posthog.emails.asyncio.sleep"):
        canceled = await emails.cancel(MESSAGE_ID)

    assert canceled.status == "canceled"
    assert [(request.method, str(request.url)) for request in seen] == [
        ("POST", CANCEL_URL),
        ("POST", CANCEL_URL),
    ]


@pytest.mark.asyncio
async def test_async_list_reads_a_page():
    emails, seen = async_emails_with([httpx.Response(200, json=list_reply()[1])])

    page = await emails.list(limit=10)

    assert [email.id for email in page.results] == [MESSAGE_ID]
    assert [str(request.url) for request in seen] == [
        LIST_URL + "?status=scheduled&limit=10"
    ]


@pytest.mark.asyncio
async def test_async_list_never_retries():
    emails, seen = async_emails_with(
        [unavailable(), httpx.Response(200, json=list_reply()[1])]
    )

    with mock.patch("posthog.emails.asyncio.sleep"):
        with pytest.raises(EmailError):
            await emails.list()

    assert len(seen) == 1


@pytest.mark.parametrize("factory", [Emails, AsyncEmails])
@pytest.mark.parametrize(
    "options, message",
    [
        ({"timeout": True}, "timeout must be a finite positive number of seconds."),
        ({"timeout": "3"}, "timeout must be a finite positive number of seconds."),
        (
            {"timeout": float("inf")},
            "timeout must be a finite positive number of seconds.",
        ),
        (
            {"timeout": float("nan")},
            "timeout must be a finite positive number of seconds.",
        ),
        ({"timeout": 0}, "timeout must be a finite positive number of seconds."),
        ({"max_retries": True}, "max_retries must be a nonnegative integer."),
        ({"max_retries": None}, "max_retries must be a nonnegative integer."),
        ({"max_retries": -1}, "max_retries must be a nonnegative integer."),
        (
            {"migrated_from": "re send"},
            "migrated_from must be visible ASCII characters, such as 'resend'.",
        ),
    ],
)
def test_invalid_transport_options_are_rejected_at_construction(
    factory, options, message
):
    with pytest.raises(ValueError) as raised:
        factory(SECRET_KEY, **options)
    assert str(raised.value) == message


@pytest.mark.parametrize("warnings", [None, {}, "not a warning list"])
def test_non_list_warnings_are_ignored(warnings):
    emails, _ = emails_with([accepted(warnings=warnings, replayed=1)])
    result = send(emails)
    assert result.warnings == ()
    assert result.replayed is False


def test_warning_parsing_keeps_order_fallback_fields_and_safe_defaults(caplog):
    emails, _ = emails_with(
        [
            accepted(
                warnings=[
                    None,
                    {
                        "code": "first",
                        "detail": "preferred",
                        "message": "ignored",
                        "fix": "update",
                    },
                    {"code": "", "detail": 42, "message": "fallback", "fix": False},
                    {},
                ]
            )
        ]
    )
    with caplog.at_level(logging.WARNING, logger="posthog"):
        result = send(emails)
    assert [(w.code, w.message, w.fix) for w in result.warnings] == [
        ("first", "preferred", "update"),
        ("warning", "fallback", None),
        ("warning", "", None),
    ]
    assert len(caplog.records) == 3
    assert all(
        record.getMessage().endswith(message)
        for record, message in zip(
            caplog.records,
            [
                "PostHog email warning first: preferred",
                "PostHog email warning warning: fallback",
                "PostHog email warning warning: ",
            ],
        )
    )


def test_code_send_preserves_utf8_and_compact_json_bytes():
    emails, adapter = emails_with([accepted()])
    send_code(emails, subject="Bonjour é", html="<p>你好</p>")
    assert adapter.requests[0].body == (
        '{"to":"user@example.com","template_key":"password-reset",'
        '"subject":"Bonjour é","html":"<p>你好</p>"}'
    ).encode("utf-8")


def test_code_send_validation_order_still_checks_recipient_before_template_and_content():
    emails, adapter = emails_with([])
    with pytest.raises(TypeError, match="to must be one email address as a string"):
        send_code(emails, to=[], template_key="bad key", subject=123, html=123)
    with pytest.raises(ValueError, match="template_key can only have letters"):
        send_code(emails, template_key="bad key", subject=123, html=123)
    assert adapter.requests == []


def test_a_refused_send_names_the_field_from_attr():
    posthog = FakePostHog()
    posthog.send_fields = SEND_FIELDS - {"cc"}

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, cc="manager@example.com")

    assert (raised.value.code, raised.value.field) == ("invalid_input", "cc")
    assert posthog.delivered == []


POSTHOG_HOST = "https://us.posthog.com"
UPLOADS_PATH = "/api/projects/@current/messaging/attachments/uploads/"
SEND_PATH = "/api/projects/@current/messaging/send/email/"
STORAGE_URL = "https://files.example.com/team/1"
STORAGE_SIGNING_KEY = b"fake-storage-signing-key"
URL_LIFETIME_SECONDS = 900
SEND_FIELDS = frozenset(
    {
        "to",
        "template_key",
        "subject",
        "html",
        "text",
        "from",
        "reply_to",
        "cc",
        "bcc",
        "capture_engagement",
        "sensitive",
        "tracking",
        "distinct_id",
        "attachments",
    }
)
REFERENCE_FIELDS = frozenset({"sha256", "filename", "content_type", "content_id"})
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of_json(cls, status: int, payload: dict[str, Any]) -> Answer:
        return cls(
            status, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        )


def api_error(
    status: int, code: str, detail: str, kind: str = "validation_error", **extra: Any
) -> Answer:
    return Answer.of_json(
        status, {"type": kind, "code": code, "detail": detail, **extra}
    )


def storage_error(status: int, code: str, message: str) -> Answer:
    return Answer(
        status,
        f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
        f"<Message>{message}</Message></Error>".encode(),
        {"Content-Type": "application/xml"},
    )


UNAUTHORIZED = api_error(
    401,
    "authentication_failed",
    "Incorrect authentication credentials.",
    kind="authentication_error",
)


@dataclass(frozen=True)
class Incoming:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def is_authorized(self) -> bool:
        return self.headers.get("authorization") == f"Bearer {SECRET_KEY}"


class Unreachable(Exception):
    pass


Handler = Callable[[Incoming], Answer]
Fault = Callable[[Incoming, Handler], Answer]


def answers(answer: Answer) -> Fault:
    return lambda _incoming, _handle: answer


def loses_the_reply(answer: Answer) -> Fault:
    def fault(incoming: Incoming, handle: Handler) -> Answer:
        handle(incoming)
        return answer

    return fault


def unreachable(incoming: Incoming, _handle: Handler) -> Answer:
    raise Unreachable(f"Max retries exceeded with url: {incoming.url}")


def alters_the_bytes(incoming: Incoming, handle: Handler) -> Answer:
    return handle(replace(incoming, body=incoming.body[::-1]))


@dataclass(frozen=True)
class DeliveredFile:
    filename: str
    content: bytes
    content_type: Optional[str] = None
    content_id: Optional[str] = None


@dataclass(frozen=True)
class DeliveredEmail:
    message_id: str
    to: str
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    html: Optional[str]
    text: Optional[str]
    attachments: tuple[DeliveredFile, ...]


def checksum(content: bytes) -> str:
    return base64.b64encode(hashlib.sha256(content).digest()).decode()


def storage_signature(
    method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str]
) -> str:
    canonical = "\n".join(
        [
            method,
            path,
            urlencode(sorted(query.items())),
            headers.get("content-length", ""),
            headers.get("x-amz-checksum-sha256", ""),
        ]
    )
    return hmac.new(STORAGE_SIGNING_KEY, canonical.encode(), hashlib.sha256).hexdigest()


def json_or_none(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def files_asked_for(body: bytes) -> Optional[list[tuple[str, int]]]:
    payload = json_or_none(body)
    if not isinstance(payload, dict) or set(payload) != {"files"}:
        return None
    files = payload["files"]
    if not isinstance(files, list) or not files:
        return None
    if not all(
        isinstance(file, dict)
        and set(file) == {"sha256", "size"}
        and isinstance(file["sha256"], str)
        and SHA256_HEX.fullmatch(file["sha256"])
        and type(file["size"]) is int
        and file["size"] > 0
        for file in files
    ):
        return None
    return [(file["sha256"], file["size"]) for file in files]


class FakePostHog:
    def __init__(
        self, storage_url: str = STORAGE_URL, posthog_host: str = POSTHOG_HOST
    ) -> None:
        self.storage_url = storage_url
        self.posthog_host = posthog_host
        self._bucket: dict[str, bytes] = {}
        self._content_types: dict[str, Optional[str]] = {}
        self.writes = 0
        self.delivered: list[DeliveredEmail] = []
        self.project_can_send_files = True
        self.send_fields = SEND_FIELDS
        self._sent_by_idempotency_key: dict[str, str] = {}
        self._faults: dict[str, list[Fault]] = {
            "uploads": [],
            "send": [],
            "storage": [],
        }
        self._signing_delay = 0.0

    @property
    def stored_files(self) -> list[bytes]:
        return sorted(self._bucket.values())

    @property
    def stored_content_types(self) -> list[Optional[str]]:
        return list(self._content_types.values())

    def already_has(self, content: bytes) -> None:
        self._bucket[hashlib.sha256(content).hexdigest()] = content

    def inject(self, route: str, *faults: Fault) -> None:
        self._faults[route].extend(faults)

    def sign_urls_in_the_past(self, minutes: int) -> None:
        self._signing_delay = minutes * 60.0

    def handle(self, incoming: Incoming) -> Answer:
        route, handler = self._route(incoming)
        faults = self._faults.get(route)
        if faults:
            return faults.pop(0)(incoming, handler)
        return handler(incoming)

    def _route(self, incoming: Incoming) -> tuple[str, Handler]:
        if incoming.url.startswith(self.storage_url + "/"):
            if incoming.method != "PUT":
                return "storage", lambda _incoming: storage_error(
                    405,
                    "MethodNotAllowed",
                    "The specified method is not allowed against this resource.",
                )
            return "storage", self._store
        if incoming.url.startswith(self.posthog_host + "/") and incoming.path in (
            UPLOADS_PATH,
            SEND_PATH,
        ):
            route = "uploads" if incoming.path == UPLOADS_PATH else "send"
            if incoming.method != "POST":
                return route, lambda _incoming: api_error(
                    405,
                    "method_not_allowed",
                    f'Method "{incoming.method}" not allowed.',
                    kind="invalid_request",
                )
            return route, (self._answer_uploads if route == "uploads" else self._send)
        return "unknown", lambda _incoming: api_error(
            404, "not_found", "Not found.", kind="invalid_request"
        )

    def _answer_uploads(self, incoming: Incoming) -> Answer:
        if not incoming.is_authorized:
            return UNAUTHORIZED
        if not self.project_can_send_files:
            return api_error(
                400,
                "field_not_accepted",
                "This project can't send files yet, so PostHog didn't prepare the uploads.",
                kind="invalid_request",
                attr="files",
                why="Sending files isn't available to this project yet.",
                fix="Send the email without files.",
            )
        files = files_asked_for(incoming.body)
        if files is None:
            return api_error(
                400, "invalid_input", "Name each file by sha256 and size.", attr="files"
            )
        return Answer.of_json(
            200, {"files": [self._slot(sha256, size) for sha256, size in files]}
        )

    def _slot(self, sha256: str, size: int) -> dict[str, Any]:
        if sha256 in self._bucket:
            return {
                "sha256": sha256,
                "status": "present",
                "expires_at": "2026-12-16T00:00:00Z",
            }
        url = f"{self.storage_url}/{sha256}"
        headers = {
            "content-length": str(size),
            "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(sha256)).decode(),
        }
        query = {
            "X-Amz-Date": str(int(time.time() - self._signing_delay)),
            "X-Amz-Expires": str(URL_LIFETIME_SECONDS),
            "X-Amz-SignedHeaders": "content-length;x-amz-checksum-sha256",
        }
        signature = storage_signature("PUT", urlsplit(url).path, query, headers)
        return {
            "sha256": sha256,
            "status": "upload",
            "url": f"{url}?{urlencode({**query, 'X-Amz-Signature': signature})}",
            "method": "PUT",
            "headers": headers,
            "url_expires_at": "2026-11-09T12:15:00Z",
        }

    def _store(self, incoming: Incoming) -> Answer:
        if "authorization" in incoming.headers:
            return storage_error(
                400,
                "InvalidArgument",
                "Only one auth mechanism allowed; only the X-Amz-Algorithm query "
                "parameter, Signature query string parameter or the Authorization "
                "header should be specified",
            )
        query = dict(parse_qsl(urlsplit(incoming.url).query, keep_blank_values=True))
        signature = query.pop("X-Amz-Signature", "")
        expected = storage_signature(
            incoming.method, incoming.path, query, incoming.headers
        )
        if not hmac.compare_digest(signature, expected):
            return storage_error(
                403,
                "SignatureDoesNotMatch",
                "The request signature we calculated does not match the signature you provided.",
            )
        if time.time() > int(query["X-Amz-Date"]) + int(query["X-Amz-Expires"]):
            return storage_error(403, "AccessDenied", "Request has expired")
        if incoming.headers["content-length"] != str(
            len(incoming.body)
        ) or incoming.headers["x-amz-checksum-sha256"] != checksum(incoming.body):
            return storage_error(
                400,
                "BadDigest",
                "The SHA256 you specified did not match the calculated checksum.",
            )
        key = incoming.path.rsplit("/", 1)[1]
        self._bucket[key] = incoming.body
        self._content_types[key] = incoming.headers.get("content-type")
        self.writes += 1
        return Answer(200)

    def _send(self, incoming: Incoming) -> Answer:
        if not incoming.is_authorized:
            return UNAUTHORIZED
        idempotency_key = incoming.headers.get("idempotency-key", "")
        if idempotency_key in self._sent_by_idempotency_key:
            return Answer.of_json(
                200,
                {
                    "message_id": self._sent_by_idempotency_key[idempotency_key],
                    "replayed": True,
                },
            )
        payload = json_or_none(incoming.body)
        if not isinstance(payload, dict):
            return api_error(400, "invalid_input", "Send a JSON object.")
        unknown = sorted(set(payload) - self.send_fields)
        if unknown:
            return api_error(
                400,
                "invalid_input",
                f"PostHog doesn't take {unknown[0]}.",
                attr=unknown[0],
            )
        references = payload.get("attachments", [])
        for index, reference in enumerate(references):
            refused = self._refused_reference(index, reference)
            if refused is not None:
                return refused
        message_id = str(uuid4())
        self.delivered.append(self._email(message_id, payload, references))
        if idempotency_key:
            self._sent_by_idempotency_key[idempotency_key] = message_id
        return Answer.of_json(202, {"message_id": message_id})

    def _refused_reference(self, index: int, reference: Any) -> Optional[Answer]:
        if (
            not isinstance(reference, dict)
            or not {"sha256", "filename"} <= set(reference) <= REFERENCE_FIELDS
        ):
            return api_error(
                400,
                "invalid_input",
                "An attachment names sha256 and filename.",
                attr=f"attachments__{index}",
            )
        if reference["sha256"] not in self._bucket:
            return api_error(
                400,
                "attachment_not_uploaded",
                "PostHog doesn't hold this file. Upload it first.",
                attr=f"attachments__{index}",
            )
        return None

    def _email(
        self, message_id: str, payload: dict[str, Any], references: list[Any]
    ) -> DeliveredEmail:
        return DeliveredEmail(
            message_id=message_id,
            to=payload["to"],
            cc=tuple(payload.get("cc", ())),
            bcc=tuple(payload.get("bcc", ())),
            subject=payload["subject"],
            html=payload.get("html"),
            text=payload.get("text"),
            attachments=tuple(
                DeliveredFile(
                    filename=reference["filename"],
                    content=self._bucket[reference["sha256"]],
                    content_type=reference.get("content_type"),
                    content_id=reference.get("content_id"),
                )
                for reference in references
            ),
        )


def lowercase(headers: Any) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


class FakePostHogAdapter(requests.adapters.BaseAdapter):
    def __init__(self, posthog: FakePostHog) -> None:
        super().__init__()
        self.posthog = posthog

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        incoming = Incoming(
            request.method or "",
            request.url or "",
            lowercase(request.headers),
            request.body if isinstance(request.body, bytes) else b"",
        )
        try:
            answer = self.posthog.handle(incoming)
        except Unreachable as exc:
            raise requests.exceptions.ConnectionError(str(exc))
        response = requests.Response()
        response.status_code = answer.status
        response._content = answer.body
        response.headers.update(answer.headers)
        response.request = request
        response.url = incoming.url
        return response

    def close(self) -> None:
        pass


def httpx_transport(posthog: FakePostHog) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        incoming = Incoming(
            request.method, str(request.url), lowercase(request.headers), request.read()
        )
        try:
            answer = posthog.handle(incoming)
        except Unreachable as exc:
            raise httpx.ConnectError(str(exc), request=request)
        return httpx.Response(
            answer.status, content=answer.body, headers=answer.headers
        )

    return httpx.MockTransport(handle)


def session_on(posthog: FakePostHog, prefix: str = "https://") -> requests.Session:
    session = requests.Session()
    session.mount(prefix, FakePostHogAdapter(posthog))
    return session


def emails_on(posthog: FakePostHog, **options: Any) -> Emails:
    return Emails(SECRET_KEY, session=session_on(posthog), **options)


def async_emails_on(
    posthog: FakePostHog, max_retries: int = 2, **client_options: Any
) -> AsyncEmails:
    return AsyncEmails(
        SECRET_KEY,
        max_retries=max_retries,
        http_client=httpx.AsyncClient(
            transport=httpx_transport(posthog), **client_options
        ),
    )


INVITE_SHA256 = "215efae54e34ac5657da47b49c8d1bd8ea12649ee1efa838c9a47d7233f18afb"
LOGO = b"\x89PNG\r\n\x1a\n"
INVITE_FILE = {"filename": "invite.ics", "content": INVITE}
INVITE_AND_LOGO = [
    {
        "filename": "invite.ics",
        "content": INVITE,
        "content_type": "text/calendar; method=REQUEST",
    },
    {"filename": "logo.png", "content": "iVBORw0KGgo=", "content_id": "logo"},
]
DELIVERED_INVITE_AND_LOGO = (
    DeliveredFile("invite.ics", INVITE, content_type="text/calendar; method=REQUEST"),
    DeliveredFile("logo.png", LOGO, content_id="logo"),
)


def test_delivers_each_file_with_its_name_type_and_content_id():
    posthog = FakePostHog()

    result = send_code(emails_on(posthog), html=HTML, attachments=INVITE_AND_LOGO)

    [email] = posthog.delivered
    assert result.id == email.message_id
    assert email.attachments == DELIVERED_INVITE_AND_LOGO


def test_delivers_cc_and_bcc_beside_the_files():
    posthog = FakePostHog()

    send_code(
        emails_on(posthog),
        html=HTML,
        cc="manager@example.com",
        bcc=["audit@example.com", "archive@example.com"],
        attachments=[INVITE_FILE],
    )

    [email] = posthog.delivered
    assert (email.cc, email.bcc) == (
        ("manager@example.com",),
        ("audit@example.com", "archive@example.com"),
    )
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


@parameterized.expand(
    [
        (
            "one_address_each",
            {"cc": "manager@example.com", "bcc": "audit@example.com"},
            ("manager@example.com",),
            ("audit@example.com",),
        ),
        (
            "lists",
            {
                "cc": ["manager@example.com", "finance@example.com"],
                "bcc": ["audit@example.com"],
            },
            ("manager@example.com", "finance@example.com"),
            ("audit@example.com",),
        ),
        ("empty_lists", {"cc": [], "bcc": [], "attachments": []}, (), ()),
        ("none", {"cc": None, "bcc": None, "attachments": None}, (), ()),
    ]
)
def test_delivers_cc_and_bcc_as_given(_name, overrides, cc, bcc):
    posthog = FakePostHog()

    send_code(emails_on(posthog), html=HTML, **overrides)

    [email] = posthog.delivered
    assert (email.to, email.cc, email.bcc, email.attachments) == (
        "user@example.com",
        cc,
        bcc,
        (),
    )


@pytest.mark.asyncio
async def test_async_delivers_cc_and_bcc():
    posthog = FakePostHog()

    await send_code(
        async_emails_on(posthog),
        html=HTML,
        cc="manager@example.com",
        bcc=["audit@example.com", "archive@example.com"],
    )

    [email] = posthog.delivered
    assert (email.cc, email.bcc) == (
        ("manager@example.com",),
        ("audit@example.com", "archive@example.com"),
    )


def test_stores_only_the_files_posthog_lacks_under_their_own_hash():
    posthog = FakePostHog()
    posthog.already_has(LOGO)

    send_code(emails_on(posthog), html=HTML, attachments=INVITE_AND_LOGO)

    assert posthog.stored_files == sorted([INVITE, LOGO])
    assert posthog.writes == 1
    [email] = posthog.delivered
    assert email.attachments == DELIVERED_INVITE_AND_LOGO


def test_sends_without_uploading_when_posthog_has_every_file():
    posthog = FakePostHog()
    posthog.already_has(INVITE)

    send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    assert posthog.writes == 0
    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


def test_the_upload_carries_only_the_signed_url_and_headers_whatever_the_session_holds():
    posthog = FakePostHog()
    session = session_on(posthog)
    session.params = {"api_key": "session-default"}
    session.headers.update(
        {"Authorization": "Bearer session-default", "Content-Type": "text/plain"}
    )

    send_code(Emails(SECRET_KEY, session=session), html=HTML, attachments=[INVITE_FILE])

    assert (posthog.stored_files, posthog.stored_content_types) == ([INVITE], [None])
    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


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
    posthog = FakePostHog()

    send_code(
        emails_on(posthog),
        html=HTML,
        attachments=[{"filename": "invite.ics", "content": content}],
    )

    assert posthog.stored_files == [INVITE]
    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


def bytes_altered_on_the_way(posthog: FakePostHog) -> None:
    posthog.inject("storage", alters_the_bytes)


def url_signed_16_minutes_ago(posthog: FakePostHog) -> None:
    posthog.sign_urls_in_the_past(16)


@parameterized.expand(
    [
        (
            "bytes_altered_on_the_way",
            bytes_altered_on_the_way,
            400,
            "BadDigest",
            "The SHA256 you specified did not match the calculated checksum.",
        ),
        (
            "expired_url",
            url_signed_16_minutes_ago,
            403,
            "AccessDenied",
            "Request has expired",
        ),
    ]
)
def test_a_refused_upload_sends_no_email_and_surfaces_the_storage_answer(
    _name, arrange, status, storage_code, storage_message
):
    posthog = FakePostHog()
    arrange(posthog)

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    error = raised.value
    assert (error.name, error.code, error.status_code, error.field, error.why) == (
        "application_error",
        "attachment_upload_failed",
        status,
        "attachments__0",
        storage_message,
    )
    assert storage_code in error.message
    assert "proxy" in error.fix
    assert (posthog.delivered, posthog.stored_files) == ([], [])


@parameterized.expand(
    [
        ("line_wrapped", base64.encodebytes(bytes(100)).decode()),
        ("unpadded", "QUI"),
        ("url_safe", base64.urlsafe_b64encode(b"\xfb\xff").decode()),
    ]
)
def test_refuses_base64_not_in_one_padded_line_and_says_how_to_encode(_name, content):
    posthog = FakePostHog()

    with pytest.raises(EmailError) as raised:
        send_code(
            emails_on(posthog),
            html=HTML,
            attachments=[{"filename": "a.bin", "content": content}],
        )

    error = raised.value
    assert (error.code, error.field) == (
        "invalid_attachment",
        "attachments__0__content",
    )
    assert "base64.b64encode" in error.message
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path, key_path = directory / "storage.pem", directory / "storage.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def storage_handler(posthog: FakePostHog) -> type[BaseHTTPRequestHandler]:
    class Storage(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            origin = f"https://127.0.0.1:{self.server.server_address[1]}"
            answer = posthog.handle(
                Incoming("PUT", origin + self.path, lowercase(self.headers), body)
            )
            self.send_response(answer.status)
            for name, value in answer.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(answer.body)))
            self.end_headers()
            self.wfile.write(answer.body)

        def log_message(self, *args: Any) -> None:
            pass

    return Storage


@pytest.fixture
def posthog_with_tls_storage(tmp_path):
    certificate, key = self_signed_certificate(tmp_path)
    posthog = FakePostHog()
    server = ThreadingHTTPServer(("127.0.0.1", 0), storage_handler(posthog))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    posthog.storage_url = f"https://127.0.0.1:{server.server_address[1]}/bucket"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield posthog, certificate
    finally:
        server.shutdown()
        server.server_close()


def test_the_upload_trusts_the_certificates_the_environment_names(
    posthog_with_tls_storage, monkeypatch
):
    posthog, certificate = posthog_with_tls_storage
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certificate))
    emails = Emails(SECRET_KEY, session=session_on(posthog, POSTHOG_HOST))

    send_code(emails, html=HTML, attachments=[INVITE_FILE])

    assert posthog.stored_files == [INVITE]
    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


def test_the_upload_refuses_a_storage_whose_certificate_nothing_trusts(
    posthog_with_tls_storage, monkeypatch
):
    posthog, _certificate = posthog_with_tls_storage
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    emails = Emails(SECRET_KEY, session=session_on(posthog, POSTHOG_HOST))

    with pytest.raises(EmailError) as raised:
        send_code(emails, html=HTML, attachments=[INVITE_FILE])

    assert (raised.value.code, raised.value.field) == (
        "network_error",
        "attachments__0",
    )
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def test_an_unreachable_storage_never_shows_the_signed_url_in_the_traceback():
    posthog = FakePostHog()
    posthog.inject("storage", unreachable, unreachable, unreachable)

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    error = raised.value
    assert (error.code, error.field) == ("network_error", "attachments__0")
    assert "file storage" in error.message
    assert "X-Amz-Signature" not in "".join(traceback.format_exception(error))
    assert (posthog.delivered, posthog.stored_files) == ([], [])


@pytest.mark.asyncio
async def test_async_unreachable_storage_never_shows_the_signed_url_in_the_traceback():
    posthog = FakePostHog()
    posthog.inject("storage", unreachable)

    with pytest.raises(EmailError) as raised:
        await send_code(
            async_emails_on(posthog, max_retries=0),
            html=HTML,
            attachments=[INVITE_FILE],
        )

    error = raised.value
    assert (error.code, error.field) == ("network_error", "attachments__0")
    assert "file storage" in error.message
    assert "X-Amz-Signature" not in "".join(traceback.format_exception(error))
    assert (posthog.delivered, posthog.stored_files) == ([], [])


@parameterized.expand(
    [
        (
            "slow_down",
            answers(storage_error(503, "SlowDown", "Please reduce your request rate.")),
        ),
        (
            "internal_error",
            answers(storage_error(500, "InternalError", "Please try again.")),
        ),
        (
            "too_many_requests",
            answers(storage_error(429, "SlowDown", "Please reduce your request rate.")),
        ),
        ("connection_dropped", unreachable),
    ]
)
def test_a_storage_that_asks_for_a_retry_is_retried_before_the_send(_name, fault):
    posthog = FakePostHog()
    posthog.inject("storage", fault)

    send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    assert posthog.stored_files == [INVITE]
    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


def test_a_project_that_cannot_send_files_uploads_and_sends_nothing():
    posthog = FakePostHog()
    posthog.project_can_send_files = False

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    error = raised.value
    assert (error.code, error.field, error.why) == (
        "field_not_accepted",
        "attachments",
        "Sending files isn't available to this project yet.",
    )
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def test_an_upload_refusal_naming_a_file_names_the_first_attachment_with_those_bytes():
    posthog = FakePostHog()
    posthog.inject(
        "uploads",
        answers(
            api_error(
                400,
                "message_too_large",
                "This file is too large to send.",
                attr="files__1__size",
            )
        ),
    )

    with pytest.raises(EmailError) as raised:
        send_code(
            emails_on(posthog),
            html=HTML,
            attachments=[
                INVITE_FILE,
                {"filename": "invite-copy.ics", "content": INVITE},
                {"filename": "logo.png", "content": LOGO},
            ],
        )

    assert (raised.value.code, raised.value.field) == (
        "message_too_large",
        "attachments__2",
    )
    assert (posthog.delivered, posthog.stored_files) == ([], [])


@parameterized.expand(
    [
        ("throttled", 429, "SlowDown", "rate_limit_exceeded"),
        ("unavailable", 503, "ServiceUnavailable", "service_unavailable"),
    ]
)
def test_a_storage_out_of_retries_names_its_passing_fault(
    _name, status, storage_code, name
):
    posthog = FakePostHog()
    posthog.inject(
        "storage", answers(storage_error(status, storage_code, "Try again."))
    )

    with pytest.raises(EmailError) as raised:
        send_code(
            emails_on(posthog, max_retries=0), html=HTML, attachments=[INVITE_FILE]
        )

    assert (raised.value.name, raised.value.code) == (name, "attachment_upload_failed")
    assert (posthog.delivered, posthog.stored_files) == ([], [])


@parameterized.expand(
    [
        ("posthog_without_the_upload_route", "Endpoint not found."),
        ("sending_from_code_off_for_the_project", "Not found."),
    ]
)
def test_an_upload_route_404_reports_posthogs_answer_on_attachments_and_sends_nothing(
    _name, detail
):
    posthog = FakePostHog()
    posthog.inject(
        "uploads",
        answers(api_error(404, "not_found", detail, kind="invalid_request", attr=None)),
    )

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    error = raised.value
    assert (error.name, error.code, error.status_code, error.field, error.message) == (
        "not_found",
        "not_found",
        404,
        "attachments",
        detail,
    )
    assert error.why and error.fix
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def test_an_upload_route_404_without_an_answer_still_names_attachments():
    posthog = FakePostHog()
    posthog.inject("uploads", answers(Answer(404, b"<h1>Not Found</h1>")))

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    error = raised.value
    assert (error.code, error.status_code, error.field) == (
        "not_found",
        404,
        "attachments",
    )
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def test_a_project_over_its_daily_upload_bytes_is_told_without_retrying():
    posthog = FakePostHog()
    posthog.inject(
        "uploads",
        answers(
            api_error(
                429,
                "upload_quota_exceeded",
                "The project uploaded its daily limit of file bytes.",
                kind="throttled_error",
            )
        ),
    )

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    assert (raised.value.code, raised.value.name) == (
        "upload_quota_exceeded",
        "daily_quota_exceeded",
    )
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def test_a_send_retried_after_a_lost_reply_replays_with_its_idempotency_key():
    posthog = FakePostHog()
    posthog.inject(
        "send",
        loses_the_reply(
            api_error(503, "delivery_unavailable", "Try again.", kind="server_error")
        ),
    )

    result = send_code(
        emails_on(posthog),
        html=HTML,
        idempotency_key="invite:user_123",
        attachments=[INVITE_FILE],
    )

    [email] = posthog.delivered
    assert (result.id, result.replayed) == (email.message_id, True)
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


def test_the_same_bytes_in_two_attachments_upload_once():
    posthog = FakePostHog()

    send_code(
        emails_on(posthog),
        html=HTML,
        attachments=[
            {"filename": "logo.png", "content": LOGO, "content_id": "logo"},
            {"filename": "logo-copy.png", "content": LOGO},
        ],
    )

    assert (posthog.stored_files, posthog.writes) == ([LOGO], 1)
    [email] = posthog.delivered
    assert email.attachments == (
        DeliveredFile("logo.png", LOGO, content_id="logo"),
        DeliveredFile("logo-copy.png", LOGO),
    )


def test_a_failed_upload_names_the_first_attachment_with_those_bytes():
    posthog = FakePostHog()
    posthog.already_has(INVITE)
    posthog.sign_urls_in_the_past(16)

    with pytest.raises(EmailError) as raised:
        send_code(
            emails_on(posthog),
            html=HTML,
            attachments=[
                INVITE_FILE,
                {"filename": "logo.png", "content": LOGO},
                {"filename": "logo-copy.png", "content": LOGO},
            ],
        )

    assert (raised.value.code, raised.value.field) == (
        "attachment_upload_failed",
        "attachments__1",
    )
    assert posthog.delivered == []


def test_refuses_eleven_files_before_reading_any_of_them():
    posthog = FakePostHog()
    attachments: list[Any] = [
        {"filename": f"{i}.txt", "content": b"hi"} for i in range(10)
    ]

    with pytest.raises(EmailError) as raised:
        send_code(
            emails_on(posthog),
            html=HTML,
            attachments=attachments + [{"filename": "late.txt", "content": None}],
        )

    assert raised.value.code == "too_many_attachments"
    assert (posthog.delivered, posthog.stored_files) == ([], [])


@parameterized.expand(
    [
        (
            "eleven_files",
            [{"filename": f"{i}.txt", "content": b"hi"} for i in range(11)],
            "too_many_attachments",
        ),
        (
            "one_file_too_large_beside_the_body",
            [{"filename": "scan.pdf", "content": bytes(29_182_821)}],
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
    posthog = FakePostHog()

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html="Hi!", attachments=attachments)

    error = raised.value
    assert (error.name, error.code, error.field) == (
        "validation_error",
        code,
        "attachments",
    )
    assert error.fix
    assert (posthog.delivered, posthog.stored_files) == ([], [])


def test_refuses_an_oversized_email_without_copying_its_files():
    posthog = FakePostHog()
    emails = emails_on(posthog)
    scan = memoryview(bytearray(8 * 1024 * 1024))

    tracemalloc.start()
    try:
        with pytest.raises(EmailError) as raised:
            send_code(
                emails,
                html=HTML,
                attachments=[
                    {"filename": f"scan-{i}.pdf", "content": scan} for i in range(10)
                ],
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert raised.value.code == "message_too_large"
    assert peak < len(scan)
    assert posthog.stored_files == []
    assert posthog.delivered == []


def test_refuses_oversized_padded_base64_without_copying_it():
    posthog = FakePostHog()
    emails = emails_on(posthog)
    scan = "AAAA" * 10_000_000 + "AA=="

    tracemalloc.start()
    try:
        with pytest.raises(EmailError) as raised:
            send_code(
                emails,
                html=HTML,
                attachments=[{"filename": "scan.pdf", "content": scan}],
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert raised.value.code == "message_too_large"
    assert peak < len(scan) // 4
    assert posthog.stored_files == []
    assert posthog.delivered == []


def test_refuses_extra_base64_padding_without_copying_it():
    posthog = FakePostHog()
    emails = emails_on(posthog)
    content = "YWJj" + "=" * 40_000_000

    tracemalloc.start()
    try:
        with pytest.raises(EmailError) as raised:
            send_code(
                emails,
                html=HTML,
                attachments=[{"filename": "a.txt", "content": content}],
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (raised.value.code, raised.value.field) == (
        "invalid_attachment",
        "attachments__0__content",
    )
    assert "base64.b64encode" in raised.value.message
    assert peak < len(content) // 4
    assert posthog.stored_files == []
    assert posthog.delivered == []


LARGEST_SCAN = bytes(29_182_815)


def test_sends_the_largest_file_that_fits_beside_the_body():
    posthog = FakePostHog()

    send_code(
        emails_on(posthog),
        html="Hi!",
        attachments=[{"filename": "scan.pdf", "content": LARGEST_SCAN}],
    )

    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("scan.pdf", LARGEST_SCAN),)


SCAN_BESIDE_A_78_BYTE_BODY = bytes(29_182_761)


@parameterized.expand(
    [
        ("html", "html", "text"),
        ("text", "text", "html"),
    ]
)
def test_counts_a_body_like_a_file_in_the_size_estimate(
    _name, body_field, other_body_field
):
    posthog = FakePostHog()
    emails = emails_on(posthog)
    attachments = [{"filename": "scan.pdf", "content": SCAN_BESIDE_A_78_BYTE_BODY}]

    send_code(
        emails, attachments=attachments, **{body_field: "a" * 57, other_body_field: "b"}
    )
    with pytest.raises(EmailError) as raised:
        send_code(
            emails,
            attachments=attachments,
            **{body_field: "é" * 57, other_body_field: "b"},
        )

    assert raised.value.code == "message_too_large"
    assert len(posthog.delivered) == 1


def test_counts_the_html_twice_without_text_since_posthog_derives_the_text():
    posthog = FakePostHog()
    emails = emails_on(posthog)
    attachments = [{"filename": "scan.pdf", "content": SCAN_BESIDE_A_78_BYTE_BODY}]

    with pytest.raises(EmailError) as raised:
        send_code(emails, html="a" * 57, attachments=attachments)
    send_code(emails, html="a" * 57, text="b", attachments=attachments)

    assert raised.value.code == "message_too_large"
    assert len(posthog.delivered) == 1


def test_uploads_only_to_an_https_storage_url():
    posthog = FakePostHog(storage_url="http://files.example.com/team/1")

    with pytest.raises(EmailError) as raised:
        send_code(
            emails_on(posthog),
            html=HTML,
            attachments=[
                {"filename": "notes.txt", "content": b"notes"},
                {"filename": "invite.ics", "content": INVITE},
            ],
        )

    error = raised.value
    assert (error.code, error.field) == ("api_error", "attachments__0")
    assert "https" in error.message and "file storage" in (error.fix or "")
    assert "files.example.com" not in str(error) + str(vars(error))
    assert (posthog.delivered, posthog.stored_files, posthog.writes) == ([], [], 0)


def test_a_plain_http_host_for_local_development_uploads_over_plain_http():
    posthog = FakePostHog(
        storage_url="http://localhost:8333/team/1",
        posthog_host="http://localhost:8000",
    )
    emails = Emails(
        SECRET_KEY,
        host="http://localhost:8000",
        session=session_on(posthog, "http://"),
    )

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": INVITE}]
    )

    [email] = posthog.delivered
    assert email.attachments == (DeliveredFile("invite.ics", INVITE),)


@parameterized.expand(
    [
        ("42_ascii_bytes_take_75", "a" * 42, 29_182_815),
        ("43_ascii_bytes_take_150", "a" * 43, 29_182_758),
        ("21_two_byte_characters_take_75", "é" * 21, 29_182_815),
        ("22_two_byte_characters_take_150", "é" * 22, 29_182_758),
    ]
)
def test_a_subject_takes_75_bytes_of_the_limit_per_42_utf8_bytes(
    _name, subject, largest_file_size
):
    posthog = FakePostHog()
    emails = emails_on(posthog)

    send_code(
        emails,
        html="Hi!",
        subject=subject,
        attachments=[{"filename": "scan.pdf", "content": bytes(largest_file_size)}],
    )
    with pytest.raises(EmailError) as raised:
        send_code(
            emails,
            html="Hi!",
            subject=subject,
            attachments=[
                {"filename": "scan.pdf", "content": bytes(largest_file_size + 1)}
            ],
        )

    assert raised.value.code == "message_too_large"
    assert len(posthog.delivered) == 1


class PostHogWithARedirectingStorage(BaseHTTPRequestHandler):
    emails_sent = 0
    redirect_followed = False

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith("/attachments/uploads/"):
            slot = {
                "sha256": INVITE_SHA256,
                "status": "upload",
                "url": f"http://{self.headers['Host']}/bucket/{INVITE_SHA256}",
                "method": "PUT",
                "headers": {"content-length": str(len(INVITE))},
                "url_expires_at": "2026-11-09T12:15:00Z",
            }
            self.answer(200, json.dumps({"files": [slot]}).encode())
        else:
            type(self).emails_sent += 1
            self.answer(202, json.dumps({"message_id": "msg_1"}).encode())

    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        type(self).redirect_followed = True
        self.answer(200, b"<html>Sign in</html>")

    def answer(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class PostHogWithAStorageThatHangsUp(PostHogWithARedirectingStorage):
    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(500)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"<Error>")
        self.close_connection = True


def test_a_storage_answer_cut_short_names_the_attachment_even_on_a_streaming_session():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PostHogWithAStorageThatHangsUp)
    PostHogWithAStorageThatHangsUp.emails_sent = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session = requests.Session()
    session.stream = True
    session.trust_env = False
    emails = Emails(
        SECRET_KEY,
        host=f"http://127.0.0.1:{server.server_address[1]}",
        max_retries=0,
        session=session,
    )

    try:
        with pytest.raises(EmailError) as raised:
            send_code(emails, html=HTML, attachments=[INVITE_FILE])
    finally:
        server.shutdown()
        server.server_close()

    assert (raised.value.code, raised.value.field) == (
        "network_error",
        "attachments__0",
    )
    assert PostHogWithAStorageThatHangsUp.emails_sent == 0


@pytest.mark.asyncio
async def test_async_upload_never_follows_a_storage_redirect_even_when_the_client_does():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PostHogWithARedirectingStorage)
    PostHogWithARedirectingStorage.emails_sent = 0
    PostHogWithARedirectingStorage.redirect_followed = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    emails = AsyncEmails(
        SECRET_KEY,
        host=f"http://127.0.0.1:{server.server_address[1]}",
        max_retries=0,
        http_client=httpx.AsyncClient(follow_redirects=True, trust_env=False),
    )

    try:
        with pytest.raises(EmailError) as raised:
            await emails.send(
                to="user@example.com",
                template_key="password-reset",
                subject="Reset your password",
                html=HTML,
                attachments=[{"filename": "invite.ics", "content": INVITE}],
            )
    finally:
        server.shutdown()
        server.server_close()

    assert (raised.value.code, raised.value.field) == (
        "attachment_upload_failed",
        "attachments__0",
    )
    assert PostHogWithARedirectingStorage.emails_sent == 0
    assert not PostHogWithARedirectingStorage.redirect_followed


class ServerElsewhere(BaseHTTPRequestHandler):
    requests_received = 0

    def do_PUT(self) -> None:
        type(self).requests_received += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT

    def log_message(self, *args: Any) -> None:
        pass


class PostHogWithStorageRedirectingElsewhere(PostHogWithARedirectingStorage):
    elsewhere = ""

    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(307)
        self.send_header("Location", f"{self.elsewhere}/bucket/copy")
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextmanager
def serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def posthog_redirecting_storage_elsewhere() -> Iterator[tuple[str, str]]:
    ServerElsewhere.requests_received = 0
    PostHogWithStorageRedirectingElsewhere.emails_sent = 0
    with serving(ServerElsewhere) as elsewhere:
        PostHogWithStorageRedirectingElsewhere.elsewhere = elsewhere
        with serving(PostHogWithStorageRedirectingElsewhere) as posthog:
            yield posthog, elsewhere


def error_text(error: EmailError) -> str:
    return " ".join(str(part) for part in (error, error.message, error.why, error.fix))


def test_an_upload_never_follows_storage_to_another_server():
    session = requests.Session()
    session.trust_env = False

    with posthog_redirecting_storage_elsewhere() as (posthog, elsewhere):
        with pytest.raises(EmailError) as raised:
            send_code(
                Emails(SECRET_KEY, host=posthog, max_retries=0, session=session),
                html=HTML,
                attachments=[INVITE_FILE],
            )

    assert (raised.value.code, raised.value.field) == (
        "attachment_upload_failed",
        "attachments__0",
    )
    assert elsewhere not in error_text(raised.value)
    assert "/bucket/" not in error_text(raised.value)
    assert ServerElsewhere.requests_received == 0
    assert PostHogWithStorageRedirectingElsewhere.emails_sent == 0


@pytest.mark.asyncio
async def test_async_upload_never_follows_storage_to_another_server():
    with posthog_redirecting_storage_elsewhere() as (posthog, elsewhere):
        emails = AsyncEmails(
            SECRET_KEY,
            host=posthog,
            max_retries=0,
            http_client=httpx.AsyncClient(follow_redirects=True, trust_env=False),
        )
        with pytest.raises(EmailError) as raised:
            await emails.send(
                to="user@example.com",
                template_key="password-reset",
                subject="Reset your password",
                html=HTML,
                attachments=[INVITE_FILE],
            )

    assert (raised.value.code, raised.value.field) == (
        "attachment_upload_failed",
        "attachments__0",
    )
    assert elsewhere not in error_text(raised.value)
    assert "/bucket/" not in error_text(raised.value)
    assert ServerElsewhere.requests_received == 0
    assert PostHogWithStorageRedirectingElsewhere.emails_sent == 0


@pytest.mark.asyncio
async def test_async_upload_carries_only_the_signed_url_and_headers_whatever_the_client_holds():
    posthog = FakePostHog()
    emails = async_emails_on(
        posthog,
        params={"api_key": "client-default"},
        headers={
            "Authorization": "Bearer client-default",
            "Content-Type": "text/plain",
        },
    )

    result = await send_code(emails, html=HTML, attachments=INVITE_AND_LOGO)

    assert (posthog.stored_files, posthog.stored_content_types) == (
        sorted([INVITE, LOGO]),
        [None, None],
    )
    [email] = posthog.delivered
    assert result.id == email.message_id
    assert email.attachments == DELIVERED_INVITE_AND_LOGO


@pytest.mark.asyncio
async def test_async_refused_upload_sends_no_email():
    posthog = FakePostHog()
    posthog.sign_urls_in_the_past(16)

    with pytest.raises(EmailError) as raised:
        await send_code(async_emails_on(posthog), html=HTML, attachments=[INVITE_FILE])

    assert (raised.value.code, raised.value.field, raised.value.status_code) == (
        "attachment_upload_failed",
        "attachments__0",
        403,
    )
    assert (posthog.delivered, posthog.stored_files) == ([], [])


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
    posthog = FakePostHog()

    with pytest.raises(EmailError) as raised:
        send_code(emails_on(posthog), html=HTML, attachments=attachments)

    error = raised.value
    assert (error.name, error.code, error.field) == (
        "validation_error",
        "invalid_attachment",
        field,
    )
    assert "secret-file-content" not in str(error)
    assert "115, 101" not in str(error)
    assert (posthog.delivered, posthog.stored_files) == ([], [])
