from __future__ import annotations

import asyncio
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
from posthog.emails import AsyncEmails, EmailError, Emails, EmailValidationError
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
    adapter = FakeAdapter(replies)
    session = requests.Session()
    session.mount("https://", adapter)
    return Emails(SECRET_KEY, session=session, **options), adapter


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
    [request] = adapter.requests
    assert (request.method, request.url) == ("POST", SEND_URL)
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert request.headers["Idempotency-Key"] == "welcome:user_123"
    assert request.headers["User-Agent"] == f"posthog-python/{VERSION}"
    assert "X-PostHog-Migrated-From" not in request.headers
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "message_data": {"total": 19.5, "expires_at": "2026-11-20T00:00:00+00:00"},
        **extra_body,
    }


def test_a_template_send_that_sets_capture_engagement_uses_the_email_send_route():
    emails, adapter = emails_with([accepted("msg_1")])

    result = send(
        emails,
        template_data={"name": "Ada"},
        distinct_id="user_123",
        idempotency_key="welcome:user_123",
        capture_engagement=False,
    )

    assert result.id == "msg_1"
    [request] = adapter.requests
    assert (request.method, request.url) == (
        "POST",
        "https://us.posthog.com/api/projects/@current/messaging/send/email/",
    )
    assert request.headers["Idempotency-Key"] == "welcome:user_123"
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "template": {"id": TEMPLATE_ID},
        "message_data": {"name": "Ada"},
        "identifiers": {"distinct_id": "user_123"},
        "capture_engagement": False,
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
    adapter = FakeAdapter([rejected(503, "delivery_unavailable")])

    with mock.patch.object(
        requests.adapters.HTTPAdapter,
        "send",
        autospec=True,
        side_effect=lambda _transport, request, **_: adapter.send(request),
    ) as transport:
        with pytest.raises(EmailError):
            send(client.emails)

    [request] = adapter.requests
    assert (request.url or "").startswith("https://posthog.example.com/api/")
    assert transport.call_args.kwargs["timeout"] == 3
    assert request.headers["X-PostHog-Migrated-From"] == "resend"


@parameterized.expand([("empty", ""), ("line_break", "resend\n"), ("not_a_string", 1)])
def test_rejects_a_migrated_from_that_is_not_a_header_value(_name, migrated_from):
    with pytest.raises(ValueError):
        Emails(SECRET_KEY, migrated_from=migrated_from)


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


@pytest.mark.parametrize(("retry_after", "waited"), [("5", 5.0), ("120", 30.0)])
def test_waits_for_retry_after_up_to_30_seconds(retry_after, waited, no_sleep):
    emails, adapter = emails_with(
        [rejected(429, "throttled", **{"Retry-After": retry_after}), accepted()]
    )

    assert send(emails).id == "msg_1"

    no_sleep.assert_called_once_with(waited)
    assert len(adapter.requests) == 2


def test_max_retries_zero_leaves_retries_to_the_caller():
    emails, adapter = emails_with(
        [rejected(503, "delivery_unavailable")], max_retries=0
    )

    with pytest.raises(EmailError):
        send(emails)

    assert len(adapter.requests) == 1


@parameterized.expand(
    [
        ("turns_retries_off", 2, 0, False),
        ("turns_retries_on", 0, 1, True),
    ]
)
def test_a_send_overrides_the_clients_max_retries(
    _name, client_max_retries, send_max_retries, retried
):
    emails, adapter = emails_with(
        [rejected(503, "delivery_unavailable"), accepted()],
        max_retries=client_max_retries,
    )

    if retried:
        assert send(emails, max_retries=send_max_retries).id == "msg_1"
        assert len(adapter.requests) == 2
    else:
        with pytest.raises(EmailError) as raised:
            send(emails, max_retries=send_max_retries)
        assert raised.value.code == "delivery_unavailable"
        assert len(adapter.requests) == 1
    assert "max_retries" not in json.loads(adapter.requests[0].body)


def test_an_acceptance_without_a_message_id_is_not_retried():
    emails, adapter = emails_with([(202, {}, {}), accepted()])

    with pytest.raises(EmailError) as raised:
        send(emails)

    assert raised.value.code == "api_error"
    assert len(adapter.requests) == 1


