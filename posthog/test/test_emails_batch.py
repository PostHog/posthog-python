from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional
from unittest import mock
from urllib.parse import parse_qs, urlsplit

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
STATUS_BY_KEY_URL = "https://us.posthog.com/api/messaging/emails/status/"
FOUR_MIB = 4 * 1024 * 1024

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


def status_found(message_id: str) -> Answer:
    return (
        200,
        {
            "id": message_id,
            "status": "sent",
            "created_at": "2026-11-22T10:00:00Z",
            "status_at": "2026-11-22T10:00:01Z",
        },
        {},
    )


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


def test_splits_by_100_recipients_and_sends_the_chunks_in_order():
    emails, posthog = emails_on([all_queued, all_queued, all_queued])

    result = batch(emails, recipients(250))

    assert [len(body["recipients"]) for body in posthog.batch_bodies()] == [
        100,
        100,
        50,
    ]
    assert [r.index for r in result.results] == list(range(250))
    assert [r.message_id for r in result.results] == [
        f"msg_key-{i}" for i in range(250)
    ]
    assert all(r.status == "queued" for r in result.results)


def test_splits_by_the_4_mib_body_and_fails_only_a_recipient_too_large_alone():
    big = "x" * (FOUR_MIB // 3)
    people = recipients(5, template_data={"note": big})
    people[3]["template_data"] = {"note": "y" * FOUR_MIB}
    emails, posthog = emails_on([all_queued, all_queued])

    result = batch(emails, people)

    sizes = [len(request.body) for request in posthog.requests]
    assert all(size <= FOUR_MIB for size in sizes)
    sent = [
        [r["idempotency_key"] for r in body["recipients"]]
        for body in posthog.batch_bodies()
    ]
    assert sent == [["key-0", "key-1"], ["key-2", "key-4"]]
    assert [r.status for r in result.results] == [
        "queued",
        "queued",
        "queued",
        "failed",
        "queued",
    ]
    too_large = result.results[3]
    assert too_large.error is not None
    assert too_large.error.code == "content_too_large"
    assert "other recipients" not in too_large.error.message
    assert (too_large.message_id, too_large.idempotency_key) == (None, "key-3")


def test_a_chunk_fills_up_to_exactly_4_mib():
    base = len(
        json.dumps(
            {"template_id": TEMPLATE_ID, "message_data": {}, "recipients": []},
            separators=(",", ":"),
        ).encode()
    )
    one = {"to": "a@example.com", "idempotency_key": "k1", "template_data": {"n": ""}}
    item = len(
        json.dumps(
            {"to": "a@example.com", "idempotency_key": "k1", "message_data": {"n": ""}},
            separators=(",", ":"),
        ).encode()
    )
    first = dict(one, template_data={"n": "a" * (FOUR_MIB - base - item - 1 - item)})
    second = dict(one, idempotency_key="k2")
    third = dict(one, idempotency_key="k3")
    emails, posthog = emails_on([all_queued, all_queued])

    batch(emails, [first, second, third])

    assert [len(request.body) for request in posthog.requests][0] == FOUR_MIB
    assert [len(b["recipients"]) for b in posthog.batch_bodies()] == [2, 1]


def test_a_batch_waits_for_a_slow_answer_whatever_the_client_timeout():
    emails, _ = emails_on([Slow(40, all_queued)], timeout=3)

    [result] = batch(emails, recipients(1)).results

    assert result.status == "queued"


def test_a_longer_client_timeout_applies_to_a_batch_too():
    emails, _ = emails_on([Slow(50, all_queued)], timeout=60)

    [result] = batch(emails, recipients(1)).results

    assert result.status == "queued"


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
    assert second.error is not None and second.error.code == "delivery_unavailable"
    assert second.later_error is not None
    assert second.later_error.code == "idempotency_key_reused"


def test_a_503_then_a_refusal_also_keeps_the_slot_unconfirmed():
    emails, _ = emails_on(
        [
            whole_call(503, "delivery_unavailable"),
            ok(refused("m0", "sending_paused")),
        ]
    )

    [result] = batch(emails, recipients(1)).results

    assert result.status == "unconfirmed"
    assert (
        result.later_error is not None and result.later_error.code == "sending_paused"
    )


def test_a_refusal_without_an_earlier_lost_answer_is_failed():
    emails, _ = emails_on([ok(refused("m0", "sending_paused"))])

    [result] = batch(emails, recipients(1)).results

    assert (result.status, result.later_error) == ("failed", None)


def test_a_slot_the_pipeline_did_not_confirm_is_unconfirmed():
    emails, _ = emails_on(
        [ok(refused("m0", "delivery_unavailable", status=503, retryable=True))]
    )

    [result] = batch(emails, recipients(1)).results

    assert (result.status, result.message_id) == ("unconfirmed", "m0")
    assert result.error is not None and result.error.code == "delivery_unavailable"


def test_a_slot_another_request_still_holds_is_unconfirmed():
    emails, _ = emails_on(
        [ok(refused("m0", "idempotency_key_in_flight", status=409, retryable=True))]
    )

    [result] = batch(emails, recipients(1)).results

    assert (result.status, result.message_id) == ("unconfirmed", "m0")
    assert result.error is not None
    assert result.error.code == "idempotency_key_in_flight"


def test_a_shutdown_during_a_batch_stops_it_before_the_next_request():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)

    def shutdown_then_queue(request: requests.PreparedRequest) -> Answer:
        client.shutdown()
        return all_queued(request)

    posthog = BatchPostHog([shutdown_then_queue, all_queued])

    with on_the_wire(posthog), pytest.raises(EmailBatchError) as raised:
        batch(client.emails, recipients(150))

    assert raised.value.code == "client_closed"
    assert len(posthog.requests) == 1
    statuses = [r.status for r in raised.value.results]
    assert statuses == ["queued"] * 100 + ["not_attempted"] * 50
    assert raised.value.results[100].error is not None
    assert raised.value.results[100].error.code == "client_closed"


