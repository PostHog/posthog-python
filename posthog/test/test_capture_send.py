import json
import unittest
import zlib
from datetime import datetime, timedelta
from unittest import mock
from uuid import UUID

import zstandard

from parameterized import parameterized

from posthog.capture_compression import CaptureCompression
from posthog.capture_event import _build_v1_batch_body, _to_v1_event
from posthog.capture_send import (
    _CAPTURE_AI_V1_PATH,
    _CAPTURE_V1_PATH,
    _HEADER_ATTEMPT,
    _HEADER_REQUEST_ID,
    _HEADER_REQUEST_TIMESTAMP,
    _HEADER_SDK_INFO,
    _MAX_BACKOFF_SECONDS,
    CaptureError,
    CaptureEventResult,
    _capture_loss_message,
    _parse_v1_response,
    _post_v1,
    _send_v1_batch,
    _backoff,
)
from posthog.request import USER_AGENT


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` for transport tests."""

    def __init__(
        self, status_code, *, json_body=None, headers=None, text="", raise_json=False
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_body = json_body
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._json_body


class _RecordingSession:
    """Captures the args of a single ``.post`` and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "timeout": timeout}
        )
        return self._response


class _PostV1Stub:
    """Drop-in for ``_post_v1`` that records calls and replays canned outcomes.

    Each item in ``outcomes`` is either a ``_FakeResponse`` to return or an
    ``Exception`` instance to raise (simulating a transport failure).
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def __call__(
        self,
        api_key,
        host,
        batch_body,
        *,
        attempt,
        request_id,
        compression=CaptureCompression.NONE,
        timeout=15,
        sdk_info=USER_AGENT,
        session=None,
        path=_CAPTURE_V1_PATH,
    ):
        self.calls.append(
            {
                "path": path,
                "attempt": attempt,
                "request_id": request_id,
                "compression": compression,
                "sdk_info": sdk_info,
                "created_at": batch_body["created_at"],
                "uuids": [e["uuid"] for e in batch_body["batch"]],
            }
        )
        outcome = self._outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _msg(uuid, event="e", **overrides):
    msg = {
        "event": event,
        "uuid": uuid,
        "distinct_id": "user-1",
        "timestamp": "2026-06-27T12:00:00+00:00",
        "type": "capture",
        "properties": {},
    }
    msg.update(overrides)
    return msg


def _results_response(directives, headers=None):
    """200 response whose ``results`` map tags each uuid.

    ``directives`` maps uuid -> ``"ok"`` (or any result string) or a
    ``(result, details)`` tuple.
    """
    results = {}
    for uid, spec in directives.items():
        result, details = spec if isinstance(spec, tuple) else (spec, None)
        results[uid] = {"result": result, "details": details}
    return _FakeResponse(200, json_body={"results": results}, headers=headers)


class TestPostV1(unittest.TestCase):
    def _post(self, response, **kwargs):
        session = _RecordingSession(response)
        body = _build_v1_batch_body([_to_v1_event(_msg("u-1"))])
        _post_v1(
            "phc_key",
            "https://app.posthog.com/",
            body,
            attempt=2,
            request_id="req-123",
            session=session,
            **kwargs,
        )
        return session.calls[0]

    def test_url_uses_v1_path_and_trims_host(self) -> None:
        call = self._post(_results_response({}))
        self.assertEqual(call["url"], "https://app.posthog.com" + _CAPTURE_V1_PATH)
        call = self._post(_results_response({}), path=_CAPTURE_AI_V1_PATH)
        self.assertEqual(call["url"], "https://app.posthog.com" + _CAPTURE_AI_V1_PATH)

    def test_required_headers_present(self) -> None:
        headers = self._post(_results_response({}))["headers"]
        self.assertEqual(headers["Authorization"], "Bearer phc_key")
        self.assertEqual(headers[_HEADER_ATTEMPT], "2")
        self.assertEqual(headers[_HEADER_REQUEST_ID], "req-123")
        self.assertTrue(headers[_HEADER_SDK_INFO].startswith("posthog-python/"))
        self.assertEqual(headers["Content-Type"], "application/json")
        request_timestamp = datetime.fromisoformat(headers[_HEADER_REQUEST_TIMESTAMP])
        self.assertEqual(request_timestamp.utcoffset(), timedelta(0))

    def test_custom_sdk_info_headers(self) -> None:
        headers = self._post(
            _results_response({}), sdk_info="posthog-python-mcp/0.3.0"
        )["headers"]
        self.assertEqual(headers[_HEADER_SDK_INFO], "posthog-python-mcp/0.3.0")
        self.assertEqual(headers["User-Agent"], "posthog-python-mcp/0.3.0")

    def test_no_api_key_in_body(self) -> None:
        # v1 authenticates via the Bearer header; the key must not leak into the body.
        data = self._post(_results_response({}))["data"]
        self.assertNotIn("phc_key", data)
        self.assertNotIn("api_key", json.loads(data))

    def test_uncompressed_body_is_json_str_without_encoding_header(self) -> None:
        call = self._post(_results_response({}), compression=CaptureCompression.NONE)
        self.assertIsInstance(call["data"], str)
        self.assertNotIn("Content-Encoding", call["headers"])

    def test_gzip_sets_encoding_header_and_compresses_body(self) -> None:
        call = self._post(_results_response({}), compression=CaptureCompression.GZIP)
        self.assertEqual(call["headers"]["Content-Encoding"], "gzip")
        self.assertIsInstance(call["data"], bytes)
        self.assertEqual(call["data"][:2], b"\x1f\x8b")  # gzip magic

    def test_deflate_sets_encoding_header_and_zlib_wraps_body(self) -> None:
        # Must be zlib-wrapped (RFC 1950, 0x78 prefix), matching posthog-go /
        # posthog-rs, so the server routes Content-Encoding: deflate to its zlib
        # decoder rather than treating it as raw deflate.
        call = self._post(_results_response({}), compression=CaptureCompression.DEFLATE)
        self.assertEqual(call["headers"]["Content-Encoding"], "deflate")
        self.assertIsInstance(call["data"], bytes)
        self.assertEqual(call["data"][0], 0x78)  # zlib header
        roundtripped = zlib.decompress(call["data"]).decode("utf-8")
        self.assertNotIn("api_key", json.loads(roundtripped))

    def test_zstd_sets_encoding_header_and_emits_standard_frame(self) -> None:
        call = self._post(_results_response({}), compression=CaptureCompression.ZSTD)
        self.assertEqual(call["headers"]["Content-Encoding"], "zstd")
        self.assertIsInstance(call["data"], bytes)
        self.assertEqual(call["data"][:4], b"\x28\xb5\x2f\xfd")  # zstd frame magic
        roundtripped = zstandard.ZstdDecompressor().decompress(call["data"])
        body = json.loads(roundtripped.decode("utf-8"))
        self.assertNotIn("api_key", body)
        self.assertEqual(len(body["batch"]), 1)

    def test_zstd_without_package_raises_actionable_error(self) -> None:
        with mock.patch("posthog.capture_send._zstandard", None):
            with self.assertRaises(ValueError) as ctx:
                self._post(_results_response({}), compression=CaptureCompression.ZSTD)
        self.assertIn("posthog[zstd]", str(ctx.exception))


class TestParseV1Response(unittest.TestCase):
    def test_success_parses_results_with_details(self) -> None:
        res = _FakeResponse(
            200,
            json_body={"results": {"u-1": {"result": "drop", "details": "spam"}}},
        )
        parsed = _parse_v1_response(res)
        self.assertTrue(parsed.is_success)
        self.assertEqual(parsed.results["u-1"].result, "drop")
        self.assertEqual(parsed.results["u-1"].details, "spam")

    @parameterized.expand(
        [
            ("unparseable_body", _FakeResponse(200, raise_json=True)),
            ("missing_results_key", _FakeResponse(200, json_body={"foo": 1})),
        ]
    )
    def test_success_with_bad_body_is_malformed(self, _name, res) -> None:
        parsed = _parse_v1_response(res)
        self.assertTrue(parsed.is_success)
        self.assertTrue(parsed.malformed)

    @parameterized.expand(
        [
            ("error_description", {"error_description": "bad batch"}, "bad batch"),
            ("error", {"error": "validation_error"}, "validation_error"),
            ("detail", {"detail": "nope"}, "nope"),
        ]
    )
    def test_error_message_extracted_from_body(self, _name, body, expected) -> None:
        parsed = _parse_v1_response(_FakeResponse(400, json_body=body))
        self.assertFalse(parsed.is_success)
        self.assertEqual(parsed.error_message, expected)

    def test_error_message_falls_back_to_text(self) -> None:
        parsed = _parse_v1_response(_FakeResponse(400, raise_json=True, text="boom"))
        self.assertEqual(parsed.error_message, "boom")

    @parameterized.expand([("numeric", "2", 2.0), ("absent", None, None)])
    def test_retry_after_header(self, _name, header_value, expected) -> None:
        headers = {"Retry-After": header_value} if header_value is not None else {}
        parsed = _parse_v1_response(_FakeResponse(503, headers=headers))
        self.assertEqual(parsed.retry_after, expected)


class TestSendV1Batch(unittest.TestCase):
    """Drives ``_send_v1_batch`` with a stubbed ``_post_v1`` and no real sleeps."""

    def setUp(self) -> None:
        sleep_patch = mock.patch("posthog.capture_send.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _run(self, batch, outcomes, **kwargs):
        stub = _PostV1Stub(outcomes)
        with mock.patch("posthog.capture_send._post_v1", stub):
            _send_v1_batch("phc_key", "https://app.posthog.com", batch, **kwargs)
        return stub

    def _run_expecting_error(self, batch, outcomes, **kwargs):
        stub = _PostV1Stub(outcomes)
        with mock.patch("posthog.capture_send._post_v1", stub):
            with self.assertRaises(CaptureError) as ctx:
                _send_v1_batch("phc_key", "https://app.posthog.com", batch, **kwargs)
        return stub, ctx.exception

    def test_all_ok_sends_once(self) -> None:
        stub = self._run([_msg("u-1")], [_results_response({"u-1": "ok"})])
        self.assertEqual(len(stub.calls), 1)
        self.sleep.assert_not_called()

    def test_custom_sdk_info_is_forwarded(self) -> None:
        stub = self._run(
            [_msg("u-1")],
            [_results_response({"u-1": "ok"})],
            sdk_info="posthog-python-mcp/0.3.0",
        )
        self.assertEqual(stub.calls[0]["sdk_info"], "posthog-python-mcp/0.3.0")

    def test_absent_uuid_treated_as_accepted(self) -> None:
        # Empty results map: the event is neither retried nor errored.
        stub = self._run([_msg("u-1")], [_results_response({})])
        self.assertEqual(len(stub.calls), 1)

    def test_partial_retry_resends_only_retry_events(self) -> None:
        batch = [_msg("u-ok"), _msg("u-retry")]
        stub = self._run(
            batch,
            [
                _results_response({"u-ok": "ok", "u-retry": "retry"}),
                _results_response({"u-retry": "ok"}),
            ],
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(stub.calls[0]["uuids"], ["u-ok", "u-retry"])
        # Second attempt carries only the event the server asked to retry.
        self.assertEqual(stub.calls[1]["uuids"], ["u-retry"])

    def test_request_id_and_created_at_stable_attempt_increments(self) -> None:
        stub = self._run(
            [_msg("u-1")],
            [
                _results_response({"u-1": "retry"}),
                _results_response({"u-1": "ok"}),
            ],
        )
        self.assertEqual(stub.calls[0]["request_id"], stub.calls[1]["request_id"])
        self.assertEqual(UUID(stub.calls[0]["request_id"]).version, 7)
        # created_at is hoisted once, so the envelope timestamp is identical
        # across retry attempts (only the attempt header increments).
        self.assertEqual(stub.calls[0]["created_at"], stub.calls[1]["created_at"])
        self.assertEqual([c["attempt"] for c in stub.calls], [1, 2])

    def test_compression_and_path_forwarded_to_post_v1(self) -> None:
        stub = self._run(
            [_msg("u-1")],
            [_results_response({"u-1": "ok"})],
            compression=CaptureCompression.DEFLATE,
            path=_CAPTURE_AI_V1_PATH,
        )
        self.assertEqual(stub.calls[0]["compression"], CaptureCompression.DEFLATE)
        self.assertEqual(stub.calls[0]["path"], _CAPTURE_AI_V1_PATH)

    def test_drop_on_2xx_surfaces_via_error(self) -> None:
        # A server-chosen drop is terminal: even on an all-ok-otherwise 2xx with
        # no retry events, the send raises so on_error sees the dropped uuid
        # (matches posthog-go/posthog-rs — a 2xx is not full delivery).
        batch = [_msg("u-ok"), _msg("u-drop")]
        stub, exc = self._run_expecting_error(
            batch,
            [_results_response({"u-ok": "ok", "u-drop": ("drop", "invalid")})],
        )
        self.assertEqual(len(stub.calls), 1)  # terminal, not retried
        self.assertEqual(exc.status, 200)
        self.assertEqual(exc.drops, [("u-drop", "invalid")])
        self.assertEqual(exc.retry_exhausted, [])

    def test_all_ok_no_drops_does_not_raise(self) -> None:
        # The success path is unchanged when the server drops nothing.
        stub = self._run(
            [_msg("u-1"), _msg("u-2")],
            [_results_response({"u-1": "ok", "u-2": "warning"})],
        )
        self.assertEqual(len(stub.calls), 1)

    def test_drop_accumulated_across_attempts_surfaces_on_later_success(self) -> None:
        # Attempt 1 drops one event and retries another; attempt 2 clears the
        # retry. The earlier drop must still surface — it is not lost when the
        # outstanding retries succeed on a later 2xx.
        batch = [_msg("u-drop"), _msg("u-retry")]
        stub, exc = self._run_expecting_error(
            batch,
            [
                _results_response(
                    {"u-drop": ("drop", "llm_events_over_quota"), "u-retry": "retry"}
                ),
                _results_response({"u-retry": "ok"}),
            ],
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(stub.calls[1]["uuids"], ["u-retry"])  # only retry resent
        self.assertEqual(exc.drops, [("u-drop", "llm_events_over_quota")])
        self.assertEqual(exc.retry_exhausted, [])

    def test_retry_exhausted_raises_with_uuids(self) -> None:
        stub, exc = self._run_expecting_error(
            [_msg("u-1")],
            [_results_response({"u-1": "retry"}), _results_response({"u-1": "retry"})],
            max_retries=1,
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(exc.retry_exhausted, ["u-1"])
        self.assertEqual(exc.drops, [])

    def test_retry_exhausted_carries_earlier_drops(self) -> None:
        # A drop seen on attempt 1 rides along on the retry-exhaustion error.
        batch = [_msg("u-ok"), _msg("u-drop"), _msg("u-retry")]
        stub, exc = self._run_expecting_error(
            batch,
            [
                _results_response(
                    {
                        "u-ok": "ok",
                        "u-drop": ("drop", "llm_events_over_quota"),
                        "u-retry": "retry",
                    }
                ),
                _results_response({"u-retry": ("retry", "not_persisted")}),
            ],
            max_retries=1,
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(exc.endpoint, _CAPTURE_V1_PATH)
        self.assertEqual(exc.retry_exhausted, ["u-retry"])
        self.assertEqual(exc.drops, [("u-drop", "llm_events_over_quota")])
        self.assertEqual(
            exc.event_results,
            {
                "u-ok": CaptureEventResult("ok"),
                "u-drop": CaptureEventResult("drop", "llm_events_over_quota"),
                "u-retry": CaptureEventResult("retry", "not_persisted"),
            },
        )
        self.assertEqual(
            exc.verdict_summary(), "drop/llm_events_over_quota=1, retry/not_persisted=1"
        )

    @parameterized.expand(
        [
            ("uppercase", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            ("no_hyphens", "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"),
            ("braced", "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}"),
            ("urn", "urn:uuid:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ]
    )
    def test_results_match_non_canonical_uuid(self, _name, sent_uuid) -> None:
        # Capture parses any of these forms but keys results canonically, so a
        # verdict for a uuid `before_send` rewrote must still reach its event.
        canonical = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        stub, exc = self._run_expecting_error(
            [_msg(sent_uuid)],
            [
                _results_response({canonical: "retry"}),
                _results_response({canonical: ("drop", "llm_events_over_quota")}),
            ],
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(exc.drops, [(canonical, "llm_events_over_quota")])

    def test_malformed_2xx_is_terminal(self) -> None:
        stub, exc = self._run_expecting_error(
            [_msg("u-1")], [_FakeResponse(200, raise_json=True)]
        )
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(exc.status, 200)

    @parameterized.expand([("bad_request", 400), ("rate_limited", 429)])
    def test_terminal_status_raises_immediately(self, _name, status) -> None:
        stub, exc = self._run_expecting_error(
            [_msg("u-1")],
            [_FakeResponse(status, json_body={"error": "nope"})],
            max_retries=2,
        )
        self.assertEqual(len(stub.calls), 1)  # not retried
        self.assertEqual(exc.status, status)

    def test_retryable_status_then_success(self) -> None:
        stub = self._run(
            [_msg("u-1")],
            [
                _FakeResponse(503, headers={"Retry-After": "2"}),
                _results_response({"u-1": "ok"}),
            ],
        )
        self.assertEqual(len(stub.calls), 2)
        self.sleep.assert_called_once_with(2.0)  # honored Retry-After

    def test_retryable_status_exhausted_raises(self) -> None:
        stub, exc = self._run_expecting_error(
            [_msg("u-1")], [_FakeResponse(503), _FakeResponse(503)], max_retries=1
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(exc.status, 503)

    def test_transport_error_then_success(self) -> None:
        stub = self._run(
            [_msg("u-1")],
            [ConnectionError("boom"), _results_response({"u-1": "ok"})],
        )
        self.assertEqual(len(stub.calls), 2)

    def test_transport_error_exhausted_raises_capture_error(self) -> None:
        stub = _PostV1Stub([ConnectionError("boom"), ConnectionError("boom")])
        with mock.patch("posthog.capture_send._post_v1", stub):
            with self.assertRaises(CaptureError) as ctx:
                _send_v1_batch(
                    "phc_key",
                    "https://app.posthog.com",
                    [_msg("u-1")],
                    max_retries=1,
                    path=_CAPTURE_AI_V1_PATH,
                )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(ctx.exception.endpoint, _CAPTURE_AI_V1_PATH)
        self.assertEqual(ctx.exception.attempts, 2)
        self.assertIsInstance(ctx.exception.__cause__, ConnectionError)

    def test_negative_max_retries_still_attempts_delivery_once(self) -> None:
        stub = _PostV1Stub([_results_response({"u-1": "ok"})])

        with mock.patch("posthog.capture_send._post_v1", stub):
            _send_v1_batch(
                "phc_key", "https://app.posthog.com", [_msg("u-1")], max_retries=-1
            )

        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["attempt"], 1)

    def test_small_retry_after_does_not_shorten_backoff(self) -> None:
        # A Retry-After smaller than the configured backoff must not make the
        # client retry earlier than its own schedule (Retry-After is a minimum).
        # attempt_index=1 -> configured backoff 0.2s; Retry-After 0.1s is ignored.
        stub = self._run(
            [_msg("u-1")],
            [
                _results_response({"u-1": "retry"}),
                _results_response({"u-1": "retry"}, headers={"Retry-After": "0.1"}),
                _results_response({"u-1": "ok"}),
            ],
            max_retries=3,
        )
        self.assertEqual(len(stub.calls), 3)
        # First backoff (attempt_index 0) waits 0.1s; second (attempt_index 1)
        # keeps the 0.2s configured backoff rather than the smaller 0.1s header.
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.1, 0.2])


class TestCaptureLossMessage(unittest.TestCase):
    @parameterized.expand(
        [
            (
                "partial_2xx",
                CaptureError(
                    200,
                    "2 event(s) not delivered",
                    endpoint=_CAPTURE_V1_PATH,
                    drops=[("u-drop", "llm_events_over_quota")],
                    retry_exhausted=["u-retry"],
                    event_results={
                        "u-ok": CaptureEventResult("ok"),
                        "u-drop": CaptureEventResult("drop", "llm_events_over_quota"),
                        "u-retry": CaptureEventResult("retry", "not_persisted"),
                    },
                ),
                3,
                "2 event(s) not persisted by /i/v1/analytics/events: 1 dropped, "
                "1 out of retries (drop/llm_events_over_quota=1, retry/not_persisted=1)",
            ),
            (
                "request_failure_after_partial_success",
                CaptureError(
                    503,
                    "unavailable",
                    endpoint=_CAPTURE_AI_V1_PATH,
                    event_results={
                        "u-ok": CaptureEventResult("warning"),
                        "u-retry": CaptureEventResult("retry"),
                    },
                ),
                2,
                "1 event(s) not persisted by /i/v1/ai/events: CaptureError (status=503)",
            ),
            (
                "other_exception_uses_caller_endpoint",
                ValueError("bad payload"),
                4,
                "4 event(s) not persisted by /i/v1/analytics/events: ValueError",
            ),
        ]
    )
    def test_message(self, _name, error, batch_size, expected) -> None:
        self.assertEqual(
            _capture_loss_message(error, batch_size, _CAPTURE_V1_PATH), expected
        )


class TestBackoff(unittest.TestCase):
    """Directly exercises ``_backoff``'s Retry-After-as-minimum + cap policy."""

    @parameterized.expand(
        [
            # (attempt_index, retry_after, expected sleep seconds)
            ("first_no_header", 0, None, 0.1),
            ("second_no_header", 1, None, 0.2),
            ("exp_capped_at_30", 10, None, 30),
            ("zero_header_uses_backoff", 0, 0, 0.1),
            ("larger_header_wins", 0, 5.0, 5.0),
            ("smaller_header_ignored", 3, 0.5, 0.8),  # configured 0.8 > 0.5
            ("equal_header_and_backoff", 1, 0.2, 0.2),
            ("header_at_ceiling", 0, 30.0, 30),
            ("header_above_ceiling_clamped", 0, 120.0, _MAX_BACKOFF_SECONDS),
            ("absurd_header_clamped", 0, 10**9, _MAX_BACKOFF_SECONDS),
        ]
    )
    def test_backoff(self, _name, attempt_index, retry_after, expected) -> None:
        with mock.patch("posthog.capture_send.time.sleep") as sleep:
            _backoff(attempt_index, retry_after)
            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], expected)