def test_warnings_reach_the_caller(caplog):
    warning = {
        "code": "template_superseded",
        "detail": "Newer content.",
        "fix": "Upload.",
    }
    emails, _ = emails_with([accepted("msg_1", warnings=[warning])])

    with caplog.at_level(logging.WARNING, logger="posthog"):
        result = send(emails)

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


def test_a_disabled_client_refuses_the_send():
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
        ("several_recipients", {"to": ["a@example.com"]}, "invalid_form", "to"),
        ("empty_recipient", {"to": " "}, "invalid_form", "to"),
        (
            "template_name",
            {"template_id": "welcome"},
            "invalid_template_id",
            "template_id",
        ),
        ("negative_max_retries", {"max_retries": -1}, "invalid_form", "max_retries"),
        (
            "capture_engagement_not_bool",
            {"capture_engagement": "no"},
            "invalid_form",
            "capture_engagement",
        ),
        ("max_retries_not_int", {"max_retries": True}, "invalid_form", "max_retries"),
        (
            "template_data_list",
            {"template_data": [1, 2]},
            "invalid_form",
            "template_data",
        ),
        (
            "key_with_space",
            {"idempotency_key": "a b"},
            "invalid_idempotency_key",
            "idempotency_key",
        ),
        (
            "key_with_trailing_newline",
            {"idempotency_key": "a\n"},
            "invalid_idempotency_key",
            "idempotency_key",
        ),
        (
            "key_too_long",
            {"idempotency_key": "k" * 256},
            "invalid_idempotency_key",
            "idempotency_key",
        ),
    ]
)
def test_rejects_invalid_arguments_before_sending(_name, overrides, code, field):
    emails, adapter = emails_with([])

    with pytest.raises(EmailValidationError) as raised:
        send(emails, **overrides)

    assert_validation_error(raised.value, code, field)
    assert adapter.requests == []


def assert_validation_error(
    error: EmailValidationError, code: str, field: Optional[str]
) -> None:
    assert isinstance(error, ValueError)
    assert (error.name, error.code, error.field, error.status_code) == (
        "validation_error",
        code,
        field,
        None,
    )


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
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
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
    assert json.loads(seen[0].content)["identifiers"] == {"distinct_id": "user_123"}


@pytest.mark.asyncio
async def test_async_send_overrides_the_clients_max_retries():
    handler = mock.Mock(
        return_value=httpx.Response(503, json={"code": "delivery_unavailable"})
    )
    emails = AsyncEmails(
        SECRET_KEY,
        max_retries=2,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with mock.patch("posthog.emails.asyncio.sleep"):
        with pytest.raises(EmailError):
            await emails.send(
                to="user@example.com", template_id=TEMPLATE_ID, max_retries=0
            )

    handler.assert_called_once()


@pytest.mark.asyncio
async def test_async_disabled_client_refuses_the_send():
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


CODE_SEND_URL = "https://us.posthog.com/api/projects/@current/messaging/send/email/"
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
                "identifiers": {"distinct_id": "user_123"},
            },
        ),
        (
            "several_reply_to",
            {"html": HTML, "reply_to": ["a@example.com", "b@example.com"]},
            {"html": HTML, "reply_to": ["a@example.com", "b@example.com"]},
        ),
        (
            "empty_address_lists",
            {"html": HTML, "reply_to": [], "cc": [], "bcc": []},
            {"html": HTML, "reply_to": [], "cc": [], "bcc": []},
        ),
    ]
)
def test_rendered_content_is_posted_as_is(_name, overrides, extra_body):
    emails, adapter = emails_with([accepted("msg_1")])

    result = send_code(emails, idempotency_key="reset:user_123", **overrides)

    assert result.id == "msg_1"
    [request] = adapter.requests
    assert (request.method, request.url) == ("POST", CODE_SEND_URL)
    assert request.headers["Idempotency-Key"] == "reset:user_123"
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "template_key": "password-reset",
        "subject": "Reset your password",
        **extra_body,
    }


