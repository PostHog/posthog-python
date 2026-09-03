import re
from unittest import mock

import pytest

from posthog.tracing import _ids
from posthog.tracing._ids import (
    is_valid_span_id,
    is_valid_trace_id,
    new_span_id,
    new_trace_id,
)

LOWER_HEX = re.compile(r"^[0-9a-f]+$")


class TestNewTraceId:
    def test_is_32_lowercase_hex_characters(self):
        trace_id = new_trace_id()
        assert len(trace_id) == 32
        assert LOWER_HEX.match(trace_id)

    def test_is_never_all_zeros(self):
        with mock.patch.object(
            _ids.secrets, "token_hex", side_effect=["0" * 32, "0" * 32, "ab" * 16]
        ):
            assert new_trace_id() == "ab" * 16

    def test_does_not_repeat(self):
        assert len({new_trace_id() for _ in range(1000)}) == 1000


class TestNewSpanId:
    def test_is_16_lowercase_hex_characters(self):
        span_id = new_span_id()
        assert len(span_id) == 16
        assert LOWER_HEX.match(span_id)

    def test_is_never_all_zeros(self):
        with mock.patch.object(
            _ids.secrets, "token_hex", side_effect=["0" * 16, "cd" * 8]
        ):
            assert new_span_id() == "cd" * 8

    def test_does_not_repeat(self):
        assert len({new_span_id() for _ in range(1000)}) == 1000


class TestValidation:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("4bf92f3577b34da6a3ce929d0e0e4736", True),
            ("0" * 32, False),
            ("abc", False),
            ("4BF92F3577B34DA6A3CE929D0E0E4736", False),
            ("zz" * 16, False),
            (12345, False),
            (None, False),
        ],
    )
    def test_is_valid_trace_id(self, value, expected):
        assert is_valid_trace_id(value) is expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("00f067aa0ba902b7", True),
            ("0" * 16, False),
            ("4bf92f3577b34da6a3ce929d0e0e4736", False),
        ],
    )
    def test_is_valid_span_id(self, value, expected):
        assert is_valid_span_id(value) is expected