def test_a_shutdown_during_a_retry_wait_sends_no_retry():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)

    def shutdown_then_rate_limit(request: requests.PreparedRequest) -> Answer:
        client.shutdown()
        return whole_call(429)

    posthog = BatchPostHog([shutdown_then_rate_limit, all_queued])

    with on_the_wire(posthog), pytest.raises(EmailBatchError) as raised:
        batch(client.emails, recipients(150))

    assert raised.value.code == "client_closed"
    assert len(posthog.requests) == 1
    statuses = [r.status for r in raised.value.results]
    assert statuses == ["failed"] * 100 + ["not_attempted"] * 50
    assert raised.value.results[0].error is not None
    assert raised.value.results[0].error.code == "client_closed"


def test_a_shutdown_before_a_lost_answer_reads_no_email_by_its_key():
    client = Posthog("phc_test", secret_key=SECRET_KEY, sync_mode=True)

    def shutdown_then_unavailable(request: requests.PreparedRequest) -> Answer:
        client.shutdown()
        return whole_call(503, "delivery_unavailable")

    posthog = BatchPostHog([shutdown_then_unavailable, status_found("m0")])

    with on_the_wire(posthog), pytest.raises(EmailBatchError) as raised:
        batch(client.emails, recipients(2), max_retries=0)

    assert raised.value.code == "delivery_unavailable"
    assert len(posthog.requests) == 1
    assert [r.status for r in raised.value.results] == ["unconfirmed"] * 2


def test_a_whole_call_error_raises_with_the_results_so_far():
    emails, posthog = emails_on([all_queued, whole_call(429, "quota_limited")])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(250))

    error = raised.value
    assert (error.code, error.status_code) == ("quota_limited", 429)
    assert (
        len(posthog.requests) == 2
    )  # a quota refusal is not retried, and the batch stops
    statuses = [r.status for r in error.results]
    assert statuses == ["queued"] * 100 + ["failed"] * 100 + ["not_attempted"] * 50
    assert error.results[150].error is not None
    assert (error.results[150].error.code, error.results[150].error.message) == (
        "quota_limited",
        "Nope.",
    )
    assert error.results[200].error is not None
    assert error.results[200].error.code == "not_attempted"
    assert error.results[249].idempotency_key == "key-249"