WELCOME = {
    "template_key": "welcome",
    "subject": "Welcome, {{ variables.first_name }}",
    "html": "<p>Hi {{ variables.first_name }}</p>",
}
# The server's content_hash v1 test vectors, shared with posthog-node.
WELCOME_HASH = "v1:f74cd913f3aa2e5667a588f2865634e56693df77793ca594d18e9b5f89e3d2e6"
WELCOME_CRLF_TEXT_HASH = (
    "v1:8112876ee1b2dd7327f571b04b546ed057bfff5a904d9f0ae507b6a449ab6453"
)
UPLOAD_URL = (
    "https://us.posthog.com/api/projects/@current/messaging_templates/by_key/welcome/"
)


def send_local_template(emails: Emails, **overrides: Any):
    return emails.send(
        **{
            "to": "user@example.com",
            **WELCOME,
            "template_data": {"first_name": "Jane"},
            **overrides,
        }
    )


@parameterized.expand(
    [
        ("html_only", {}, WELCOME_HASH),
        ("crlf_text", {"text": "\r\n\r"}, WELCOME_CRLF_TEXT_HASH),
        ("empty_text", {"text": ""}, WELCOME_HASH),
    ]
)
def test_a_local_template_is_sent_by_key_and_content_hash(_name, overrides, hash):
    emails, adapter = emails_with([accepted("msg_1")])

    result = send_local_template(
        emails, from_="Acme <hello@example.com>", distinct_id="user_123", **overrides
    )

    assert result.id == "msg_1"
    [request] = adapter.requests
    assert (request.method, request.url) == ("POST", CODE_SEND_URL)
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "template": {"key": "welcome", "hash": hash},
        "message_data": {"first_name": "Jane"},
        "from": "Acme <hello@example.com>",
        "identifiers": {"distinct_id": "user_123"},
    }


def uploaded() -> Reply:
    return (201, {"id": "tpl_1", "key": "welcome", "result": "created"}, {})


def test_an_outdated_local_template_is_uploaded_then_sent_again_with_the_same_key():
    emails, adapter = emails_with(
        [rejected(409, "template_outdated"), uploaded(), accepted("msg_1")]
    )

    result = send_local_template(emails, text="Hi {{ variables.first_name }}")

    assert result.id == "msg_1"
    first_send, upload, second_send = adapter.requests
    assert (upload.method, upload.url) == ("PUT", UPLOAD_URL)
    assert json.loads(upload.body) == {
        "name": "welcome",
        "subject": "Welcome, {{ variables.first_name }}",
        "html": "<p>Hi {{ variables.first_name }}</p>",
        "text": "Hi {{ variables.first_name }}",
        "if_not_superseded": True,
        "if_code_managed": True,
    }
    assert "Idempotency-Key" not in upload.headers
    assert upload.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert second_send.body == first_send.body
    assert (
        second_send.headers["Idempotency-Key"] == first_send.headers["Idempotency-Key"]
    )


def test_a_sends_max_retries_also_covers_its_upload():
    emails, adapter = emails_with(
        [rejected(409, "template_outdated"), rejected(503, "delivery_unavailable")],
        max_retries=2,
    )

    with pytest.raises(EmailError) as raised:
        send_local_template(emails, max_retries=0)

    assert raised.value.status_code == 503
    assert [request.method for request in adapter.requests] == ["POST", "PUT"]


def test_every_request_of_a_send_says_where_the_caller_migrated_from():
    emails, adapter = emails_with(
        [rejected(409, "template_outdated"), uploaded(), accepted()],
        migrated_from="resend",
    )

    send_local_template(emails)

    assert [
        request.headers["X-PostHog-Migrated-From"] for request in adapter.requests
    ] == [
        "resend",
        "resend",
        "resend",
    ]


@parameterized.expand(
    [
        ("refused_upload", rejected(409, "template_edited_in_posthog"), 2),
        ("outdated_again", None, 3),
    ]
)
def test_a_local_template_uploads_at_most_once_per_send(
    _name, upload_reply, requests_made
):
    replies = (
        [rejected(409, "template_outdated"), upload_reply]
        if upload_reply
        else [
            rejected(409, "template_outdated"),
            uploaded(),
            rejected(409, "template_outdated"),
        ]
    )
    emails, adapter = emails_with(replies)

    with pytest.raises(EmailError) as raised:
        send_local_template(emails)

    assert (raised.value.status_code, raised.value.name) == (409, "validation_error")
    assert len(adapter.requests) == requests_made


