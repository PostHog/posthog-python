import unittest
from datetime import datetime, timezone

from parameterized import parameterized

from posthog.capture_v1 import (
    _build_v1_batch_body,
    _to_v1_event,
    _coerce_bool,
    _coerce_str,
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