def test_a_lost_answer_after_retries_reads_each_email_by_its_key():
    def status_by_key(request: requests.PreparedRequest) -> Answer:
        key = parse_qs(urlsplit(request.url).query)["idempotency_key"][0]
        if key == "key-0":
            return status_found("m0")
        return whole_call(404, "not_found")

    emails, posthog = emails_on(
        [
            requests.ConnectionError("reset"),
            requests.ConnectionError("reset"),
            requests.ConnectionError("reset"),
            status_by_key,
            status_by_key,
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    error = raised.value
    assert error.code == "network_error"
    reads = [r for r in posthog.requests if r.url.startswith(STATUS_BY_KEY_URL)]
    assert [parse_qs(urlsplit(r.url).query)["idempotency_key"] for r in reads] == [
        ["key-0"],
        ["key-1"],
    ]
    assert [(r.status, r.message_id) for r in error.results] == [
        ("queued", "m0"),
        ("unconfirmed", None),
    ]
    assert error.results[1].error is not None
    assert error.results[1].error.code == "delivery_unavailable"


def test_reading_by_key_stops_when_posthog_stays_unreachable():
    emails, posthog = emails_on(
        [whole_call(503, "delivery_unavailable")] * 3
        + [requests.ConnectionError("down")]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(3), max_retries=2)

    assert (
        len(posthog.requests) == 4
    )  # three batch attempts, one status read, then it stops
    assert [r.status for r in raised.value.results] == ["unconfirmed"] * 3


def test_a_key_with_reserved_characters_is_encoded_in_the_status_read():
    emails, posthog = emails_on(
        [requests.ConnectionError("reset"), whole_call(404, "not_found")]
    )

    with pytest.raises(EmailBatchError):
        batch(
            emails,
            [{"to": "a@example.com", "idempotency_key": "order#1&x=2"}],
            max_retries=0,
        )

    read = posthog.requests[1]
    assert parse_qs(urlsplit(read.url).query) == {"idempotency_key": ["order#1&x=2"]}


def test_batch_unavailable_is_not_retried_and_says_to_send_singly():
    emails, posthog = emails_on([whole_call(409, "batch_unavailable")])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2))

    assert len(posthog.requests) == 1
    assert raised.value.code == "batch_unavailable"
    assert raised.value.fix == (
        "Until batch sends are on for the project, send each email the batch didn't "
        "queue with send() and its recipient's idempotency_key."
    )
    assert [r.status for r in raised.value.results] == ["failed", "failed"]


@pytest.mark.parametrize(
    "body",
    [None, {"type": "invalid_request", "code": "not_found", "detail": "Not found."}],
)
def test_a_404_says_batch_sends_are_unavailable(body):
    emails, _ = emails_on([(404, body, {})])

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(1))

    assert raised.value.code == "not_found"
    assert raised.value.why is not None and "batch sends" in raised.value.why.lower()
    assert raised.value.fix == (
        "Check template_id, or send each email the batch didn't queue with send() "
        "and its recipient's idempotency_key."
    )


def test_an_answer_without_a_result_per_recipient_leaves_the_chunk_unconfirmed():
    emails, posthog = emails_on(
        [ok(queued("m0")), whole_call(404, "not_found"), whole_call(404, "not_found")]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(2), max_retries=0)

    assert raised.value.code == "api_error"
    assert [r.status for r in raised.value.results] == ["unconfirmed", "unconfirmed"]
    assert [r.url.startswith(STATUS_BY_KEY_URL) for r in posthog.requests] == [
        False,
        True,
        True,
    ]


def test_warnings_reach_the_caller_once(caplog):
    warning = {"code": "sandbox_sender", "detail": "Sent from the test sender."}

    def queued_with_warning(request: requests.PreparedRequest) -> Answer:
        status, body, headers = all_queued(request)
        return status, {**body, "warnings": [warning]}, headers

    emails, _ = emails_on([queued_with_warning, queued_with_warning])

    with caplog.at_level(logging.WARNING, logger="posthog"):
        result = batch(emails, recipients(150))

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


def test_warnings_reach_the_caller_when_a_later_request_fails(caplog):
    warning = {"code": "sandbox_sender", "detail": "Sent from the test sender."}

    def queued_with_warning(request: requests.PreparedRequest) -> Answer:
        status, body, headers = all_queued(request)
        return status, {**body, "warnings": [warning]}, headers

    emails, _ = emails_on([queued_with_warning, whole_call(429, "quota_limited")])

    with caplog.at_level(logging.WARNING, logger="posthog"):
        with pytest.raises(EmailBatchError) as raised:
            batch(emails, recipients(150))

    assert [w.code for w in raised.value.warnings] == ["sandbox_sender"]
    assert caplog.text.count("sandbox_sender") == 1