def test_rendered_content_is_never_uploaded():
    emails, adapter = emails_with([rejected(409, "template_outdated")])

    with pytest.raises(EmailError):
        send_code(emails, html=HTML)

    assert [request.method for request in adapter.requests] == ["POST"]


@pytest.mark.asyncio
async def test_async_uploads_an_outdated_local_template_then_sends_again():
    seen: list[httpx.Request] = []
    replies = [
        httpx.Response(409, json={"code": "template_outdated"}),
        httpx.Response(201, json={"id": "tpl_1", "result": "created"}),
        httpx.Response(202, json={"message_id": "msg_1"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AsyncEmails(
            SECRET_KEY, http_client=client, migrated_from="resend"
        ).send(to="user@example.com", **WELCOME, template_data={"first_name": "Jane"})

    assert result.id == "msg_1"
    assert [(request.method, str(request.url)) for request in seen] == [
        ("POST", CODE_SEND_URL),
        ("PUT", UPLOAD_URL),
        ("POST", CODE_SEND_URL),
    ]
    first_send, upload, second_send = seen
    assert {request.headers["X-PostHog-Migrated-From"] for request in seen} == {
        "resend"
    }
    assert json.loads(first_send.content)["template"] == {
        "key": "welcome",
        "hash": WELCOME_HASH,
    }
    assert "Idempotency-Key" not in upload.headers
    assert second_send.content == first_send.content
    assert (
        second_send.headers["Idempotency-Key"] == first_send.headers["Idempotency-Key"]
    )


@parameterized.expand(
    [
        (
            "both_forms",
            {"template_id": TEMPLATE_ID, "html": HTML},
            "invalid_form",
            None,
        ),
        ("no_form", {"template_key": None}, "invalid_form", None),
        ("no_content", {}, "invalid_form", "html"),
        ("no_subject", {"subject": None, "html": HTML}, "invalid_form", "subject"),
        ("subject_not_text", {"subject": 1, "html": HTML}, "invalid_form", "subject"),
        ("from_not_text", {"html": HTML, "from_": 1}, "invalid_form", "from_"),
        (
            "from_keyword",
            {"html": HTML, "from": "a@example.com"},
            "invalid_form",
            "from",
        ),
        (
            "key_with_space",
            {"template_key": "a b", "html": HTML},
            "invalid_template_key",
            "template_key",
        ),
        (
            "key_with_trailing_newline",
            {"template_key": "a\n", "html": HTML},
            "invalid_template_key",
            "template_key",
        ),
        (
            "key_too_long",
            {"template_key": "k" * 401, "html": HTML},
            "invalid_template_key",
            "template_key",
        ),
        ("empty_html", {"html": ""}, "invalid_form", "html"),
        ("empty_html_and_text", {"html": "", "text": ""}, "invalid_form", "html"),
        ("blank_cc", {"html": HTML, "cc": " "}, "invalid_form", "cc"),
        ("cc_not_text", {"html": HTML, "cc": 123}, "invalid_form", "cc"),
        (
            "template_data_list",
            {"html": HTML, "template_data": [1]},
            "invalid_form",
            "template_data",
        ),
        (
            "cc_on_template_form",
            {
                "template_id": TEMPLATE_ID,
                "template_key": None,
                "subject": None,
                "cc": "a@example.com",
            },
            "invalid_form",
            "cc",
        ),
    ]
)
def test_rejects_a_code_send_that_mixes_or_misses_fields(_name, overrides, code, field):
    emails, adapter = emails_with([])
    args = {
        "to": "user@example.com",
        "template_key": "password-reset",
        "subject": "Reset your password",
        **overrides,
    }

    with pytest.raises(EmailValidationError) as raised:
        emails.send(
            **{name: value for name, value in args.items() if value is not None}
        )

    assert_validation_error(raised.value, code, field)
    assert adapter.requests == []


COPIES_HEADERS_AND_TAGS = {
    "cc": "manager@example.com",
    "bcc": ["audit@example.com", "archive@example.com"],
    "headers": {"X-Entity-Ref-ID": "order-123"},
    "tags": [{"name": "category", "value": "password_reset"}],
}
COPIES_HEADERS_AND_TAGS_BODY = {
    "cc": ["manager@example.com"],
    "bcc": ["audit@example.com", "archive@example.com"],
    "headers": {"X-Entity-Ref-ID": "order-123"},
    "tags": [{"name": "category", "value": "password_reset"}],
}


def test_rendered_content_carries_copies_headers_and_tags():
    emails, adapter = emails_with([accepted()])

    send_code(emails, html=HTML, **COPIES_HEADERS_AND_TAGS)

    [request] = adapter.requests
    assert json.loads(request.body) == {
        "to": "user@example.com",
        "template_key": "password-reset",
        "subject": "Reset your password",
        "html": HTML,
        **COPIES_HEADERS_AND_TAGS_BODY,
    }


def test_a_server_that_does_not_take_a_field_refuses_the_send_and_names_it():
    refused = {
        "type": "invalid_request",
        "code": "invalid_request",
        "detail": "cc: PostHog doesn't take this field on this route.",
        "attr": "cc",
    }
    emails, adapter = emails_with([(400, refused, {}), accepted()])

    with pytest.raises(EmailError) as raised:
        send_code(emails, html=HTML, cc="manager@example.com")

    error = raised.value
    assert (error.name, error.code, error.field) == (
        "validation_error",
        "invalid_request",
        "cc",
    )
    assert len(adapter.requests) == 1


def test_a_local_template_sends_copies_headers_and_tags_but_never_uploads_them():
    emails, adapter = emails_with(
        [rejected(409, "template_outdated"), uploaded(), accepted()]
    )

    send_local_template(emails, **COPIES_HEADERS_AND_TAGS)

    first_send, upload, _ = adapter.requests
    sent = json.loads(first_send.body)
    assert {field: sent[field] for field in COPIES_HEADERS_AND_TAGS_BODY} == (
        COPIES_HEADERS_AND_TAGS_BODY
    )
    assert not set(COPIES_HEADERS_AND_TAGS_BODY) & set(json.loads(upload.body))


INVITE = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"


def test_attachments_are_sent_base64_encoded_once_across_retries():
    emails, adapter = emails_with([rejected(503, "delivery_unavailable"), accepted()])

    send_code(
        emails,
        html=HTML,
        attachments=[
            {
                "filename": "invite.ics",
                "content": INVITE,
                "content_type": "text/calendar; method=REQUEST",
            },
            {"filename": "logo.png", "content": "iVBORw0KGgo=", "content_id": "logo"},
        ],
    )

    first, retry = adapter.requests
    assert json.loads(first.body)["attachments"] == [
        {
            "filename": "invite.ics",
            "content": "QkVHSU46VkNBTEVOREFSDQpFTkQ6VkNBTEVOREFSDQo=",
            "content_type": "text/calendar; method=REQUEST",
        },
        {"filename": "logo.png", "content": "iVBORw0KGgo=", "content_id": "logo"},
    ]
    assert retry.body == first.body


@parameterized.expand(
    [
        ("bytearray", bytearray(INVITE)),
        ("memoryview", memoryview(INVITE)),
        (
            "strided_memoryview",
            memoryview(bytes(b for byte in INVITE for b in (byte, 0)))[::2],
        ),
    ]
)
def test_attachment_content_from_any_bytes_buffer_is_base64_encoded(_name, content):
    emails, adapter = emails_with([accepted()])

    send_code(
        emails, html=HTML, attachments=[{"filename": "invite.ics", "content": content}]
    )

    [request] = adapter.requests
    assert json.loads(request.body)["attachments"] == [
        {
            "filename": "invite.ics",
            "content": "QkVHSU46VkNBTEVOREFSDQpFTkQ6VkNBTEVOREFSDQo=",
        }
    ]


@parameterized.expand(
    [
        ("list_of_ints", {"filename": "a.txt", "content": [104, 105]}),
        ("no_filename", {"content": b"104"}),
        ("unknown_field", {"filename": "a.txt", "content": b"104", "path": "/tmp/a"}),
    ]
)
def test_rejects_an_attachment_that_is_not_a_file(_name, attachment):
    emails, adapter = emails_with([])

    with pytest.raises(EmailValidationError) as raised:
        send_code(emails, html=HTML, attachments=[attachment])

    assert_validation_error(raised.value, "invalid_form", "attachments")
    assert "104" not in str(raised.value)
    assert adapter.requests == []
