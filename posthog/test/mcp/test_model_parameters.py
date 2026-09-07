from types import SimpleNamespace

import pytest

from posthog.mcp._model_parameters import (
    add_model_parameter_to_schema,
    request_meta_from_context,
    resolve_model,
)
from posthog.mcp.constants import DEFAULT_MODEL_PARAMETER_DESCRIPTION


@pytest.mark.parametrize(
    "schema",
    [
        None,
        {},
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ],
)
def test_add_model_parameter_to_schema(schema):
    result = add_model_parameter_to_schema(schema, "search")

    assert result["properties"]["llm_model"] == {
        "type": "string",
        "description": DEFAULT_MODEL_PARAMETER_DESCRIPTION,
    }
    assert "llm_model" in result["required"]
    assert result.get("additionalProperties") is not False


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/$defs/Input"},
        {"oneOf": [{"type": "object", "properties": {}}]},
        {
            "type": "object",
            "properties": {"llm_model": {"type": "number"}},
        },
    ],
)
def test_add_model_parameter_to_schema_preserves_unsafe_or_owned_schema(schema):
    assert add_model_parameter_to_schema(schema, "search") is schema


@pytest.mark.parametrize(
    ("metadata", "argument", "allow_self_reported", "expected"),
    [
        (
            {"x-codex-turn-metadata": {"model": "  gpt-5.6-sol  "}},
            "claude-opus-4-8",
            True,
            ("gpt-5.6-sol", "client_metadata"),
        ),
        (
            {"x-codex-turn-metadata": {"model": "unknown"}},
            "claude-opus-4-8",
            True,
            ("claude-opus-4-8", "self_reported"),
        ),
        (
            {"x-codex-turn-metadata": "gpt-5.6-sol"},
            "claude-opus-4-8",
            True,
            ("claude-opus-4-8", "self_reported"),
        ),
        (None, "unknown", True, (None, None)),
        (None, "claude-opus-4-8", False, (None, None)),
    ],
)
def test_resolve_model(metadata, argument, allow_self_reported, expected):
    assert (
        resolve_model(
            metadata,
            {"llm_model": argument},
            allow_self_reported=allow_self_reported,
        )
        == expected
    )


def test_request_meta_from_context_supports_pydantic_style_metadata():
    meta = SimpleNamespace(
        model_dump=lambda **kwargs: {"x-codex-turn-metadata": {"model": "gpt-5.6-sol"}}
    )

    assert request_meta_from_context(SimpleNamespace(meta=meta)) == {
        "x-codex-turn-metadata": {"model": "gpt-5.6-sol"}
    }
