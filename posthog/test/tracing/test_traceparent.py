import pytest

from posthog.tracing._traceparent import (
    RemoteSpanContext,
    format_traceparent,
    normalize_traceparent,
    parse_traceparent,
    sanitize_tracestate,
    traceparent_header,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


class TestParseTraceparent:
    def test_parses_a_sampled_header(self):
        assert parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-01") == RemoteSpanContext(
            TRACE_ID, SPAN_ID, "01"
        )

    def test_continues_a_sampled_out_trace_and_keeps_the_flag(self):
        assert parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-00") == RemoteSpanContext(
            TRACE_ID, SPAN_ID, "00"
        )

    @pytest.mark.parametrize(
        "inbound,expected", [("05", "01"), ("04", "00"), ("ff", "01")]
    )
    def test_zeroes_flags_version_00_does_not_define(self, inbound, expected):
        parsed = parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-{inbound}")
        assert parsed is not None
        assert parsed.flags == expected

    def test_accepts_a_future_version_with_extra_fields(self):
        parsed = parse_traceparent(f"01-{TRACE_ID}-{SPAN_ID}-01-extra")
        assert parsed == RemoteSpanContext(TRACE_ID, SPAN_ID, "01")

    def test_ignores_surrounding_whitespace(self):
        parsed = parse_traceparent(f"  00-{TRACE_ID}-{SPAN_ID}-01 ")
        assert parsed == RemoteSpanContext(TRACE_ID, SPAN_ID, "01")

    def test_decodes_a_raw_asgi_header_value(self):
        parsed = parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-01".encode("ascii"))
        assert parsed == RemoteSpanContext(TRACE_ID, SPAN_ID, "01")

    @pytest.mark.parametrize(
        "value",
        [
            f"00-{TRACE_ID.upper()}-{SPAN_ID}-01",
            f"00-{TRACE_ID}-{SPAN_ID.upper()}-01",
            f"00-{TRACE_ID}-{SPAN_ID}-0A",
        ],
    )
    def test_rejects_uppercase_rather_than_folding_it(self, value):
        assert parse_traceparent(value) is None

    def test_rejects_a_version_00_header_with_trailing_fields(self):
        assert parse_traceparent(f"00-{TRACE_ID}-{SPAN_ID}-01-extra") is None

    @pytest.mark.parametrize(
        "value",
        [
            "garbage",
            "",
            f"ff-{TRACE_ID}-{SPAN_ID}-01",
            f"00-{'0' * 32}-{SPAN_ID}-01",
            f"00-{TRACE_ID}-{'0' * 16}-01",
            f"00-{TRACE_ID[:30]}-{SPAN_ID}-01",
            f"00-{TRACE_ID}-{SPAN_ID}",
            42,
            None,
            [f"00-{TRACE_ID}-{SPAN_ID}-01"],
            "00-caf\u00e9".encode("utf-8"),
        ],
    )
    def test_returns_none_for_malformed_input(self, value):
        assert parse_traceparent(value) is None


class TestFormatTraceparent:
    def test_sets_the_sampled_flag_on_a_trace_started_here(self):
        assert format_traceparent(TRACE_ID, SPAN_ID) == f"00-{TRACE_ID}-{SPAN_ID}-01"

    def test_propagates_the_flags_byte_it_was_given(self):
        assert (
            format_traceparent(TRACE_ID, SPAN_ID, "00") == f"00-{TRACE_ID}-{SPAN_ID}-00"
        )

    def test_round_trips_through_the_parser(self):
        assert parse_traceparent(format_traceparent(TRACE_ID, SPAN_ID, "00")) == (
            RemoteSpanContext(TRACE_ID, SPAN_ID, "00")
        )


