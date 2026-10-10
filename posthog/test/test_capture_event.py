import unittest
from datetime import datetime, timedelta, timezone

from parameterized import parameterized

from uuid import UUID

from posthog.capture_event import (
    _build_v1_batch_body,
    _canonical_event_uuid,
    _to_v1_event,
)


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


_CANONICAL_UUID = "0190a8f3-1b2c-7d4e-8f90-123456789abc"


class TestCanonicalEventUuid(unittest.TestCase):
    @parameterized.expand(
        [
            ("canonical", _CANONICAL_UUID),
            ("uppercase", _CANONICAL_UUID.upper()),
            ("unhyphenated", _CANONICAL_UUID.replace("-", "")),
            ("braced", "{" + _CANONICAL_UUID + "}"),
            ("urn", "urn:uuid:" + _CANONICAL_UUID),
            ("urn_uppercase", "URN:UUID:" + _CANONICAL_UUID.upper()),
            ("uuid_instance", UUID(_CANONICAL_UUID)),
        ]
    )
    def test_accepted_forms_are_canonicalized(self, _name, value) -> None:
        self.assertEqual(_canonical_event_uuid(value), _CANONICAL_UUID)

    @parameterized.expand(
        [
            ("bare_uuid_prefix", "uuid:" + _CANONICAL_UUID),
            ("misplaced_hyphen", "0190a8f31-b2c-7d4e-8f90-123456789abc"),
            ("surrounding_whitespace", " " + _CANONICAL_UUID),
            ("braced_unhyphenated", "{" + _CANONICAL_UUID.replace("-", "") + "}"),
            ("too_short", _CANONICAL_UUID[:-1]),
            ("empty", ""),
            ("int", 123),
        ]
    )
    def test_other_forms_are_rejected(self, _name, value) -> None:
        self.assertIsNone(_canonical_event_uuid(value))


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
            options={"product_tour_id": "tour-1"},
            **{"$set": {"name": "Max"}},
        )
        original_properties = dict(msg["properties"])
        _to_v1_event(msg)
        self.assertEqual(msg["properties"], original_properties)
        self.assertEqual(msg["options"], {"product_tour_id": "tour-1"})
        self.assertIn("$set", msg)  # top-level $set untouched on the original

    @parameterized.expand(
        [
            ("cookieless_mode", "$cookieless_mode", "cookieless_mode", True),
            ("ignore_sent_at", "$ignore_sent_at", "disable_skew_correction", "true"),
            (
                "process_person_profile",
                "$process_person_profile",
                "process_person_profile",
                0,
            ),
            ("product_tour_id", "$product_tour_id", "product_tour_id", 123),
        ]
    )
    def test_legacy_property_fills_option_unchanged(
        self, _name, prop_key, option_key, raw
    ) -> None:
        event = _to_v1_event(_legacy_msg(properties={prop_key: raw}))
        self.assertEqual(event["options"], {option_key: raw})
        self.assertNotIn(prop_key, event["properties"])

    @parameterized.expand(
        [
            ("option_set", {"cookieless_mode": False}, False),
            ("option_null", {"cookieless_mode": None}, True),
            ("option_missing", {}, True),
        ]
    )
    def test_caller_option_wins_over_legacy_property(
        self, _name, options, expected
    ) -> None:
        event = _to_v1_event(
            _legacy_msg(properties={"$cookieless_mode": True}, options=options)
        )
        self.assertEqual(event["options"], {"cookieless_mode": expected})
        self.assertNotIn("$cookieless_mode", event["properties"])

    def test_caller_options_pass_through_unchanged(self) -> None:
        options = {"process_person_profile": "false", "future_option": {"a": [1]}}
        event = _to_v1_event(_legacy_msg(options=options))
        self.assertEqual(event["options"], options)

    @parameterized.expand([("list", ["x"]), ("string", "x"), ("int", 1)])
    def test_non_dict_options_are_logged_and_ignored(self, _name, options) -> None:
        with self.assertLogs("posthog", level="ERROR") as logs:
            event = _to_v1_event(
                _legacy_msg(properties={"$cookieless_mode": True}, options=options)
            )
        self.assertEqual(event["options"], {"cookieless_mode": True})
        self.assertIn("options must be a dict", logs.output[0])

    @parameterized.expand(
        [
            ("session_id", "$session_id", "session_id", "s-123"),
            ("window_id", "$window_id", "window_id", "w-456"),
            ("empty_string", "$session_id", "session_id", ""),
        ]
    )
    def test_top_level_string_sentinels(self, _name, prop_key, field_name, raw) -> None:
        event = _to_v1_event(_legacy_msg(properties={prop_key: raw}))
        self.assertEqual(event[field_name], raw)
        self.assertNotIn(prop_key, event["properties"])

    @parameterized.expand(
        [
            ("number", "$session_id", 42, "number"),
            ("bool", "$window_id", True, "bool"),
            ("array", "$session_id", ["s-1"], "array"),
            ("object", "$window_id", {"id": "w-1"}, "object"),
        ]
    )
    def test_non_string_sentinel_is_dropped_with_a_warning(
        self, _name, prop_key, raw, type_name
    ) -> None:
        with self.assertLogs("posthog", level="WARNING") as logs:
            event = _to_v1_event(_legacy_msg(properties={prop_key: raw}))
        self.assertNotIn(prop_key.lstrip("$"), event)
        self.assertNotIn(prop_key, event["properties"])
        self.assertIn(
            f"dropping {prop_key}: a {type_name} value is not a string",
            logs.records[0].getMessage(),
        )

    def test_null_sentinel_is_dropped_silently(self) -> None:
        with self.assertNoLogs("posthog", level="WARNING"):
            event = _to_v1_event(_legacy_msg(properties={"$session_id": None}))
        self.assertNotIn("session_id", event)
        self.assertNotIn("$session_id", event["properties"])

    def test_all_sentinels_together(self) -> None:
        event = _to_v1_event(
            _legacy_msg(
                properties={
                    "$cookieless_mode": True,
                    "$ignore_sent_at": True,
                    "$product_tour_id": "tour-x",
                    "$process_person_profile": False,
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
        msg = _legacy_msg(
            properties={"$set": {"a": "from_props", "b": "props_only"}},
            **{"$set": {"a": "from_top", "c": "top_only"}},
        )
        event = _to_v1_event(msg)
        self.assertEqual(
            event["properties"]["$set"],
            {"a": "from_top", "b": "props_only", "c": "top_only"},
        )

    def test_groups_left_in_properties(self) -> None:
        event = _to_v1_event(_legacy_msg(properties={"$groups": {"company": "ph"}}))
        self.assertEqual(event["properties"]["$groups"], {"company": "ph"})

    def test_timestamp_naive_datetime_made_tz_aware(self) -> None:
        event = _to_v1_event(_legacy_msg(timestamp=datetime(2026, 6, 27, 12, 0, 0)))
        parsed = datetime.fromisoformat(event["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_timestamp_aware_datetime_converted_to_exact_utc_instant(self) -> None:
        event = _to_v1_event(
            _legacy_msg(
                timestamp=datetime(
                    2026,
                    6,
                    27,
                    17,
                    45,
                    tzinfo=timezone(timedelta(hours=5, minutes=45)),
                )
            )
        )
        self.assertEqual(event["timestamp"], "2026-06-27T12:00:00+00:00")

    def test_timestamp_parseable_string_converted_to_exact_utc_instant(self) -> None:
        event = _to_v1_event(_legacy_msg(timestamp="2026-06-27T17:45:00+05:45"))
        self.assertEqual(event["timestamp"], "2026-06-27T12:00:00+00:00")

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

    def test_created_at_passthrough_used_verbatim(self) -> None:
        # _send_v1_batch hoists created_at and passes it in so it stays stable
        # across retry attempts.
        body = _build_v1_batch_body([], created_at="2026-06-27T12:00:00+00:00")
        self.assertEqual(body["created_at"], "2026-06-27T12:00:00+00:00")

    def test_historical_migration_omitted_when_false(self) -> None:
        self.assertNotIn("historical_migration", _build_v1_batch_body([]))

    def test_historical_migration_present_when_true(self) -> None:
        body = _build_v1_batch_body([], historical_migration=True)
        self.assertIs(body["historical_migration"], True)
