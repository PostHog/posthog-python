from uuid import UUID

from posthog._uuid import uuid7


def test_uuid7_generates_version_7_uuid_string():
    generated = uuid7()

    parsed = UUID(generated)

    assert parsed.version == 7
    assert str(parsed) == generated
