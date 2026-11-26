from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from unittest import mock

import httpx
import pytest
import requests
from parameterized import parameterized

from posthog import AsyncPosthog, Posthog
from posthog.emails import (
    AsyncEmails,
    EmailBatchError,
    EmailError,
    Emails,
)

SECRET_KEY = "phs_test_secret"
TEMPLATE_ID = "0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b"
BATCH_URL = "https://us.posthog.com/api/messaging/send/email_batch/"

Answer = Any  # an Exception, or (status, body, headers)


class Slow:
    """A server that answers after ``seconds``. A shorter wait times out."""

    def __init__(self, seconds: float, answer: Answer) -> None:
        self.seconds = seconds
        self.answer = answer

    def within(self, waited: Optional[float], timed_out: Exception) -> Answer:
        if waited is not None and waited < self.seconds:
            return timed_out
        return self.answer


def read_timeout(timeout: Any) -> Optional[float]:
    return timeout[1] if isinstance(timeout, tuple) else timeout


class BatchPostHog(requests.adapters.BaseAdapter):
    """Answers each request with the next answer, or with a callable of the request."""

    def __init__(self, answers: list[Answer]) -> None:
        super().__init__()
        self.answers = list(answers)
        self.requests: list[requests.PreparedRequest] = []

    def send(
        self, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Slow):
            answer = answer.within(
                read_timeout(kwargs.get("timeout")), requests.ReadTimeout("timed out")
            )
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        status, body, headers = answer
        reply = requests.Response()
        reply.status_code = status
        if isinstance(body, bytes):
            reply._content = body
        else:
            reply._content = b"" if body is None else json.dumps(body).encode()
        reply.headers.update(headers)
        reply.request = request
        return reply

    def close(self) -> None:
        pass

    def batch_bodies(self) -> list[dict[str, Any]]:
        return [
            json.loads(request.body)
            for request in self.requests
            if request.url == BATCH_URL
        ]


def on_the_wire(posthog: BatchPostHog) -> Any:
    def send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: Any,
    ) -> requests.Response:
        return posthog.send(request, **kwargs)

    return mock.patch.object(requests.adapters.HTTPAdapter, "send", send)


def emails_on(answers: list[Answer], **options: Any) -> tuple[Emails, BatchPostHog]:
    adapter = BatchPostHog(answers)
    session = requests.Session()
    session.mount("https://", adapter)
    return Emails(SECRET_KEY, session=session, **options), adapter


def queued(message_id: str, replayed: bool = False) -> dict[str, Any]:
    return {"message_id": message_id, "status": "queued", "replayed": replayed}


def refused(
    message_id: str, code: str, status: int = 422, retryable: bool = False
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "error": {
            "code": code,
            "detail": f"Refused: {code}.",
            "status": status,
            "retryable": retryable,
        },
    }


def ok(*slots: dict[str, Any], **fields: Any) -> Answer:
    return (200, {"results": list(slots), **fields}, {})


def all_queued(request: requests.PreparedRequest) -> Answer:
    keys = [r["idempotency_key"] for r in json.loads(request.body)["recipients"]]
    return ok(*(queued(f"msg_{key}") for key in keys))


def whole_call(status: int, code: Optional[str] = None, **headers: str) -> Answer:
    body = (
        {"type": "invalid_request", "code": code, "detail": "Nope."} if code else None
    )
    return (status, body, headers)


def recipients(count: int, **extra: Any) -> list[dict[str, Any]]:
    return [
        {"to": f"user{i}@example.com", "idempotency_key": f"key-{i}", **extra}
        for i in range(count)
    ]


def batch(emails: Emails, people: list[dict[str, Any]], **overrides: Any):
    return emails.batch(
        **{"recipients": people, "template_id": TEMPLATE_ID, **overrides}
    )


@pytest.fixture(autouse=True)
def no_sleep():
    with (
        mock.patch("posthog.emails.time.sleep") as sleep,
        mock.patch("posthog.emails.random.uniform", return_value=1.0),
    ):
        yield sleep


def test_a_template_batch_posts_one_request_with_a_result_per_recipient():
    emails, posthog = emails_on(
        [
            ok(
                queued("m0"),
                queued("m1", replayed=True),
                refused("m2", "recipient_suppressed"),
            )
        ]
    )

    result = batch(
        emails,
        [
            {"to": "a@example.com", "idempotency_key": "welcome:a", "distinct_id": "a"},
            {"to": "b@example.com", "template_data": {"name": "Bea"}},
            {"to": "c@example.com", "idempotency_key": "welcome:c"},
        ],
        template_data={"name": "friend", "plan": "pro"},
    )

    [request] = posthog.requests
    assert (request.method, request.url) == ("POST", BATCH_URL)
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert "Idempotency-Key" not in request.headers
    body = json.loads(request.body)
    generated = body["recipients"][1]["idempotency_key"]
    assert body == {
        "template_id": TEMPLATE_ID,
        "message_data": {"name": "friend", "plan": "pro"},
        "recipients": [
            {"to": "a@example.com", "idempotency_key": "welcome:a", "distinct_id": "a"},
            {
                "to": "b@example.com",
                "idempotency_key": generated,
                "message_data": {"name": "Bea"},
            },
            {"to": "c@example.com", "idempotency_key": "welcome:c"},
        ],
    }
    assert generated not in ("welcome:a", "welcome:c")
    assert [r.index for r in result.results] == [0, 1, 2]
    assert [r.idempotency_key for r in result.results] == [
        "welcome:a",
        generated,
        "welcome:c",
    ]
    assert [(r.status, r.message_id, r.replayed) for r in result.results] == [
        ("queued", "m0", False),
        ("queued", "m1", True),
        ("failed", "m2", False),
    ]
    error = result.results[2].error
    assert isinstance(error, EmailError)
    assert (error.code, error.status_code, error.message_id) == (
        "recipient_suppressed",
        422,
        "m2",
    )
    assert result.results[2].to == "c@example.com"


