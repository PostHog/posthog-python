"""Capture the calling model from MCP request metadata or an injected argument.

MCP does not standardize or attest model identity. Codex currently exposes its
model in request ``_meta``; other clients can use the SDK-injected
``llm_model`` argument as an explicitly lower-confidence fallback.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from .constants import DEFAULT_MODEL_PARAMETER_DESCRIPTION
from .logger import log
from .types import MCPAnalyticsModelOptions, MCPAnalyticsModelSource

_CODEX_TURN_METADATA_KEY = "x-codex-turn-metadata"


def is_capture_model_enabled(
    capture_model: object,
) -> bool:
    return capture_model is True or isinstance(capture_model, MCPAnalyticsModelOptions)


def get_model_description(capture_model: object) -> Optional[str]:
    if isinstance(capture_model, MCPAnalyticsModelOptions):
        return capture_model.description
    return None


def add_model_parameter_to_schema(
    input_schema: Optional[Dict[str, Any]],
    tool_name: str = "unknown",
    description_override: Optional[str] = None,
    required: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return a copied schema with an SDK-owned ``llm_model`` field.

    Existing application fields and complex schemas fail closed: the SDK must
    never overwrite or later strip a value that belongs to the tool itself.
    """
    schema = input_schema
    if not can_inject_model_parameter(schema):
        log(
            f'WARN: Tool "{tool_name}" has an application-owned llm_model or '
            "complex schema ($ref/oneOf/allOf/anyOf). Skipping model injection."
        )
        return schema

    if not schema:
        schema = {"type": "object", "properties": {}, "required": []}

    schema = copy.deepcopy(schema)
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    schema["properties"]["llm_model"] = {
        "type": "string",
        "description": description_override or DEFAULT_MODEL_PARAMETER_DESCRIPTION,
    }
    if required:
        required_list = schema.get("required")
        if isinstance(required_list, list):
            if "llm_model" not in required_list:
                required_list.append("llm_model")
        else:
            schema["required"] = ["llm_model"]
    return schema


def can_inject_model_parameter(input_schema: Any) -> bool:
    if not isinstance(input_schema, dict):
        return True
    properties = input_schema.get("properties")
    if isinstance(properties, dict) and "llm_model" in properties:
        return False
    return not any(input_schema.get(key) for key in ("$ref", "oneOf", "allOf", "anyOf"))


def request_meta_from_context(context: Any) -> Optional[Dict[str, Any]]:
    """Read request ``_meta`` from either MCP Python SDK generation."""
    meta = getattr(context, "meta", None)
    if isinstance(meta, dict):
        return meta
    model_dump = getattr(meta, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="python", by_alias=True)
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001 - metadata must never break a tool call
            return None
    dict_method = getattr(meta, "dict", None)
    if callable(dict_method):
        try:
            dumped = dict_method(by_alias=True)
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001 - Pydantic v1 compatibility
            return None
    return None


def resolve_model(
    request_meta: Optional[Dict[str, Any]],
    arguments: Optional[Dict[str, Any]],
    *,
    allow_self_reported: bool,
) -> tuple[Optional[str], Optional[MCPAnalyticsModelSource]]:
    """Resolve the strongest model identity visible to the MCP server."""
    codex_metadata = (request_meta or {}).get(_CODEX_TURN_METADATA_KEY)
    if isinstance(codex_metadata, dict) and "model" in codex_metadata:
        model = normalize_model(codex_metadata.get("model"))
        if model:
            return model, "client_metadata"

    if allow_self_reported:
        model = normalize_model((arguments or {}).get("llm_model"))
        if model:
            return model, "self_reported"
    return None, None


def normalize_model(model: Any) -> Optional[str]:
    if not isinstance(model, str):
        return None
    normalized = model.strip()
    if not normalized or normalized.lower() == "unknown":
        return None
    return normalized