@pytest.mark.parametrize(
    "attr, field",
    [
        ("recipients__37__idempotency_key", "recipients__137__idempotency_key"),
        ("recipients__\u00b2__idempotency_key", "recipients__\u00b2__idempotency_key"),
        ("recipients__" + "9" * 5000 + "__idempotency_key", "recipients"),
    ],
)
def test_a_refused_recipient_field_names_its_position_in_recipients(attr, field):
    emails, _ = emails_on(
        [
            all_queued,
            (
                400,
                {
                    "type": "validation_error",
                    "code": "max_length",
                    "detail": "Ensure this field has no more than 255 characters.",
                    "attr": attr,
                },
                {},
            ),
        ]
    )

    with pytest.raises(EmailBatchError) as raised:
        batch(emails, recipients(150))

    assert raised.value.field == field
    assert [r.status for r in raised.value.results] == ["queued"] * 100 + [
        "failed"
    ] * 50


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
async def test_async_batch_sends_chunks_of_100_in_order():
    emails, seen = async_emails_on([async_all_queued, async_all_queued])

    result = await emails.batch(recipients=recipients(150), template_id=TEMPLATE_ID)

    assert [len(json.loads(r.content)["recipients"]) for r in seen] == [100, 50]
    assert [r.status for r in result.results] == ["queued"] * 150


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
    assert only.later_error is not None
    assert only.later_error.code == "sending_paused"


@pytest.mark.asyncio
async def test_async_batch_waits_for_a_slow_answer_whatever_the_client_timeout():
    emails, _ = async_emails_on([Slow(40, async_all_queued)], timeout=3)

    result = await emails.batch(recipients=recipients(1), template_id=TEMPLATE_ID)

    assert [r.status for r in result.results] == ["queued"]


@pytest.mark.asyncio
async def test_async_batch_reads_a_lost_email_by_its_key():
    emails, seen = async_emails_on(
        [httpx.ConnectError("reset"), httpx.Response(404, json={"code": "not_found"})]
    )

    with pytest.raises(EmailBatchError) as raised:
        await emails.batch(
            recipients=recipients(1), template_id=TEMPLATE_ID, max_retries=0
        )

    assert raised.value.code == "network_error"
    assert [r.status for r in raised.value.results] == ["unconfirmed"]
    assert str(seen[-1].url) == STATUS_BY_KEY_URL + "?idempotency_key=key-0"


@pytest.mark.asyncio
async def test_async_shutdown_during_a_batch_stops_it_before_the_next_request():
    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY)
    seen: list[httpx.Request] = []
    shutting_down: list[asyncio.Task[None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if not shutting_down:
            shutting_down.append(asyncio.create_task(client.shutdown()))
            for _ in range(5):
                await asyncio.sleep(0)
        return async_all_queued(request)

    async def wire(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        return await handler(request)

    with (
        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", wire),
        pytest.raises(EmailBatchError) as raised,
    ):
        await client.emails.batch(recipients=recipients(150), template_id=TEMPLATE_ID)
    await shutting_down[0]

    assert raised.value.code == "client_closed"
    assert len(seen) == 1
    statuses = [r.status for r in raised.value.results]
    assert statuses == ["queued"] * 100 + ["not_attempted"] * 50


@pytest.mark.asyncio
async def test_async_shutdown_during_a_retry_wait_sends_no_retry():
    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY)
    seen: list[httpx.Request] = []
    shutting_down: list[asyncio.Task[None]] = []
    yield_to_shutdown = asyncio.sleep  # the retry wait below is patched out

    async def wire(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        seen.append(request)
        if shutting_down:
            return async_all_queued(request)
        shutting_down.append(asyncio.create_task(client.shutdown()))
        for _ in range(5):
            await yield_to_shutdown(0)
        return httpx.Response(429)

    with (
        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", wire),
        mock.patch("posthog.emails.asyncio.sleep"),
        pytest.raises(EmailBatchError) as raised,
    ):
        await client.emails.batch(recipients=recipients(2), template_id=TEMPLATE_ID)
    await shutting_down[0]

    assert raised.value.code == "client_closed"
    assert len(seen) == 1
    assert [r.status for r in raised.value.results] == ["failed", "failed"]


@pytest.mark.asyncio
async def test_async_shutdown_before_a_lost_answer_reads_no_email_by_its_key():
    client = AsyncPosthog("phc_test", secret_key=SECRET_KEY)
    seen: list[httpx.Request] = []
    shutting_down: list[asyncio.Task[None]] = []

    async def wire(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        seen.append(request)
        if shutting_down:
            return httpx.Response(200, json=status_found("m0")[1])
        shutting_down.append(asyncio.create_task(client.shutdown()))
        for _ in range(5):
            await asyncio.sleep(0)
        return httpx.Response(503, json={"code": "delivery_unavailable"})

    with (
        mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", wire),
        pytest.raises(EmailBatchError) as raised,
    ):
        await client.emails.batch(
            recipients=recipients(2), template_id=TEMPLATE_ID, max_retries=0
        )
    await shutting_down[0]

    assert raised.value.code == "delivery_unavailable"
    assert len(seen) == 1
    assert [r.status for r in raised.value.results] == ["unconfirmed"] * 2
