from datetime import datetime, timedelta, timezone

from posthog.exception_utils import format_timestamp


def test_format_timestamp_converts_aware_value_to_utc():
    value = datetime(
        2026,
        6,
        27,
        17,
        45,
        0,
        123456,
        tzinfo=timezone(timedelta(hours=5, minutes=45)),
    )

    assert format_timestamp(value) == "2026-06-27T12:00:00.123456Z"
