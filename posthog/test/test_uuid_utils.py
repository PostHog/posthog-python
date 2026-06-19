from uuid import UUID

import pytest

from posthog import _uuid as uuid_utils


@pytest.mark.parametrize(
    ("stdlib_uuid7", "expected"),
    [
        (
            UUID("01920000-0000-7000-8000-000000000001"),
            "01920000-0000-7000-8000-000000000001",
        ),
        (None, "01234567-89ab-7fff-bfff-ffffffffffff"),
    ],
)
def test_uuid7_generates_version_7_uuid_string(monkeypatch, stdlib_uuid7, expected):
    if stdlib_uuid7 is None:
        monkeypatch.delattr(uuid_utils.uuid, "uuid7", raising=False)
        monkeypatch.setattr(uuid_utils.time, "time", lambda: 0x0123456789AB / 1000)
        monkeypatch.setattr(uuid_utils.os, "urandom", lambda length: b"\xff" * length)
    else:
        monkeypatch.setattr(
            uuid_utils.uuid, "uuid7", lambda: stdlib_uuid7, raising=False
        )

    generated = uuid_utils.uuid7()
    parsed = UUID(generated)

    assert generated == expected
    assert parsed.version == 7
    assert str(parsed) == generated