def test_a_code_batch_sends_the_same_content_to_every_recipient():
    emails, posthog = emails_on([all_queued])

    result = emails.batch(
        recipients=recipients(2),
        template_key="launch-news",
        subject="We launched",
        html="<p>Hi</p>",
        from_="Acme <news@example.com>",
        tracking=False,
    )

    body = posthog.batch_bodies()[0]
    assert body == {
        "template_key": "launch-news",
        "subject": "We launched",
        "html": "<p>Hi</p>",
        "from": "Acme <news@example.com>",
        "tracking": False,
        "recipients": [
            {"to": "user0@example.com", "idempotency_key": "key-0"},
            {"to": "user1@example.com", "idempotency_key": "key-1"},
        ],
    }
    assert [r.status for r in result.results] == ["queued", "queued"]


@parameterized.expand(
    [
        ("scheduled_at", {"scheduled_at": "2026-12-01T09:00:00Z"}, TypeError),
        (
            "cc",
            {"template_key": "k", "subject": "s", "html": "h", "cc": "x@example.com"},
            TypeError,
        ),
        (
            "bcc",
            {"template_key": "k", "subject": "s", "html": "h", "bcc": "x@example.com"},
            TypeError,
        ),
        (
            "attachments",
            {"template_key": "k", "subject": "s", "html": "h", "attachments": []},
            TypeError,
        ),
        ("no_recipients", {"recipients": []}, ValueError),
        ("recipients_not_a_list", {"recipients": "a@example.com"}, TypeError),
        ("recipient_not_a_mapping", {"recipients": ["a@example.com"]}, TypeError),
        ("recipient_without_to", {"recipients": [{"distinct_id": "a"}]}, TypeError),
        ("recipient_blank_to", {"recipients": [{"to": " "}]}, ValueError),
        (
            "recipient_unknown_field",
            {"recipients": [{"to": "a@example.com", "cc": "b@example.com"}]},
            TypeError,
        ),
        (
            "recipient_bad_key",
            {"recipients": [{"to": "a@example.com", "idempotency_key": "has space"}]},
            ValueError,
        ),
        (
            "recipient_template_data_not_a_mapping",
            {"recipients": [{"to": "a@example.com", "template_data": [1]}]},
            TypeError,
        ),
        (
            "code_form_recipient_template_data",
            {
                "template_key": "k",
                "subject": "s",
                "html": "h",
                "recipients": [{"to": "a@example.com", "template_data": {"a": 1}}],
            },
            TypeError,
        ),
        (
            "both_forms",
            {
                "template_key": "k",
                "subject": "s",
                "html": "h",
                "template_id": TEMPLATE_ID,
            },
            TypeError,
        ),
    ]
)
def test_refuses_what_a_batch_does_not_take_before_any_request(_name, overrides, error):
    emails, posthog = emails_on([])
    args = {"recipients": recipients(1), "template_id": TEMPLATE_ID, **overrides}
    if "template_key" in overrides and "template_id" not in overrides:
        del args["template_id"]

    with pytest.raises(error):
        emails.batch(**args)

    assert posthog.requests == []


def test_names_both_positions_of_a_repeated_idempotency_key():
    emails, posthog = emails_on([])
    people = recipients(3)
    people[2]["idempotency_key"] = "key-0"

    with pytest.raises(ValueError, match=r"recipients\[2\].*recipients\[0\]"):
        batch(emails, people)

    assert posthog.requests == []


def test_names_the_position_of_a_bad_recipient():
    emails, _ = emails_on([])
    people = recipients(3)
    people[1]["to"] = ""

    with pytest.raises(ValueError, match=r"recipients\[1\]"):
        batch(emails, people)


def test_more_than_100_recipients_is_refused_before_any_request():
    emails, posthog = emails_on([])

    with pytest.raises(ValueError, match=r"at most 100 recipients.*101"):
        batch(emails, recipients(101))

    assert posthog.requests == []


def test_100_recipients_go_out_in_one_request():
    emails, posthog = emails_on([all_queued])

    result = batch(emails, recipients(100))

    assert [len(body["recipients"]) for body in posthog.batch_bodies()] == [100]
    assert [r.message_id for r in result.results] == [
        f"msg_key-{i}" for i in range(100)
    ]


