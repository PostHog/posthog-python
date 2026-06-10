"""Opt-in Ed25519 signing of ``$exception`` events.

When a backend service configures an Ed25519 private key, the SDK signs every captured
``$exception`` event over a canonical projection of its ``$exception_list`` and attaches the
signature as event properties. PostHog's error-tracking ingestion (cymbal) re-derives the same
projection, verifies it against the project's registered *public* key, and stamps a trusted
``$exception_verified`` flag — proving the exception genuinely came from your backend rather
than being forged through the public ingest key.

The canonical projection is a deliberately small, byte-stable subset of each exception
(type, message, and each frame's function/filename/lineno/module). It excludes everything
cymbal mutates during ingestion (in-app flags, absolute paths, source context, the injected
exception id) and anything float-valued, so the bytes the SDK signs match the bytes cymbal
verifies. The encoding is explicit length-prefixed binary rather than JSON, to avoid
cross-language canonicalisation pitfalls (key order, non-ASCII escaping, number formatting).

Requires the optional ``cryptography`` dependency: ``pip install posthoganalytics[exception-signing]``.
"""

import base64
import hashlib
import struct
from typing import Any, Optional

CANONICAL_MAGIC = b"PHEXC1\n"

SIGNATURE_PROPERTY = "$exception_signature"
KEY_ID_PROPERTY = "$exception_signature_key_id"
VERSION_PROPERTY = "$exception_signature_version"
SIGNATURE_VERSION = 1


def _lp(value: Any) -> bytes:
    """Length-prefixed UTF-8 encoding: u32 big-endian length + bytes. None/missing -> empty."""
    if value is None:
        data = b""
    else:
        data = str(value).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def build_canonical(exception_list: Any) -> bytes:
    """Deterministic, length-prefixed encoding of the signable projection of ``$exception_list``.

    Both the SDK (here) and cymbal must produce identical bytes for the same input, so this
    reads only stable string/int fields and never floats.
    """
    out = bytearray(CANONICAL_MAGIC)
    exceptions = exception_list if isinstance(exception_list, list) else []
    out += struct.pack(">I", len(exceptions))
    for exc in exceptions:
        exc = exc if isinstance(exc, dict) else {}
        out += _lp(exc.get("type"))
        out += _lp(exc.get("value"))
        stacktrace = exc.get("stacktrace")
        frames = stacktrace.get("frames") if isinstance(stacktrace, dict) else None
        frames = frames if isinstance(frames, list) else []
        out += struct.pack(">I", len(frames))
        for frame in frames:
            frame = frame if isinstance(frame, dict) else {}
            out += _lp(frame.get("function"))
            out += _lp(frame.get("filename"))
            lineno = frame.get("lineno")
            out += _lp(lineno if lineno is None else str(lineno))
            out += _lp(frame.get("module"))
    return bytes(out)


def derive_key_id(public_key_raw: bytes) -> str:
    """Stable short fingerprint of a raw 32-byte Ed25519 public key.

    Computed identically by the SDK (from the configured private key) and by PostHog (from the
    registered public key), so a signature's key id resolves to the right stored key.
    """
    digest = hashlib.sha256(public_key_raw).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:16]


class ExceptionSigner:
    """Holds a parsed Ed25519 private key and signs ``$exception`` events.

    Constructed once at client init from a PEM private key. Raises a clear error if the optional
    ``cryptography`` dependency is missing or the key isn't Ed25519.
    """

    def __init__(self, private_key_pem: str):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError as e:  # pragma: no cover - exercised via install extras
            raise ImportError(
                "Exception signing requires the optional 'cryptography' dependency. "
                "Install it with: pip install posthoganalytics[exception-signing]"
            ) from e

        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("exception_signing_private_key must be an Ed25519 private key (PEM)")

        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        self._key = key
        public_raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = derive_key_id(public_raw)

    def sign(self, canonical: bytes) -> str:
        return base64.b64encode(self._key.sign(canonical)).decode("ascii")

    def sign_event(self, event: dict) -> dict:
        """Attach signature properties to a ``$exception`` event in place; returns it.

        Non-exception events pass through untouched.
        """
        if event.get("event") != "$exception":
            return event
        properties = event.get("properties")
        if not isinstance(properties, dict):
            return event
        canonical = build_canonical(properties.get("$exception_list"))
        properties[SIGNATURE_PROPERTY] = self.sign(canonical)
        properties[KEY_ID_PROPERTY] = self.key_id
        properties[VERSION_PROPERTY] = SIGNATURE_VERSION
        return event


def make_signer(private_key_pem: Optional[str]) -> Optional[ExceptionSigner]:
    """Build a signer from a PEM key, or None when no key is configured."""
    if not private_key_pem:
        return None
    return ExceptionSigner(private_key_pem)
