"""UUID generation helpers."""

import os
import time
import uuid


_UUID7_RANDOM_BITS = 74
_UUID7_RANDOM_MASK = (1 << _UUID7_RANDOM_BITS) - 1
_UUID7_TIMESTAMP_MASK = (1 << 48) - 1


def uuid7() -> str:
    """Return a UUID v7 string.

    Python 3.14+ includes ``uuid.uuid7`` in the standard library. Older
    supported runtimes do not, so fall back to a small RFC 9562-compatible
    implementation using the current Unix epoch milliseconds and 74 random bits.
    """

    stdlib_uuid7 = getattr(uuid, "uuid7", None)
    if stdlib_uuid7 is not None:
        return str(stdlib_uuid7())

    unix_ts_ms = int(time.time() * 1000) & _UUID7_TIMESTAMP_MASK
    random_bits = int.from_bytes(os.urandom(10), "big") & _UUID7_RANDOM_MASK
    rand_a = random_bits >> 62
    rand_b = random_bits & ((1 << 62) - 1)

    uuid_int = (unix_ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=uuid_int))