def test_a_batch_the_server_finds_too_large_raises_its_413_with_its_fix():
    emails, posthog = emails_on(
        [
            (
                413,
                {
                    "type": "invalid_request",
                    "code": "content_too_large",
                    "detail": "Too big.",
                    "fix": "Split the recipients into smaller batches.",
                },
                {},
            )
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    error = raised.value
    assert (error.code, error.status_code, error.message, error.fix) == (
        "content_too_large",
        413,
        "Too big.",
        "Split the recipients into smaller batches.",
    )
    assert [r.status for r in error.results] == ["failed", "failed"]
    assert len(posthog.requests) == 1


def test_a_400_content_too_large_keeps_its_own_fix_and_no_split_advice():
    emails, _ = emails_on(
        [
            (
                400,
                {
                    "code": "content_too_large",
                    "detail": "The shared data is too large.",
                    "fix": "Send less shared template_data.",
                },
                {},
            ),
            (400, {"code": "content_too_large", "detail": "Too large."}, {}),
        ]
    )

    with pytest.raises(EmailBatchError) as with_fix:
        batch(emails, recipients(2))
    with pytest.raises(EmailBatchError) as without_fix:
        batch(emails, recipients(2))

    assert (with_fix.value.status_code, with_fix.value.fix) == (
        400,
        "Send less shared template_data.",
    )
    assert "smaller batches" not in (without_fix.value.fix or "")


def test_a_body_over_4_mib_goes_out_whole_in_one_request():
    big = "x" * (5 * 1024 * 1024)
    emails, posthog = emails_on(
        [
            (
                413,
                {
                    "code": "content_too_large",
                    "detail": "Too big.",
                    "fix": "Split the recipients into smaller batches.",
                },
                {},
            )
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2), template_data={"note": big})

    [body] = posthog.batch_bodies()
    assert body["message_data"] == {"note": big}
    assert len(body["recipients"]) == 2
    assert (raised.value.status_code, raised.value.code) == (413, "content_too_large")


@pytest.mark.parametrize(
    "count, says",
    [(2, "smaller batches"), (1, "template_data")],
    ids=["many", "one"],
)
@pytest.mark.parametrize(
    "fix", [None, "", "   ", ["split"]], ids=["absent", "empty", "blank", "list"]
)
def test_a_413_without_a_usable_fix_says_to_split_or_send_less(count, says, fix):
    body: dict[str, Any] = {"code": "content_too_large", "detail": "Big."}
    if fix is not None:
        body["fix"] = fix
    emails, _ = emails_on([(413, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(count))

    assert raised.value.fix is not None and says in raised.value.fix


def test_a_batch_waits_for_a_slow_answer_whatever_the_client_timeout():
    emails, _ = emails_on([Slow(40, all_queued)], timeout=3)

    [result] = batch(emails, recipients(1)).results

    assert result.status == "queued"


@pytest.mark.parametrize(
    "client_timeout,answer_after", [(3, 46), (60, 61)], ids=["45s", "60s"]
)
def test_a_batch_answer_past_its_timeout_is_unconfirmed(client_timeout, answer_after):
    emails, posthog = emails_on(
        [Slow(answer_after, all_queued)], timeout=client_timeout
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(1), max_retries=0)

    assert raised.value.code == "network_error"
    assert [r.status for r in raised.value.results] == ["unconfirmed"]
    assert len(posthog.requests) == 1


def test_a_longer_client_timeout_applies_to_a_batch_too():
    emails, _ = emails_on([Slow(50, all_queued)], timeout=60)

    [result] = batch(emails, recipients(1)).results

    assert result.status == "queued"


def test_a_retry_after_a_lost_answer_resends_the_key_the_sdk_generated():
    emails, posthog = emails_on(
        [requests.ConnectionError("reset"), ok(queued("m0"), queued("m1"))]
    )
    people = [
        {"to": "user0@example.com"},
        {"to": "user1@example.com", "idempotency_key": "key-1"},
    ]

    result = batch(emails, people)

    first, second = posthog.batch_bodies()
    generated = first["recipients"][0]["idempotency_key"]
    assert generated and second["recipients"][0]["idempotency_key"] == generated
    assert [r.idempotency_key for r in result.results] == [generated, "key-1"]
    assert [r.status for r in result.results] == ["queued", "queued"]


def test_a_retry_after_a_lost_answer_keeps_a_refused_slot_unconfirmed():
    emails, posthog = emails_on(
        [
            requests.ConnectionError("reset"),
            ok(queued("m0", replayed=True), refused("m1", "idempotency_key_reused")),
        ]
    )

    result = batch(emails, recipients(2))

    bodies = [request.body for request in posthog.requests]
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    first, second = result.results
    assert (first.status, first.message_id, first.replayed) == ("queued", "m0", True)
    assert (second.status, second.message_id) == ("unconfirmed", "m1")
    assert second.error is not None
    assert second.error.code == "idempotency_key_reused"
    assert second.error.fix is not None and "same idempotency_key" in second.error.fix


def test_a_503_then_a_refusal_also_keeps_the_slot_unconfirmed():
    emails, _ = emails_on(
        [
            whole_call(503, "delivery_unavailable"),
            ok(refused("m0", "sending_paused")),
        ]
    )

    [result] = batch(emails, recipients(1)).results

    assert result.status == "unconfirmed"
    assert result.error is not None and result.error.code == "sending_paused"


def test_a_refusal_without_an_earlier_lost_answer_is_failed():
    emails, _ = emails_on([ok(refused("m0", "sending_paused"))])

    [result] = batch(emails, recipients(1)).results

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "sending_paused"


def test_a_slot_the_pipeline_did_not_confirm_is_unconfirmed():
    emails, _ = emails_on(
        [ok(refused("m0", "delivery_unavailable", status=503, retryable=True))]
    )

    [result] = batch(emails, recipients(1)).results

    assert (result.status, result.message_id) == ("unconfirmed", "m0")
    assert result.error is not None and result.error.code == "delivery_unavailable"
    assert result.error.fix is not None
    assert "same idempotency_key" in result.error.fix


def test_a_slot_another_request_still_holds_is_unconfirmed():
    emails, _ = emails_on(
        [ok(refused("m0", "idempotency_key_in_flight", status=409, retryable=True))]
    )

    [result] = batch(emails, recipients(1)).results

    assert (result.status, result.message_id) == ("unconfirmed", "m0")
    assert result.error is not None
    assert result.error.code == "idempotency_key_in_flight"
    assert result.error.fix is not None
    assert "same idempotency_key" in result.error.fix


def test_a_shutdown_during_a_retry_wait_sends_no_retry():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)
    posthog = BatchPostHog([whole_call(429), all_queued])

    def shutdown_during_the_wait(delay: float) -> None:
        client.shutdown()

    with (
        on_the_wire(posthog),
        mock.patch("posthog.emails.time.sleep", side_effect=shutdown_during_the_wait),
        pytest.raises(EmailBatchError) as raised,
    ):
        batch(client.emails, recipients(2))

    assert raised.value.code == "client_closed"
    assert len(posthog.requests) == 1
    statuses = [r.status for r in raised.value.results]
    assert statuses == ["failed", "failed"]
    assert raised.value.results[0].error is not None
    assert raised.value.results[0].error.code == "client_closed"


def test_a_shutdown_during_a_retry_wait_after_a_503_leaves_recipients_unconfirmed():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)
    posthog = BatchPostHog([whole_call(503, "delivery_unavailable"), all_queued])

    def shutdown_during_the_wait(delay: float) -> None:
        client.shutdown()

    with (
        on_the_wire(posthog),
        mock.patch("posthog.emails.time.sleep", side_effect=shutdown_during_the_wait),
        pytest.raises(EmailBatchError) as raised,
    ):
        batch(client.emails, recipients(2))

    assert raised.value.code == "client_closed"
    assert len(posthog.requests) == 1
    results = raised.value.results
    assert [(r.status, r.idempotency_key) for r in results] == [
        ("unconfirmed", "key-0"),
        ("unconfirmed", "key-1"),
    ]
    error = results[0].error
    assert error is not None and error.fix is not None
    assert "same idempotency_key" in error.fix


def test_a_whole_call_error_raises_with_every_recipient_failed():
    emails, posthog = emails_on([whole_call(429, "quota_limited")])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(3))

    error = raised.value
    assert (error.code, error.status_code) == ("quota_limited", 429)
    assert len(posthog.requests) == 1  # a quota refusal is not retried
    assert [r.status for r in error.results] == ["failed"] * 3
    assert error.results[1].error is not None
    assert (error.results[1].error.code, error.results[1].error.message) == (
        "quota_limited",
        "Nope.",
    )
    assert error.results[2].idempotency_key == "key-2"


def test_a_429_after_a_lost_answer_leaves_every_recipient_unconfirmed():
    emails, posthog = emails_on(
        [requests.ConnectionError("reset"), whole_call(429, "rate_limited")]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2), max_retries=1)

    assert raised.value.code == "rate_limited"
    assert [r.status for r in raised.value.results] == ["unconfirmed"] * 2
    error = raised.value.results[1].error
    assert error is not None and error.code == "rate_limited"
    assert error.fix is not None and "same idempotency_key" in error.fix
    assert len(posthog.requests) == 2