class TestNormalizeTraceparent:
    def test_carries_version_and_flags_through_as_received(self):
        assert (
            normalize_traceparent(f"01-{TRACE_ID}-{SPAN_ID}-05")
            == f"01-{TRACE_ID}-{SPAN_ID}-05"
        )

    def test_decodes_a_raw_asgi_header_value(self):
        header = f"00-{TRACE_ID}-{SPAN_ID}-01"
        assert normalize_traceparent(header.encode("ascii")) == header

    def test_echoes_a_higher_version_whole_including_its_trailing_fields(self):
        assert (
            normalize_traceparent(f" 01-{TRACE_ID}-{SPAN_ID}-01-what ")
            == f"01-{TRACE_ID}-{SPAN_ID}-01-what"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "garbage",
            f"ff-{TRACE_ID}-{SPAN_ID}-01",
            f"00-{'0' * 32}-{SPAN_ID}-01",
            f"00-{TRACE_ID.upper()}-{SPAN_ID}-01",
            f"00-{TRACE_ID}-{SPAN_ID}-01-extra",
            f"01-{TRACE_ID}-{SPAN_ID}-01-x\rX-Injected: yes",
            f"01-{TRACE_ID}-{SPAN_ID}-01-caf\u00e9",
            f"01-{TRACE_ID}-{SPAN_ID}-01-" + "x" * 512,
            ["a", "b"],
        ],
    )
    def test_rejects_malformed_input(self, value):
        assert normalize_traceparent(value) is None


class TestTraceparentHeader:
    def test_unwraps_a_one_element_list(self):
        header = f"00-{TRACE_ID}-{SPAN_ID}-01"
        assert traceparent_header([header]) == header
        assert traceparent_header((header,)) == header

    def test_leaves_two_values_for_the_parser_to_reject(self):
        values = [f"00-{TRACE_ID}-{SPAN_ID}-01", f"00-{TRACE_ID}-{SPAN_ID}-00"]
        assert traceparent_header(values) == values
        assert parse_traceparent(traceparent_header(values)) is None

    def test_passes_a_string_through(self):
        assert traceparent_header("x") == "x"

    def test_decodes_a_raw_asgi_header_value_even_inside_a_list(self):
        assert traceparent_header(b"x") == "x"
        assert traceparent_header([b"x"]) == "x"

    def test_leaves_undecodable_bytes_for_the_parser_to_reject(self):
        assert traceparent_header(b"caf\xc3\xa9") == b"caf\xc3\xa9"
        assert parse_traceparent(traceparent_header(b"caf\xc3\xa9")) is None


class TestSanitizeTracestate:
    def test_preserves_a_valid_vendor_list_unchanged(self):
        assert sanitize_tracestate("vendor=abc,other=def") == "vendor=abc,other=def"

    def test_decodes_a_raw_asgi_header_value(self):
        assert sanitize_tracestate(b"vendor=abc") == "vendor=abc"

    def test_trims_surrounding_whitespace(self):
        assert sanitize_tracestate("  vendor=abc ") == "vendor=abc"

    def test_keeps_a_tab_separated_vendor_list(self):
        assert sanitize_tracestate("vendor=abc,\tother=def") == "vendor=abc,\tother=def"

    def test_tolerates_empty_members(self):
        assert sanitize_tracestate("vendor=abc,,other=def") == "vendor=abc,,other=def"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "novalue",
            "vendor=abc,novalue",
            42,
            None,
            ",".join(f"k{i}=v" for i in range(33)),
            # One member, over 512 characters: nothing survives the trim.
            "k=" + "v" * 600,
            "vendor=abc\r\nother=def",
            "vendor=\ud800",
            "vendor=é",
        ],
    )
    def test_discards_invalid_values(self, value):
        assert sanitize_tracestate(value) is None

    def test_trims_an_over_long_value_by_dropping_large_members_first(self):
        # The spec's scenario: 600 characters, one member of 150. Dropping that
        # member leaves 449 characters, kept unchanged.
        large = "big=" + "x" * 146
        small = ["k{:02d}=".format(i) + "v" * 13 for i in range(25)]
        value = ",".join(small[:10] + [large] + small[10:])
        assert len(large) == 150 and len(value) == 600
        trimmed = sanitize_tracestate(value)
        assert trimmed == ",".join(small)
        assert len(trimmed) == 449

    def test_trims_from_the_right_once_no_large_member_is_left(self):
        members = ["k{:02d}=".format(i) + "v" * 60 for i in range(10)]
        # Ten 64-character members; seven fit in 512.
        assert sanitize_tracestate(",".join(members)) == ",".join(members[:7])
