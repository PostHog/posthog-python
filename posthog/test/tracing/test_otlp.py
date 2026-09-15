import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from posthog.tracing import _otlp
from posthog.tracing._otlp import (
    CIRCULAR_VALUE,
    MAX_VALUE_ITEMS,
    MAX_VALUE_NODES,
    TRUNCATED_VALUE,
    SpanEventRecord,
    SpanRecord,
    SpanStatus,
    build_otlp_span,
    build_resource_attributes,
    build_traces_payload,
    host_resource_attributes,
    span_kind_to_otlp,
    to_any_value,
    to_key_value_list,
)
from posthog.tracing._sanitize import UNSERIALIZABLE_VALUE
from posthog.version import VERSION

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
START_NS = 1_700_000_000_000_000_000
SNAPSHOT_DIRECTORY = Path(__file__).parents[1] / "snapshots"
END_NS = START_NS + 80_000_000


def record(**overrides) -> SpanRecord:
    base = dict(
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        name="checkout",
        start_ns=START_NS,
        end_ns=END_NS,
    )
    base.update(overrides)
    return SpanRecord(**base)


class TestToAnyValue:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, {"boolValue": True}),
            (False, {"boolValue": False}),
            (42, {"intValue": "42"}),
            (-7, {"intValue": "-7"}),
            (2**63 - 1, {"intValue": str(2**63 - 1)}),
            (0.25, {"doubleValue": 0.25}),
            (2.0, {"doubleValue": 2.0}),
            (float("nan"), {"stringValue": "NaN"}),
            (float("inf"), {"stringValue": "Infinity"}),
            (float("-inf"), {"stringValue": "-Infinity"}),
            ("hello", {"stringValue": "hello"}),
            (b"\x00\x01", {"stringValue": "b'\\x00\\x01'"}),
            (
                datetime(2023, 1, 1, tzinfo=timezone.utc),
                {"stringValue": "2023-01-01T00:00:00+00:00"},
            ),
        ],
    )
    def test_encodes_primitives(self, value, expected):
        assert to_any_value(value) == expected

    def test_encodes_an_integer_beyond_int64_as_a_string(self):
        assert to_any_value(2**64) == {"stringValue": str(2**64)}
        assert to_any_value(-(2**63) - 1) == {"stringValue": str(-(2**63) - 1)}

    def test_encodes_arrays_and_drops_none_elements(self):
        assert to_any_value([1, None, "a"]) == {
            "arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "a"}]}
        }

    def test_encodes_mappings_as_kvlist(self):
        assert to_any_value({"a": 1, "b": None}) == {
            "kvlistValue": {"values": [{"key": "a", "value": {"intValue": "1"}}]}
        }

    def test_replaces_a_lone_surrogate_with_the_replacement_character(self):
        assert to_any_value("value \ud83d") == {"stringValue": "value \ufffd"}
        assert to_any_value("\udc00x") == {"stringValue": "\ufffdx"}

    def test_keeps_a_surrogate_pair_as_the_character_it_spells(self):
        assert to_any_value("a" + "\ud83d" + "\ude00") == {"stringValue": "a\U0001f600"}

    def test_encodes_an_int_subclass_as_its_number(self):
        import enum

        class Code(enum.IntEnum):
            OK = 200

        class Weird(int):
            def __str__(self):
                return "weird"

        assert to_any_value(Code.OK) == {"intValue": "200"}
        assert to_any_value(Weird(3)) == {"intValue": "3"}

    def test_falls_back_to_str_for_unknown_types(self):
        class Thing:
            def __str__(self):
                return "thing"

        assert to_any_value(Thing()) == {"stringValue": "thing"}

    def test_encodes_a_callable_as_a_stable_marker(self):
        assert to_any_value(lambda: None) == {"stringValue": "[Function]"}

    def test_survives_a_hostile_str(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        assert to_any_value(Hostile()) == {"stringValue": UNSERIALIZABLE_VALUE}

    def test_marks_a_cycle_instead_of_recursing(self):
        loop: dict = {}
        loop["self"] = loop
        assert to_any_value(loop) == {
            "kvlistValue": {
                "values": [{"key": "self", "value": {"stringValue": CIRCULAR_VALUE}}]
            }
        }

    def test_treats_a_repeated_sibling_as_duplication_not_a_cycle(self):
        shared = {"x": 1}
        encoded = to_any_value([shared, shared])
        assert (
            encoded["arrayValue"]["values"]
            == [{"kvlistValue": {"values": [{"key": "x", "value": {"intValue": "1"}}]}}]
            * 2
        )

    def test_truncates_a_deep_value(self):
        value: list = []
        current = value
        for _ in range(30):
            nested: list = []
            current.append(nested)
            current = nested
        encoded = to_any_value(value)
        while "arrayValue" in encoded:
            encoded = encoded["arrayValue"]["values"][0]
        assert encoded == {"stringValue": TRUNCATED_VALUE}

    def test_truncates_a_long_array(self):
        encoded = to_any_value(list(range(MAX_VALUE_ITEMS + 5)))
        values = encoded["arrayValue"]["values"]
        assert len(values) == MAX_VALUE_ITEMS + 1
        assert values[-1] == {"stringValue": TRUNCATED_VALUE}

    def test_bounds_total_nodes(self):
        encoded = json.dumps(to_any_value([[1] * 200 for _ in range(200)]))
        assert TRUNCATED_VALUE in encoded
        assert 0 < encoded.count('"intValue"') < MAX_VALUE_NODES


class TestToKeyValueList:
    def test_drops_none_values_and_empty_keys(self):
        assert to_key_value_list({"": 1, "a": None, "b": 2}) == [
            {"key": "b", "value": {"intValue": "2"}}
        ]

    def test_stringifies_non_string_keys(self):
        assert to_key_value_list({1: "x"}) == [
            {"key": "1", "value": {"stringValue": "x"}}
        ]

    def test_marks_only_the_raising_key(self):
        class Explosive(dict):
            def __getitem__(self, key):
                if key == "bad":
                    raise RuntimeError("boom")
                return super().__getitem__(key)

        assert to_key_value_list(Explosive(good=1, bad=2)) == [
            {"key": "good", "value": {"intValue": "1"}},
            {"key": "bad", "value": {"stringValue": UNSERIALIZABLE_VALUE}},
        ]

    def test_returns_empty_for_a_non_mapping(self):
        assert to_key_value_list(["a"]) == []
        assert to_key_value_list(None) == []


class TestSpanKindToOtlp:
    @pytest.mark.parametrize(
        "kind,code",
        [
            ("internal", 1),
            ("server", 2),
            ("client", 3),
            ("producer", 4),
            ("consumer", 5),
        ],
    )
    def test_maps_each_kind(self, kind, code):
        assert span_kind_to_otlp(kind) == code

    @pytest.mark.parametrize("kind", [None, "", "weird", 3, "__class__"])
    def test_defaults_to_internal(self, kind):
        assert span_kind_to_otlp(kind) == 1


class TestBuildOtlpSpan:
    def test_builds_the_minimal_shape(self):
        assert build_otlp_span(record()) == {
            "traceId": TRACE_ID,
            "spanId": SPAN_ID,
            "name": "checkout",
            "kind": 1,
            "startTimeUnixNano": str(START_NS),
            "endTimeUnixNano": str(END_NS),
            "flags": 0x101,
        }

    def test_omits_status_when_never_set(self):
        assert "status" not in build_otlp_span(record())

    def test_encodes_ok_and_error_status_codes(self):
        assert build_otlp_span(record(status=SpanStatus("ok")))["status"] == {"code": 1}
        assert build_otlp_span(record(status=SpanStatus("error", "boom")))[
            "status"
        ] == {
            "code": 2,
            "message": "boom",
        }

    def test_ignores_an_unknown_status_code(self):
        assert "status" not in build_otlp_span(record(status=SpanStatus("weird")))

    def test_includes_parent_tracestate_attributes_and_events(self):
        span = build_otlp_span(
            record(
                parent_span_id="b7ad6b7169203331",
                trace_state="vendor=abc",
                attributes={"k": "v"},
                events=[
                    SpanEventRecord("cache miss", START_NS + 10, {"key": "user:1"})
                ],
            )
        )
        assert span["parentSpanId"] == "b7ad6b7169203331"
        assert span["traceState"] == "vendor=abc"
        assert span["attributes"] == [{"key": "k", "value": {"stringValue": "v"}}]
        assert span["events"] == [
            {
                "name": "cache miss",
                "timeUnixNano": str(START_NS + 10),
                "attributes": [{"key": "key", "value": {"stringValue": "user:1"}}],
            }
        ]

    def test_omits_event_attributes_when_empty(self):
        span = build_otlp_span(record(events=[SpanEventRecord("tick", START_NS, {})]))
        assert span["events"] == [{"name": "tick", "timeUnixNano": str(START_NS)}]

    def test_marks_a_root_span_as_known_not_remote(self):
        assert build_otlp_span(record())["flags"] == 0x101

    def test_marks_a_header_parent_as_remote(self):
        assert build_otlp_span(record(parent_is_remote=True))["flags"] == 0x301

    def test_omits_dropped_counts_when_nothing_was_dropped(self):
        span = build_otlp_span(record(events=[SpanEventRecord("e", START_NS)]))
        assert "droppedAttributesCount" not in span
        assert "droppedEventsCount" not in span
        assert "droppedAttributesCount" not in span["events"][0]

    def test_emits_dropped_counts_on_the_span_and_its_events(self):
        span = build_otlp_span(
            record(
                dropped_attributes_count=2,
                dropped_events_count=3,
                events=[SpanEventRecord("e", START_NS, {"k": 1}, 4)],
            )
        )
        assert span["droppedAttributesCount"] == 2
        assert span["droppedEventsCount"] == 3
        assert span["events"][0]["droppedAttributesCount"] == 4

    @pytest.mark.parametrize(
        "value,expected",
        [
            (2**40, 0xFFFFFFFF),
            (-1, 0),
            (1.9, 1),
            ("3", 0),
            (True, 0),
            (float("inf"), 0),
        ],
    )
    def test_clamps_a_dropped_count_to_uint32(self, value, expected):
        span = build_otlp_span(record(dropped_attributes_count=value))
        assert span.get("droppedAttributesCount", 0) == expected

    def test_propagates_an_inbound_sampled_out_flag(self):
        assert build_otlp_span(record(trace_flags="00"))["flags"] == 0x100

    def test_falls_back_to_sampled_when_the_flags_byte_is_unusable(self):
        assert build_otlp_span(record(trace_flags="zz"))["flags"] == 0x101

    def test_replaces_lone_surrogates_in_every_free_text_field(self):
        lone = "value \ud83d"
        span = build_otlp_span(
            record(
                name=lone,
                trace_state=f"vendor={lone}",
                status=SpanStatus("error", lone),
                events=[SpanEventRecord(lone, START_NS)],
            )
        )
        for text in (
            span["name"],
            span["traceState"],
            span["status"]["message"],
            span["events"][0]["name"],
        ):
            assert "\ud83d" not in text
            assert "\ufffd" in text

    def test_replaces_lone_surrogates_in_attribute_keys(self):
        span = build_otlp_span(record(attributes={"k\ud800": "v"}))
        assert span["attributes"][0]["key"] == "k\ufffd"

    def test_drops_only_an_attribute_whose_key_cannot_be_stringified(self):
        class HostileKey:
            def __str__(self):
                raise RuntimeError("no")

            def __hash__(self):
                return 1

        assert to_key_value_list({"a": {HostileKey(): 1, "b": 2}}) == [
            {
                "key": "a",
                "value": {
                    "kvlistValue": {
                        "values": [{"key": "b", "value": {"intValue": "2"}}]
                    }
                },
            }
        ]

    def test_keeps_the_span_when_a_status_message_cannot_be_stringified(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        span = build_otlp_span(record(status=SpanStatus("error", Hostile())))
        assert span["status"] == {"code": 2, "message": UNSERIALIZABLE_VALUE}


class TestResourceAttributes:
    def test_always_emits_service_name(self):
        attrs = build_resource_attributes(None, None, None, {})
        assert attrs["service.name"] == "unknown_service"
        assert attrs["telemetry.sdk.name"] == "posthog-python"
        assert attrs["telemetry.sdk.version"] == VERSION

    def test_uses_the_configured_service_name_and_optional_keys(self):
        attrs = build_resource_attributes("api", "1.2.3", "prod", {})
        assert attrs["service.name"] == "api"
        assert attrs["service.version"] == "1.2.3"
        assert attrs["deployment.environment"] == "prod"

    def test_omits_environment_and_version_when_unset(self):
        attrs = build_resource_attributes("api", None, None, {})
        assert "service.version" not in attrs
        assert "deployment.environment" not in attrs

    def test_protects_sdk_identity_keys_from_user_attributes(self):
        attrs = build_resource_attributes(
            "api", None, None, {"telemetry.sdk.name": "custom", "region": "eu"}
        )
        assert attrs["telemetry.sdk.name"] == "posthog-python"
        assert attrs["region"] == "eu"

    def test_host_attributes_map_darwin_to_macos(self):
        with (
            mock.patch.object(_otlp.platform, "system", return_value="Darwin"),
            mock.patch.object(_otlp.platform, "release", return_value="23.1.0"),
        ):
            assert host_resource_attributes() == {
                "os.name": "macOS",
                "os.version": "23.1.0",
            }

    def test_host_attributes_pass_unknown_names_through_and_omit_empty(self):
        with (
            mock.patch.object(_otlp.platform, "system", return_value="Linux"),
            mock.patch.object(_otlp.platform, "release", return_value=""),
        ):
            assert host_resource_attributes() == {"os.name": "Linux"}

    def test_host_attributes_report_the_windows_build_like_node(self):
        with (
            mock.patch.object(_otlp.platform, "system", return_value="Windows"),
            mock.patch.object(_otlp.platform, "release", return_value="11"),
            mock.patch.object(_otlp.platform, "version", return_value="10.0.22631"),
        ):
            assert host_resource_attributes() == {
                "os.name": "Windows",
                "os.version": "10.0.22631",
            }

    @pytest.mark.parametrize(
        "system", ["CYGWIN_NT-10.0-19045", "MSYS_NT-10.0-19045", "MINGW64_NT-10.0"]
    )
    def test_host_attributes_file_posix_layers_over_windows_under_windows(self, system):
        with (
            mock.patch.object(_otlp.platform, "system", return_value=system),
            mock.patch.object(_otlp.platform, "release", return_value="3.5.4"),
        ):
            assert host_resource_attributes() == {
                "os.name": "Windows",
                "os.version": "3.5.4",
            }

    def test_an_oversized_user_attribute_does_not_cost_the_identity_keys(self):
        # One user value large enough to exhaust the encoder's traversal budget.
        attrs = build_resource_attributes(
            "api", "1.2.3", "prod", {"huge": [list(range(1000))] * 20, "os.name": "x"}
        )
        payload = build_traces_payload([], attrs)
        keys = [
            kv["key"] for kv in payload["resourceSpans"][0]["resource"]["attributes"]
        ]
        # User keys share one budget, so "os.name" after "huge" is lost with it;
        # the SDK's own keys are encoded on a budget of their own.
        assert keys == [
            "huge",
            "service.name",
            "deployment.environment",
            "service.version",
            "telemetry.sdk.name",
            "telemetry.sdk.version",
        ]

    def test_host_attributes_survive_a_failing_platform_module(self):
        with mock.patch.object(_otlp.platform, "system", side_effect=OSError("no")):
            assert host_resource_attributes() == {}


class TestBuildTracesPayload:
    def test_produces_one_resource_one_scope_n_spans(self):
        spans = [build_otlp_span(record()) for _ in range(20)]
        payload = build_traces_payload(spans, {"service.name": "api"})
        assert len(payload["resourceSpans"]) == 1
        assert len(payload["resourceSpans"][0]["scopeSpans"]) == 1
        assert len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 20

    def test_matches_the_shape_the_ingestion_service_accepts(self):
        # Golden fixture ported from posthog-js.
        payload = build_traces_payload(
            [
                build_otlp_span(
                    record(
                        parent_span_id="b7ad6b7169203331",
                        name="GET /users/:id",
                        kind="server",
                        status=SpanStatus("error", "boom"),
                        attributes={
                            "posthogDistinctId": "user-123",
                            "sessionId": "session-123",
                            "http.status_code": 500,
                            "http.duration_ratio": 0.25,
                            "cached": False,
                        },
                        events=[
                            SpanEventRecord(
                                "exception",
                                START_NS + 40_000_000,
                                {
                                    "exception.type": "TypeError",
                                    "exception.message": "boom",
                                },
                            )
                        ],
                    )
                )
            ],
            {"service.name": "checkout-api", "telemetry.sdk.name": "posthog-python"},
        )

        scope = payload["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["version"] == VERSION
        scope["version"] = "<SDK_VERSION>"
        actual = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        assert actual == (SNAPSHOT_DIRECTORY / "otlp_traces_payload.json").read_text()