def test_a_whole_call_refusal_after_a_503_leaves_every_recipient_unconfirmed():
    emails, posthog = emails_on(
        [
            whole_call(503, "delivery_unavailable"),
            (
                429,
                {"code": "quota_limited", "detail": "Nope.", "attr": "recipients"},
                {"Retry-After": "7"},
            ),
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2), max_retries=1)

    assert raised.value.code == "quota_limited"
    assert [r.status for r in raised.value.results] == ["unconfirmed"] * 2
    error = raised.value.results[0].error
    assert error is not None and error.code == "quota_limited"
    assert error.fix is not None and "same idempotency_key" in error.fix
    assert (error.retry_after, error.field) == (7.0, "recipients")
    assert len(posthog.requests) == 2


def test_a_lost_answer_after_retries_leaves_each_email_unconfirmed_without_a_read():
    emails, posthog = emails_on([requests.ConnectionError("reset")] * 3)

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    error = raised.value
    assert error.code == "network_error"
    assert [r.url for r in posthog.requests] == [BATCH_URL] * 3
    assert [(r.status, r.message_id) for r in error.results] == [
        ("unconfirmed", None),
        ("unconfirmed", None),
    ]
    assert [r.idempotency_key for r in error.results] == ["key-0", "key-1"]
    lost = error.results[1].error
    assert lost is not None and lost.code == "network_error"
    assert lost.fix is not None and "same idempotency_key" in lost.fix


