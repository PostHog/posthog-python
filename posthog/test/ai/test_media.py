import base64
import json
from dataclasses import dataclass

from posthog.ai.media import (
    bytes_to_base64,
    ensure_serializable,
    normalize_part_keys,
    to_plain,
)
from posthog.ai.utils import finalize_ai_content


def test_to_plain_pydantic_strips_none():
    from google.genai import types

    part = types.Part(text="hi")
    plain = to_plain(part)
    assert plain == {"text": "hi"}


def test_to_plain_dict_strips_none():
    assert to_plain(
        {"text": None, "inline_data": {"mime_type": "image/png", "data": "AA"}}
    ) == {"inline_data": {"mime_type": "image/png", "data": "AA"}}


def test_to_plain_dataclass():
    @dataclass
    class Block:
        text: str

    assert to_plain(Block(text="x")) == {"text": "x"}


def test_to_plain_passthrough():
    assert to_plain("str") == "str"
    assert to_plain(3) == 3


def test_bytes_to_base64():
    assert bytes_to_base64(b"\x00\x01") == base64.b64encode(b"\x00\x01").decode()


def test_normalize_part_keys_camel_to_snake():
    out = normalize_part_keys({"inlineData": {"mimeType": "image/png", "data": "AA"}})
    assert out == {"inline_data": {"mime_type": "image/png", "data": "AA"}}


def test_normalize_part_keys_file_data():
    out = normalize_part_keys({"fileData": {"fileUri": "u", "mimeType": "video/mp4"}})
    assert out == {"file_data": {"file_uri": "u", "mime_type": "video/mp4"}}


def test_normalize_part_keys_untouched_snake():
    d = {"inline_data": {"mime_type": "image/png", "data": "AA"}}
    assert normalize_part_keys(d) == d


def test_ensure_serializable_bytes_leaf_preserves_structure_for_redaction():
    # The claude-agent-sdk tool-span path is finalize_ai_content(ensure_serializable(...)).
    # A single bytes leaf must not collapse the whole dict to a repr string, or
    # the base64 under a strong key escapes finalize's redaction.
    long_b64 = base64.b64encode(b"\xff" * 300).decode()
    out = finalize_ai_content(
        ensure_serializable({"data": long_b64, "blob": b"\x00\x01"})
    )
    assert isinstance(out, dict)
    assert long_b64 not in repr(out)
    assert out["data"] == "[base64 image redacted]"


def test_ensure_serializable_coerces_non_string_keys():
    out = ensure_serializable({1: "x"})
    assert out == {"1": "x"}
    json.dumps(out)


def test_ensure_serializable_coerces_tuple_keys():
    json.dumps(ensure_serializable({("a", "b"): "x"}))
