import gzip
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from posthog.tracing._transport import (
    OTLP_MAX_BODY_BYTES,
    SendOutcome,
    parse_retry_after,
    send_traces_batch,
)
from posthog.version import VERSION

PAYLOAD = {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "x"}]}]}]}


def fake_client(**overrides):
    base = dict(
        disabled=False,
        send=True,
        host="https://us.example.com/",
        api_key="phc_test_key",
        timeout=7,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def mock_session(status_code=200, headers=None):
    session = mock.Mock()
    session.post.return_value = mock.Mock(
        status_code=status_code, headers=headers or {}
    )
    return session


def send(client=None, payload=PAYLOAD, session=None):
    session = session or mock_session()
    with mock.patch("posthog.tracing._transport._get_session", return_value=session):
        outcome = send_traces_batch(client or fake_client(), payload)
    return outcome, session


class TestRequestShape:
    def test_posts_to_the_traces_endpoint_with_bearer_auth(self):
        outcome, session = send()
        assert outcome == SendOutcome("ok")
        args, kwargs = session.post.call_args
        assert args[0] == "https://us.example.com/i/v1/traces"
        assert kwargs["headers"]["Authorization"] == "Bearer phc_test_key"
        assert kwargs["headers"]["User-Agent"] == "posthog-python/" + VERSION
        assert kwargs["timeout"] == 7

    def test_does_not_put_the_project_key_in_the_query_string(self):
        _, session = send()
        assert "token=" not in session.post.call_args[0][0]

    def test_gzips_the_json_body_and_says_so(self):
        _, session = send()
        kwargs = session.post.call_args[1]
        assert kwargs["headers"]["Content-Encoding"] == "gzip"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert json.loads(gzip.decompress(kwargs["data"])) == PAYLOAD

    def test_falls_back_to_the_default_timeout(self):
        _, session = send(fake_client(timeout=None))
        assert session.post.call_args[1]["timeout"] == 15


class TestGates:
    def test_disabled_client_is_fatal_without_a_request(self):
        outcome, session = send(fake_client(disabled=True))
        assert outcome.kind == "fatal"
        assert not session.post.called

    def test_send_false_is_ok_without_a_request(self):
        outcome, session = send(fake_client(send=False))
        assert outcome.kind == "ok"
        assert not session.post.called

    def test_an_oversized_body_is_too_large_without_a_request(self):
        payload = {"resourceSpans": [{"blob": "x" * (OTLP_MAX_BODY_BYTES + 1)}]}
        outcome, session = send(payload=payload)
        assert outcome.kind == "too-large"
        assert outcome.measured_locally
        assert not session.post.called

    def test_a_413_is_not_marked_as_measured_locally(self):
        outcome, _ = send(session=mock_session(413))
        assert outcome.kind == "too-large"
        assert not outcome.measured_locally

    def test_the_limit_is_what_hosted_ingestion_accepts(self):
        assert OTLP_MAX_BODY_BYTES == 10 * 1024 * 1024

    def test_a_body_exactly_at_the_limit_is_sent(self):
        # {"s":""} is 8 bytes of JSON around the string.
        outcome, session = send(payload={"s": "x" * (OTLP_MAX_BODY_BYTES - 8)})
        assert outcome.kind == "ok"
        assert session.post.called

    def test_a_body_one_byte_over_the_limit_is_not_sent(self):
        outcome, session = send(payload={"s": "x" * (OTLP_MAX_BODY_BYTES - 7)})
        assert outcome.kind == "too-large"
        assert not session.post.called

    def test_measures_the_uncompressed_body(self):
        # Compresses to a few kilobytes.
        outcome, session = send(payload={"s": "\u2603" * OTLP_MAX_BODY_BYTES})
        assert outcome.kind == "too-large"
        assert not session.post.called


class TestOutcomes:
    @pytest.mark.parametrize(
        "status,kind",
        [
            (200, "ok"),
            (204, "ok"),
            (413, "too-large"),
            (408, "retry-later"),
            (429, "retry-later"),
            (500, "retry-later"),
            (503, "retry-later"),
            (400, "fatal"),
            (401, "fatal"),
            (404, "fatal"),
        ],
    )
    def test_maps_status_codes(self, status, kind):
        outcome, _ = send(session=mock_session(status))
        assert outcome.kind == kind

    def test_a_transport_error_is_retriable(self):
        session = mock.Mock()
        session.post.side_effect = requests.exceptions.ConnectionError("down")
        outcome, _ = send(session=session)
        assert outcome == SendOutcome("retry-later")

    def test_reads_retry_after_delta_seconds(self):
        outcome, _ = send(session=mock_session(429, {"Retry-After": "120"}))
        assert outcome == SendOutcome("retry-later", 120.0)

    def test_reads_retry_after_http_date(self):
        outcome, _ = send(
            session=mock_session(503, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})
        )
        assert outcome.kind == "retry-later"
        assert outcome.retry_after is not None and outcome.retry_after > 0


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("120", 120.0),
            ("  30 ", 30.0),
            ("60, 120", 60.0),
            ("Thu, 10 Sep 2026 12:00:30 GMT", 30.0),
        ],
    )
    def test_reads_both_wire_forms(self, value, expected):
        assert parse_retry_after(value, NOW) == expected

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "0",
            "-5",
            "+5",
            "5.5",
            "1e3",
            "10 minutes",
            "Wed, 21 Oct 2015 07:28:00 GMT",
            "Thu, 10 Sep 2026 12:00:00 GMT",
            42,
        ],
    )
    def test_treats_anything_else_as_absent(self, value):
        assert parse_retry_after(value, NOW) is None

    def test_ignores_an_unparseable_retry_after(self):
        outcome, _ = send(session=mock_session(429, {"Retry-After": "10 minutes"}))
        assert outcome == SendOutcome("retry-later", None)

    def test_survives_a_throwing_headers_object(self):
        response = mock.Mock(status_code=503)
        response.headers.get.side_effect = RuntimeError("no headers")
        session = mock.Mock()
        session.post.return_value = response
        outcome, _ = send(session=session)
        assert outcome == SendOutcome("retry-later", None)