def test_a_batch_that_succeeds_reads_no_unconfirmed_email_back():
    unconfirmed_slot = {
        "error": {
            "code": "delivery_unavailable",
            "detail": "Not confirmed.",
            "status": 503,
            "retryable": True,
        }
    }
    emails, posthog = emails_on([ok(queued("m0"), unconfirmed_slot)])

    result = batch(emails, recipients(2), max_retries=0)

    assert [(r.status, r.message_id) for r in result.results] == [
        ("queued", "m0"),
        ("unconfirmed", None),
    ]
    assert [r.url for r in posthog.requests] == [BATCH_URL]


def test_an_unconfirmed_recipient_holds_the_last_answer():
    emails, posthog = emails_on(
        [
            (503, {"code": "service_unavailable", "detail": "First."}, {}),
            whole_call(409, "idempotency_key_in_flight"),
            (503, {"code": "delivery_unavailable", "detail": "Last."}, {}),
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(1), max_retries=2)

    [result] = raised.value.results
    assert result.status == "unconfirmed"
    assert result.error is not None
    assert (result.error.code, result.error.message) == (
        "delivery_unavailable",
        UNCONFIRMED,
    )
    assert result.error.fix is not None and "same idempotency_key" in result.error.fix
    assert len(posthog.requests) == 3


def test_a_whole_call_in_flight_answer_leaves_the_batch_unconfirmed():
    emails, posthog = emails_on([whole_call(409, "idempotency_key_in_flight")])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2), max_retries=0)

    assert raised.value.code == "idempotency_key_in_flight"
    assert [(r.status, r.message_id) for r in raised.value.results] == [
        ("unconfirmed", None),
        ("unconfirmed", None),
    ]
    first = raised.value.results[0].error
    assert first is not None and first.code == "idempotency_key_in_flight"
    assert first.fix is not None and "same idempotency_key" in first.fix
    assert "of the first request that used it" in first.fix
    assert len(posthog.requests) == 1


