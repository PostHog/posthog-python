from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator, Optional, Union
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
        (
            "template_data_on_code_form",
            {"html": HTML, "template_data": {"first_name": "Jane"}},
            TypeError,
        ),
        ("cc_on_code_form", {"html": HTML, "cc": "a@example.com"}, TypeError),
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
        ("row_without_fields", {"results": [{"id": MESSAGE_ID}], "next": None}),
        ("next_not_a_string", {**list_reply()[1], "next": 123}),
        ("next_empty", {**list_reply()[1], "next": ""}),
        ("next_left_out", {"results": list_reply()[1]["results"]}),
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
    ]
)
def test_a_cancel_reply_without_a_canceled_email_raises(_name, body):
    emails, _ = emails_with([(200, body, {})])

    with pytest.raises(EmailError) as raised:
        emails.cancel(MESSAGE_ID)

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
