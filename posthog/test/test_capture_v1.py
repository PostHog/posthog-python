import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from parameterized import parameterized

from posthog.capture_v1 import (
    _CAPTURE_V1_PATH,
    _HEADER_ATTEMPT,
    _HEADER_REQUEST_ID,
    _HEADER_SDK_INFO,
    CaptureV1Error,
    _build_v1_batch_body,
    parse_v1_response,
    post_v1,
    send_v1_batch,
    _to_v1_event,
    _coerce_bool,
    _coerce_str,
)


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
    """Drop-in for ``post_v1`` that records calls and replays canned outcomes.

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
        gzip=False,
        timeout=15,
        session=None,
    ):
        self.calls.append(
            {
                "attempt": attempt,
                "request_id": request_id,
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


def _legacy_msg(event="my_event", properties=None, **overrides) -> dict:
    """Minimal legacy-shaped message as it looks coming off the queue."""
    msg = {
        "event": event,
        "uuid": "0190000000007000800000000000000a",
        "distinct_id": "user-1",
        "timestamp": "2026-06-27T12:00:00+00:00",
        "type": "capture",
        "properties": {"$lib": "posthog-python", "$lib_version": "9.9.9"},
    }
    if properties is not None:
        msg["properties"] = properties
    msg.update(overrides)
    return msg


class TestCoercion(unittest.TestCase):
    @parameterized.expand(
        [
            ("bool_true", True, True),
            ("bool_false", False, False),
            ("str_true", "true", True),
            ("str_true_upper", "TRUE", True),
            ("str_true_padded", "  true ", True),
            ("str_one", "1", True),
            ("str_false", "false", False),
            ("str_zero", "0", False),
            ("int_nonzero", 5, True),
            ("int_zero", 0, False),
            ("float_nonzero", 1.5, True),
            ("float_zero", 0.0, False),
            ("neg_int", -1, True),
            ("str_yes_uncoercible", "yes", None),
            ("str_empty_uncoercible", "", None),
            ("none_uncoercible", None, None),
            ("dict_uncoercible", {"a": 1}, None),
        ]
    )
    def test_coerce_bool(self, _name, value, expected) -> None:
        self.assertIs(_coerce_bool(value), expected)

    @parameterized.expand(
        [
            ("str", "tour-1", "tour-1"),
            ("empty_str", "", ""),
            ("int", 123, None),
            ("bool", True, None),
            ("none", None, None),
        ]
    )
    def test_coerce_str(self, _name, value, expected) -> None:
        self.assertEqual(_coerce_str(value), expected)


class TestToV1Event(unittest.TestCase):
    def test_required_fields_preserved(self) -> None:
        event = _to_v1_event(_legacy_msg(event="signed_up"))
        self.assertEqual(event["event"], "signed_up")
        self.assertEqual(event["uuid"], "0190000000007000800000000000000a")
        self.assertEqual(event["distinct_id"], "user-1")
        self.assertEqual(event["timestamp"], "2026-06-27T12:00:00+00:00")

    def test_strips_lib_and_lib_version(self) -> None:
        event = _to_v1_event(_legacy_msg())
        self.assertNotIn("$lib", event["properties"])
        self.assertNotIn("$lib_version", event["properties"])

    def test_options_empty_dict_when_no_sentinels(self) -> None:
        event = _to_v1_event(_legacy_msg(properties={"plain": "value"}))
        self.assertEqual(event["options"], {})
        self.assertEqual(event["properties"], {"plain": "value"})

    def test_does_not_leak_non_wire_top_level_keys(self) -> None:
        event = _to_v1_event(_legacy_msg())
        # `type` is legacy-only; the v1 event carries only documented fields.
        self.assertEqual(
            set(event),
            {"event", "uuid", "distinct_id", "timestamp", "options", "properties"},
        )

    def test_does_not_mutate_input(self) -> None:
        msg = _legacy_msg(
            properties={"$cookieless_mode": True, "$session_id": "s-1"},
            **{"$set": {"name": "Max"}},
        )
        original_properties = dict(msg["properties"])
        _to_v1_event(msg)
        self.assertEqual(msg["properties"], original_properties)
        self.assertIn("$set", msg)  # top-level $set untouched on the original

    @parameterized.expand(
        [
            ("cookieless_mode", "$cookieless_mode", "cookieless_mode", True, True),
            (
                "ignore_sent_at_rename",
                "$ignore_sent_at",
                "disable_skew_correction",
                "true",
                True,
            ),
            (
                "process_person_profile",
                "$process_person_profile",
                "process_person_profile",
                "false",
                False,
            ),
            (
                "product_tour_id",
                "$product_tour_id",
                "product_tour_id",
                "tour-7",
                "tour-7",
            ),
        ]
    )
    def test_option_sentinels_lifted_renamed_and_coerced(
        self, _name, prop_key, wire_key, raw, expected
    ) -> None:
        event = _to_v1_event(_legacy_msg(properties={prop_key: raw}))
        self.assertEqual(event["options"], {wire_key: expected})
        self.assertNotIn(prop_key, event["properties"])

    @parameterized.expand(
        [
            ("bad_bool", "$cookieless_mode", "maybe"),
            ("bad_tour_id_int", "$product_tour_id", 123),
        ]
    )
    def test_option_sentinel_removed_but_omitted_on_bad_coercion(
        self, _name, prop_key, raw
    ) -> None:
        event = _to_v1_event(_legacy_msg(properties={prop_key: raw}))
        # Removed from properties (sentinels must never reach v1 props) but not
        # emitted as an option, so a wrong type cannot 400 the whole batch.
        self.assertNotIn(prop_key, event["properties"])
        self.assertEqual(event["options"], {})

    @parameterized.expand(
        [
            ("session_id", "$session_id", "session_id", "s-123"),
            ("window_id", "$window_id", "window_id", "w-456"),
        ]
    )
    def test_top_level_string_sentinels(self, _name, prop_key, field_name, raw) -> None:
        event = _to_v1_event(_legacy_msg(properties={prop_key: raw}))
        self.assertEqual(event[field_name], raw)
        self.assertNotIn(prop_key, event["properties"])

    def test_top_level_sentinel_omitted_but_removed_when_not_string(self) -> None:
        event = _to_v1_event(_legacy_msg(properties={"$session_id": 42}))
        self.assertNotIn("session_id", event)
        self.assertNotIn("$session_id", event["properties"])

    def test_all_sentinels_together(self) -> None:
        event = _to_v1_event(
            _legacy_msg(
                properties={
                    "$cookieless_mode": True,
                    "$ignore_sent_at": "1",
                    "$product_tour_id": "tour-x",
                    "$process_person_profile": 0,
                    "$session_id": "s-1",
                    "$window_id": "w-1",
                    "$geoip_disable": True,
                    "custom": "keep",
                }
            )
        )
        self.assertEqual(
            event["options"],
            {
                "cookieless_mode": True,
                "disable_skew_correction": True,
                "product_tour_id": "tour-x",
                "process_person_profile": False,
            },
        )
        self.assertEqual(event["session_id"], "s-1")
        self.assertEqual(event["window_id"], "w-1")
        # Non-sentinel props (including $geoip_disable) are left intact.
        self.assertEqual(
            event["properties"], {"$geoip_disable": True, "custom": "keep"}
        )

    @parameterized.expand([("set", "$set"), ("set_once", "$set_once")])
    def test_top_level_set_relocated_into_properties(self, _name, key) -> None:
        msg = _legacy_msg(properties={}, **{key: {"email": "a@b.com"}})
        event = _to_v1_event(msg)
        self.assertEqual(event["properties"][key], {"email": "a@b.com"})
        self.assertNotIn(key, event)  # not a top-level v1 field

    def test_top_level_set_merges_with_existing_properties_set(self) -> None:
        # properties wins on key collision.
        msg = _legacy_msg(
            properties={"$set": {"a": "from_props", "b": "props_only"}},
            **{"$set": {"a": "from_top", "c": "top_only"}},
        )
        event = _to_v1_event(msg)
        self.assertEqual(
            event["properties"]["$set"],
            {"a": "from_props", "b": "props_only", "c": "top_only"},
        )

    def test_groups_left_in_properties(self) -> None:
        event = _to_v1_event(_legacy_msg(properties={"$groups": {"company": "ph"}}))
        self.assertEqual(event["properties"]["$groups"], {"company": "ph"})

    def test_timestamp_naive_datetime_made_tz_aware(self) -> None:
        event = _to_v1_event(_legacy_msg(timestamp=datetime(2026, 6, 27, 12, 0, 0)))
        parsed = datetime.fromisoformat(event["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_timestamp_none_defaults_to_utc_now(self) -> None:
        event = _to_v1_event(_legacy_msg(timestamp=None))
        parsed = datetime.fromisoformat(event["timestamp"])
        self.assertEqual(parsed.tzinfo, timezone.utc)


class TestBuildV1BatchBody(unittest.TestCase):
    def test_envelope_shape_and_no_legacy_fields(self) -> None:
        events = [{"event": "e"}]
        body = _build_v1_batch_body(events)
        self.assertEqual(body["batch"], events)
        self.assertNotIn("api_key", body)
        self.assertNotIn("sent_at", body)

    def test_created_at_is_tz_aware_rfc3339(self) -> None:
        body = _build_v1_batch_body([])
        parsed = datetime.fromisoformat(body["created_at"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_historical_migration_omitted_when_false(self) -> None:
        self.assertNotIn("historical_migration", _build_v1_batch_body([]))

    def test_historical_migration_present_when_true(self) -> None:
        body = _build_v1_batch_body([], historical_migration=True)
        self.assertIs(body["historical_migration"], True)


class TestPostV1(unittest.TestCase):
    def _post(self, response, **kwargs):
        session = _RecordingSession(response)
        body = _build_v1_batch_body([_to_v1_event(_msg("u-1"))])
        post_v1(
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

    def test_required_headers_present(self) -> None:
        headers = self._post(_results_response({}))["headers"]
        self.assertEqual(headers["Authorization"], "Bearer phc_key")
        self.assertEqual(headers[_HEADER_ATTEMPT], "2")
        self.assertEqual(headers[_HEADER_REQUEST_ID], "req-123")
        self.assertTrue(headers[_HEADER_SDK_INFO].startswith("posthog-python/"))
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_no_api_key_in_body(self) -> None:
        # v1 authenticates via the Bearer header; the key must not leak into the body.
        data = self._post(_results_response({}))["data"]
        self.assertNotIn("phc_key", data)
        self.assertNotIn("api_key", json.loads(data))

    def test_uncompressed_body_is_json_str_without_encoding_header(self) -> None:
        call = self._post(_results_response({}), gzip=False)
        self.assertIsInstance(call["data"], str)
        self.assertNotIn("Content-Encoding", call["headers"])

    def test_gzip_sets_encoding_header_and_compresses_body(self) -> None:
        call = self._post(_results_response({}), gzip=True)
        self.assertEqual(call["headers"]["Content-Encoding"], "gzip")
        self.assertIsInstance(call["data"], bytes)
        self.assertEqual(call["data"][:2], b"\x1f\x8b")  # gzip magic


class TestParseV1Response(unittest.TestCase):
    def test_success_parses_results_with_details(self) -> None:
        res = _FakeResponse(
            200,
            json_body={"results": {"u-1": {"result": "drop", "details": "spam"}}},
        )
        parsed = parse_v1_response(res)
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
        parsed = parse_v1_response(res)
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
        parsed = parse_v1_response(_FakeResponse(400, json_body=body))
        self.assertFalse(parsed.is_success)
        self.assertEqual(parsed.error_message, expected)

    def test_error_message_falls_back_to_text(self) -> None:
        parsed = parse_v1_response(_FakeResponse(400, raise_json=True, text="boom"))
        self.assertEqual(parsed.error_message, "boom")

    @parameterized.expand([("numeric", "2", 2.0), ("absent", None, None)])
    def test_retry_after_header(self, _name, header_value, expected) -> None:
        headers = {"Retry-After": header_value} if header_value is not None else {}
        parsed = parse_v1_response(_FakeResponse(503, headers=headers))
        self.assertEqual(parsed.retry_after, expected)


class TestSendV1Batch(unittest.TestCase):
    """Drives ``send_v1_batch`` with a stubbed ``post_v1`` and no real sleeps."""

    def setUp(self) -> None:
        sleep_patch = mock.patch("posthog.capture_v1.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _run(self, batch, outcomes, **kwargs):
        stub = _PostV1Stub(outcomes)
        with mock.patch("posthog.capture_v1.post_v1", stub):
            send_v1_batch("phc_key", "https://app.posthog.com", batch, **kwargs)
        return stub

    def _run_expecting_error(self, batch, outcomes, **kwargs):
        stub = _PostV1Stub(outcomes)
        with mock.patch("posthog.capture_v1.post_v1", stub):
            with self.assertRaises(CaptureV1Error) as ctx:
                send_v1_batch("phc_key", "https://app.posthog.com", batch, **kwargs)
        return stub, ctx.exception

    def test_all_ok_sends_once(self) -> None:
        stub = self._run([_msg("u-1")], [_results_response({"u-1": "ok"})])
        self.assertEqual(len(stub.calls), 1)
        self.sleep.assert_not_called()

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

    def test_request_id_stable_and_attempt_increments(self) -> None:
        stub = self._run(
            [_msg("u-1")],
            [
                _results_response({"u-1": "retry"}),
                _results_response({"u-1": "ok"}),
            ],
        )
        self.assertEqual(stub.calls[0]["request_id"], stub.calls[1]["request_id"])
        self.assertEqual([c["attempt"] for c in stub.calls], [1, 2])

    def test_drop_on_2xx_is_logged_not_raised(self) -> None:
        batch = [_msg("u-ok"), _msg("u-drop")]
        with self.assertLogs("posthog", level="WARNING") as logs:
            stub = self._run(
                batch,
                [_results_response({"u-ok": "ok", "u-drop": ("drop", "invalid")})],
            )
        self.assertEqual(len(stub.calls), 1)
        self.assertTrue(any("u-drop" in line for line in logs.output))

    def test_retry_exhausted_raises_with_uuids(self) -> None:
        stub, exc = self._run_expecting_error(
            [_msg("u-1")],
            [_results_response({"u-1": "retry"}), _results_response({"u-1": "retry"})],
            max_retries=1,
        )
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(exc.retry_exhausted, ["u-1"])

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

    def test_transport_error_exhausted_reraises_original(self) -> None:
        stub = _PostV1Stub([ConnectionError("boom"), ConnectionError("boom")])
        with mock.patch("posthog.capture_v1.post_v1", stub):
            with self.assertRaises(ConnectionError):
                send_v1_batch(
                    "phc_key", "https://app.posthog.com", [_msg("u-1")], max_retries=1
                )
        self.assertEqual(len(stub.calls), 2)