def test_a_refusal_after_an_in_flight_answer_keeps_the_batch_unconfirmed():
    emails, posthog = emails_on(
        [
            whole_call(409, "idempotency_key_in_flight"),
            whole_call(429, "quota_limited"),
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(1), max_retries=1)

    assert raised.value.code == "quota_limited"
    [result] = raised.value.results
    assert result.status == "unconfirmed"
    assert result.error is not None and result.error.code == "quota_limited"
    assert result.error.fix is not None and "same idempotency_key" in result.error.fix
    assert len(posthog.requests) == 2


@pytest.mark.parametrize(
    "body",
    [None, b"<html>Conflict</html>", {"code": []}, {"code": "   "}],
    ids=["lost", "not_json", "code_not_text", "code_blank"],
)
def test_a_409_without_a_readable_code_leaves_the_batch_unconfirmed(body):
    emails, posthog = emails_on([(409, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    results = raised.value.results
    assert [(r.status, r.idempotency_key) for r in results] == [
        ("unconfirmed", "key-0"),
        ("unconfirmed", "key-1"),
    ]
    error = results[0].error
    assert error is not None and error.fix is not None
    assert "same idempotency_key" in error.fix
    assert len(posthog.requests) == 1


def test_an_in_flight_code_on_a_status_other_than_409_is_failed():
    emails, posthog = emails_on([whole_call(401, "idempotency_key_in_flight")])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    assert [r.status for r in raised.value.results] == ["failed", "failed"]
    assert len(posthog.requests) == 1


def test_an_in_flight_retry_after_a_503_is_the_error():
    emails, posthog = emails_on(
        [
            whole_call(503, "delivery_unavailable"),
            whole_call(409, "idempotency_key_in_flight"),
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(1), max_retries=1)

    [result] = raised.value.results
    assert result.status == "unconfirmed"
    assert result.error is not None
    assert result.error.code == "idempotency_key_in_flight"
    assert len(posthog.requests) == 2


@pytest.mark.parametrize("code", [[], {}], ids=["list", "object"])
def test_a_whole_call_answer_with_a_code_that_is_not_text_is_a_batch_error(code):
    emails, posthog = emails_on([(400, {"code": code, "detail": "Nope."}, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    assert raised.value.code == "api_error"
    assert [r.status for r in raised.value.results] == ["failed", "failed"]
    assert len(posthog.requests) == 1


def test_batch_unavailable_is_not_retried_and_says_to_send_singly():
    emails, posthog = emails_on([whole_call(409, "batch_unavailable")])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    assert len(posthog.requests) == 1
    assert raised.value.code == "batch_unavailable"
    assert raised.value.name == "validation_error"
    assert raised.value.fix == (
        "Until batch sends are on for the project, send each email the batch didn't "
        "queue with send() and its recipient's idempotency_key."
    )
    assert [r.status for r in raised.value.results] == ["failed", "failed"]


FIELDLESS_404_BODIES = [
    pytest.param(None, id="no_body"),
    pytest.param(
        {
            "type": "invalid_request",
            "code": "not_found",
            "attr": None,
            "detail": "Not found.",
        },
        id="unknown_template_or_batch_off",
    ),
    pytest.param(
        {
            "type": "invalid_request",
            "code": "not_found",
            "detail": "Endpoint not found.",
        },
        id="route_missing",
    ),
    pytest.param({"code": "", "detail": "Not found."}, id="blank_code"),
    pytest.param({"code": "api_error"}, id="api_error"),
]


def assert_no_update_advice(error: EmailError) -> None:
    said = " ".join(filter(None, [error.message, error.why, error.fix])).lower()
    assert "version" not in said
    assert "update" not in said
    assert "server" not in said


@pytest.mark.parametrize("body", FIELDLESS_404_BODIES)
def test_a_404_without_a_field_says_the_template_id_is_wrong_or_batches_are_off(body):
    emails, posthog = emails_on([(404, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    error = raised.value
    assert len(posthog.requests) == 1
    assert (error.code, error.status_code, error.field) == ("not_found", 404, None)
    assert error.message == (
        "PostHog found no template with this template_id, or batch sends aren't "
        "available for this project, so PostHog sent no email of this request."
    )
    assert error.fix == (
        "Check template_id. If it's right, send each recipient on its own with send() "
        "and its idempotency_key until batch sends are available."
    )
    assert_no_update_advice(error)
    assert [(r.status, r.error and r.error.message) for r in error.results] == [
        ("failed", error.message),
        ("failed", error.message),
    ]


@pytest.mark.parametrize("body", FIELDLESS_404_BODIES)
def test_a_code_batch_404_without_a_field_says_batches_from_code_are_off(body):
    emails, _ = emails_on([(404, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        emails.batch(
            template_key="digest",
            subject="Hi",
            html="<p>Hi</p>",
            recipients=recipients(1),
        )

    error = raised.value
    assert (error.code, error.status_code, error.field) == ("not_found", 404, None)
    assert error.message == (
        "Batch sends or sending from code aren't available for this project, so "
        "PostHog sent no email of this request."
    )
    assert error.fix == (
        "If the project can send email from code, send each recipient on its own "
        "with send() and its idempotency_key until batch sends are available."
    )
    assert "template_id" not in " ".join(
        filter(None, [error.message, error.why, error.fix])
    )
    assert_no_update_advice(error)
    assert [r.status for r in raised.value.results] == ["failed"]


SEND_AGAIN = (
    "Send it again with the same idempotency_key within 24 hours of the first "
    "request that used it, so it goes out once."
)


@pytest.mark.parametrize(
    "form",
    [
        {"template_id": TEMPLATE_ID},
        {"template_key": "digest", "subject": "Hi", "html": "<p>Hi</p>"},
    ],
    ids=["template", "code"],
)
@pytest.mark.parametrize(
    "lost", [requests.ConnectionError("reset"), (503, None, {})], ids=["dropped", "503"]
)
def test_a_404_after_a_lost_answer_leaves_every_recipient_unconfirmed(form, lost):
    body = {
        "type": "invalid_request",
        "code": "not_found",
        "attr": None,
        "detail": "Not found.",
    }
    emails, posthog = emails_on([lost, (404, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        emails.batch(recipients=recipients(2), **form)

    error = raised.value
    assert len(posthog.requests) == 2
    assert (error.code, error.status_code) == ("not_found", 404)
    assert "sent no email" not in error.message
    assert "may still go out" in error.message
    assert [(r.status, r.idempotency_key) for r in error.results] == [
        ("unconfirmed", "key-0"),
        ("unconfirmed", "key-1"),
    ]
    for result in error.results:
        assert result.error is not None
        assert result.error.fix == SEND_AGAIN
        assert "sent no email" not in result.error.message


UNCONFIRMED = "PostHog didn't confirm this email, so it may still go out."
BATCH_UNCONFIRMED = (
    "PostHog didn't confirm this batch, so some of its emails may still go out."
)
SEND_EACH_AGAIN = (
    "Send each recipient again with the same idempotency_key within 24 hours of the "
    "first request that used it, so each email goes out once, and read each "
    "recipient's result."
)


@pytest.mark.parametrize(
    "body",
    [
        {"code": "conflict", "detail": "Conflict."},
        {"code": "api_error", "detail": "Something went wrong."},
        {
            "code": "idempotency_key_reused",
            "detail": "This key was used for another email.",
        },
    ],
    ids=["conflict", "api_error", "key_reused"],
)
def test_a_409_with_a_code_other_than_batch_unavailable_leaves_the_batch_unconfirmed(
    body,
):
    emails, _ = emails_on([(409, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    error = raised.value
    assert (error.status_code, error.code) == (409, body["code"])
    assert error.fix == SEND_EACH_AGAIN
    assert [(r.status, r.idempotency_key) for r in error.results] == [
        ("unconfirmed", "key-0"),
        ("unconfirmed", "key-1"),
    ]
    assert all(r.error is not None and r.error.fix == SEND_AGAIN for r in error.results)


@pytest.mark.parametrize(
    "status, body",
    [
        (
            400,
            {
                "code": "content_too_large",
                "detail": "The template_data is too large, so PostHog didn't send it.",
            },
        ),
        (404, {"code": "not_found", "detail": "Not found."}),
        (
            409,
            {
                "code": "batch_unavailable",
                "detail": "Batch sends aren't on, so nothing was sent.",
            },
        ),
        (
            413,
            {
                "code": "content_too_large",
                "detail": "The batch is larger than PostHog can send at once, so PostHog didn't send it.",
                "fix": "Split the recipients into smaller batches.",
            },
        ),
    ],
    ids=["400", "404", "409_batch_unavailable", "413"],
)
def test_a_refused_retry_after_a_lost_answer_never_says_nothing_was_sent(status, body):
    emails, posthog = emails_on([requests.ConnectionError("reset"), (status, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    error = raised.value
    assert len(posthog.requests) == 2
    assert (error.status_code, error.code) == (status, body["code"])
    assert error.message == BATCH_UNCONFIRMED
    assert error.fix == SEND_EACH_AGAIN
    said = " ".join(filter(None, [error.message, error.why, error.fix])).lower()
    assert "split" not in said and "didn't send" not in said and "nothing" not in said
    assert [(r.status, r.idempotency_key) for r in error.results] == [
        ("unconfirmed", "key-0"),
        ("unconfirmed", "key-1"),
    ]


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param([requests.ConnectionError("reset")] * 4, id="network"),
        pytest.param(
            [(503, {"code": "delivery_unavailable", "detail": "Delivery failed."}, {})]
            * 4,
            id="503",
        ),
        pytest.param(
            [ok(refused("m0", "idempotency_key_in_flight", status=409), queued("m1"))],
            id="in_flight_slot",
        ),
        pytest.param(
            [ok({"message_id": "m0", "error": {"code": "api_error"}}, queued("m1"))],
            id="api_error_slot_without_detail",
        ),
        pytest.param(
            [
                requests.ConnectionError("reset"),
                ok(refused("m0", "invalid_recipient"), queued("m1")),
            ],
            id="refused_slot_after_a_lost_answer",
        ),
    ],
)
def test_an_unconfirmed_recipient_is_never_told_its_email_was_not_sent(answers):
    emails, _ = emails_on(answers)

    try:
        results = batch(emails, recipients(2)).results
    except EmailBatchError as raised:
        results = raised.results

    unconfirmed = [r for r in results if r.status == "unconfirmed"]
    assert unconfirmed
    for result in unconfirmed:
        assert result.error is not None
        assert result.error.message == UNCONFIRMED
        said = " ".join(filter(None, [result.error.message, result.error.why])).lower()
        for words in (
            "not queued",
            "didn't queue",
            "not sent",
            "didn't send",
            "failed",
        ):
            assert words not in said


def test_a_404_that_names_a_field_keeps_what_posthog_said():
    body = {"code": "not_found", "attr": "template_id", "detail": "No such template."}
    emails, _ = emails_on([(404, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(1))

    error = raised.value
    assert (error.code, error.field, error.message) == (
        "not_found",
        "template_id",
        "No such template.",
    )
    assert_no_update_advice(error)


def test_an_answer_without_a_result_per_recipient_leaves_the_batch_unconfirmed():
    emails, posthog = emails_on([ok(queued("m0"))])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2), max_retries=0)

    assert raised.value.code == "api_error"
    assert [r.status for r in raised.value.results] == ["unconfirmed", "unconfirmed"]
    assert [r.url for r in posthog.requests] == [BATCH_URL]


def test_warnings_reach_the_caller_once(caplog):
    warning = {"code": "sandbox_sender", "detail": "Sent from the test sender."}

    def queued_with_warning(request: requests.PreparedRequest) -> Answer:
        status, body, headers = all_queued(request)
        return status, {**body, "warnings": [warning, warning]}, headers

    emails, _ = emails_on([queued_with_warning])

    with caplog.at_level(logging.WARNING, logger="posthog"):
        result = batch(emails, recipients(2))

    assert [w.code for w in result.warnings] == ["sandbox_sender"]
    assert caplog.text.count("sandbox_sender") == 1


def test_a_disabled_client_sends_nothing():
    emails, posthog = emails_on([], disabled=True)

    with pytest.raises(EmailError) as disabled:
        batch(emails, recipients(1))

    assert disabled.value.code == "client_disabled"
    assert posthog.requests == []


def test_a_client_off_cloud_without_an_app_host_sends_nothing():
    client = Posthog(
        "phc_test", host="https://ph.example.com", secret_key=SECRET_KEY, sync_mode=True
    )
    posthog = BatchPostHog([])

    with on_the_wire(posthog), pytest.raises(EmailError) as no_host:
        batch(client.emails, recipients(1))

    assert no_host.value.code == "missing_app_host"
    assert posthog.requests == []


def test_a_refused_recipient_field_names_its_position_in_recipients():
    emails, _ = emails_on(
        [
            (
                400,
                {
                    "type": "validation_error",
                    "code": "invalid",
                    "detail": "Enter a valid email address.",
                    "attr": "recipients__1__to",
                },
                {},
            ),
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    assert raised.value.field == "recipients__1__to"
    assert [r.status for r in raised.value.results] == ["failed", "failed"]


def test_a_code_batch_without_content_names_batch():
    emails, posthog = emails_on([])

    with pytest.raises(TypeError, match=r"batch\(\) needs html, text or both"):
        emails.batch(template_key="digest", subject="Hi", recipients=recipients(1))

    assert posthog.requests == []


def async_emails_on(
    answers: list[Any], **options: Any
) -> tuple[AsyncEmails, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Slow):
            answer = answer.within(
                request.extensions["timeout"]["read"], httpx.ReadTimeout("timed out")
            )
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncEmails(SECRET_KEY, http_client=client, **options), seen


def async_all_queued(request: httpx.Request) -> httpx.Response:
    keys = [r["idempotency_key"] for r in json.loads(request.content)["recipients"]]
    return httpx.Response(200, json={"results": [queued(f"m-{k}") for k in keys]})


@pytest.mark.asyncio
async def test_async_batch_sends_100_recipients_in_one_request():
    emails, seen = async_emails_on([async_all_queued])

    result = await emails.batch(recipients=recipients(100), template_id=TEMPLATE_ID)

    assert [len(json.loads(r.content)["recipients"]) for r in seen] == [100]
    assert [r.status for r in result.results] == ["queued"] * 100


@pytest.mark.asyncio
async def test_async_batch_refuses_more_than_100_recipients_before_any_request():
    emails, seen = async_emails_on([])

    with pytest.raises(ValueError, match="at most 100 recipients"):
        await emails.batch(recipients=recipients(101), template_id=TEMPLATE_ID)

    assert seen == []


@pytest.mark.asyncio
async def test_async_batch_keeps_a_recipient_unconfirmed_after_a_lost_answer():
    emails, _ = async_emails_on(
        [
            httpx.ConnectError("reset"),
            httpx.Response(200, json={"results": [refused("m-0", "sending_paused")]}),
        ]
    )

    with mock.patch("posthog.emails.asyncio.sleep"):
        result = await emails.batch(recipients=recipients(1), template_id=TEMPLATE_ID)

    [only] = result.results
    assert only.status == "unconfirmed"
    assert only.error is not None and only.error.code == "sending_paused"


@pytest.mark.asyncio
async def test_async_batch_waits_for_a_slow_answer_whatever_the_client_timeout():
    emails, _ = async_emails_on([Slow(40, async_all_queued)], timeout=3)

    result = await emails.batch(recipients=recipients(1), template_id=TEMPLATE_ID)

    assert [r.status for r in result.results] == ["queued"]


@pytest.mark.asyncio
async def test_async_batch_reads_no_lost_email_back():
    emails, seen = async_emails_on([httpx.ConnectError("reset")])

    with pytest.raises(EmailBatchError) as raised:
        await emails.batch(
            recipients=recipients(1), template_id=TEMPLATE_ID, max_retries=0
        )

    assert raised.value.code == "network_error"
    assert [(r.status, r.message_id) for r in raised.value.results] == [
        ("unconfirmed", None)
    ]
    assert [str(r.url) for r in seen] == [BATCH_URL]


@pytest.mark.asyncio
async def test_async_batch_that_succeeds_reads_no_unconfirmed_email_back():
    unconfirmed_slot = {
        "error": {
            "code": "delivery_unavailable",
            "detail": "Not confirmed.",
            "status": 503,
            "retryable": True,
        }
    }
    emails, seen = async_emails_on(
        [httpx.Response(200, json={"results": [unconfirmed_slot]})]
    )

    result = await emails.batch(
        recipients=recipients(1), template_id=TEMPLATE_ID, max_retries=0
    )

    assert [(r.status, r.message_id) for r in result.results] == [("unconfirmed", None)]
    assert [str(r.url) for r in seen] == [BATCH_URL]


@pytest.mark.asyncio
async def test_async_shutdown_during_a_retry_wait_sends_no_retry():
    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY)
    seen: list[httpx.Request] = []
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def wire(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        seen.append(request)
        if waits:
            return async_all_queued(request)
        return httpx.Response(429)

    shutting_down: list[asyncio.Task[None]] = []

    async def shutdown_during_the_wait(delay: float) -> None:
        if waits:
            await real_sleep(0)
            return
        waits.append(delay)
        shutting_down.append(asyncio.create_task(client.shutdown()))
        for _ in range(5):
            await real_sleep(0)

    with (
        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", wire),
        mock.patch("posthog.emails.asyncio.sleep", new=shutdown_during_the_wait),
        pytest.raises(EmailBatchError) as raised,
    ):
        await client.emails.batch(recipients=recipients(2), template_id=TEMPLATE_ID)
    await shutting_down[0]

    assert raised.value.code == "client_closed"
    assert len(seen) == 1
    assert [r.status for r in raised.value.results] == ["failed", "failed"]
