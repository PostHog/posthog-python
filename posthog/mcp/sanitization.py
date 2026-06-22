# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Event sanitization: redact non-text response content blocks, large base64
strings, PostHog tokens, and sensitive keys. Pure functions that return new
objects without mutating the input; run after customer redaction (``before_send``
runs later in the pipeline) but before truncation.
"""

from __future__ import annotations

import re
from typing import Any, Dict

_CONTEXT_ARGUMENT_NAME = "context"
_REDACTED_VALUE = "[redacted]"
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/\n\r]+=*$")
_SIZE_GATE = 10_240
_POSTHOG_TOKEN_PATTERN = re.compile(r"\bph[a-z]_[A-Za-z0-9_-]{20,}\b")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"^(authorization|cookie|set-cookie|x-api-key|api[-_]?key|api[-_]?token|"
    r"access[-_]?token|refresh[-_]?token|token|password|secret|client[-_]?secret|"
    r"private[-_]?key)$",
    re.IGNORECASE,
)


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _should_redact_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.match(key))


def _sanitize_string(value: str) -> str:
    if len(value) >= _SIZE_GATE and _BASE64_PATTERN.match(value):
        return "[binary data redacted - not supported by PostHog MCP analytics]"
    return _POSTHOG_TOKEN_PATTERN.sub(_REDACTED_VALUE, value)


def sanitize_captured_value(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, list):
        return [sanitize_captured_value(item) for item in value]
    # bool is an int subclass; both pass through unchanged.
    if not isinstance(value, dict):
        return value

    result: Dict[str, Any] = {}
    for key, nested in value.items():
        result[key] = (
            _REDACTED_VALUE
            if _should_redact_key(str(key))
            else sanitize_captured_value(nested)
        )
    return result


def sanitize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize an event's response, parameters, and user_intent. Returns a new
    shallow copy; does not mutate the input."""
    result = {**event}

    if result.get("response") is not None:
        result["response"] = _sanitize_response(result["response"])

    if result.get("parameters") is not None:
        result["parameters"] = sanitize_captured_value(result["parameters"])

    # The intent comes straight from an agent-narrated `context` string, so it
    # can contain a secret the LLM read aloud. Redact it like any other value.
    if result.get("user_intent") is not None:
        result["user_intent"] = sanitize_captured_value(result["user_intent"])

    return result


def _sanitize_response(response: Any) -> Any:
    if response is None or not isinstance(response, (dict, list, str)):
        return sanitize_captured_value(response)

    sanitized = sanitize_captured_value(response)
    if not _is_record(sanitized):
        return sanitized

    result = {**sanitized}
    content = result.get("content")
    if isinstance(content, list):
        result["content"] = [_sanitize_content_block(block) for block in content]

    if result.get("structuredContent") is not None and isinstance(
        result["structuredContent"], (dict, list)
    ):
        result["structuredContent"] = sanitize_captured_value(
            result["structuredContent"]
        )

    return result


def _sanitize_content_block(block: Any) -> Any:
    if not _is_record(block):
        return block

    block_type = block.get("type")
    if block_type == "text":
        return sanitize_captured_value(block)
    if block_type == "image":
        return {
            "type": "text",
            "text": "[image content redacted - not supported by PostHog MCP analytics]",
        }
    if block_type == "audio":
        return {
            "type": "text",
            "text": "[audio content redacted - not supported by PostHog MCP analytics]",
        }
    if block_type == "resource":
        return _sanitize_resource_block(block)
    if block_type == "resource_link":
        return sanitize_captured_value(block)
    return {
        "type": "text",
        "text": f'[unsupported content type "{block_type}" redacted - not supported by PostHog MCP analytics]',
    }


def _sanitize_resource_block(block: Dict[str, Any]) -> Any:
    resource = block.get("resource")
    if isinstance(resource, dict) and "blob" in resource:
        return {
            "type": "text",
            "text": "[binary resource content redacted - not supported by PostHog MCP analytics]",
        }
    return sanitize_captured_value(block)


def build_captured_mcp_parameters(request: Any) -> Dict[str, Any]:
    """Build the sanitized ``$mcp_parameters`` payload from a request, stripping
    the injected ``context`` argument before logging."""
    if not _is_record(request):
        return {"request": sanitize_captured_value(request)}

    captured_request: Dict[str, Any] = {}
    for key in ("id", "jsonrpc", "method"):
        if key in request:
            captured_request[key] = sanitize_captured_value(request[key])

    if "params" in request:
        captured_request["params"] = _build_captured_mcp_params(request["params"])

    return {"request": captured_request}


def _build_captured_mcp_params(params: Any) -> Any:
    if not _is_record(params):
        return sanitize_captured_value(params)

    captured: Dict[str, Any] = {}
    for key, value in params.items():
        captured[key] = (
            _build_captured_mcp_arguments(value)
            if key == "arguments"
            else sanitize_captured_value(value)
        )
    return captured


def _build_captured_mcp_arguments(arguments: Any) -> Any:
    if not _is_record(arguments):
        return sanitize_captured_value(arguments)

    captured: Dict[str, Any] = {}
    for key, value in arguments.items():
        if key == _CONTEXT_ARGUMENT_NAME:
            continue
        captured[key] = sanitize_captured_value(value)
    return captured
