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

# SDK-injected arguments stripped from captured $mcp_parameters (they surface as
# dedicated properties: $mcp_intent and $mcp_conversation_id).
_INJECTED_ARGUMENT_NAMES = ("context", "conversation_id")
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
    return _redact_secret_tokens(_POSTHOG_TOKEN_PATTERN.sub(_REDACTED_VALUE, value))


def _redact_secret_tokens(value: str) -> str:
    """Redact credential-looking words, leaving the surrounding text intact.

    The PostHog-token pattern above only knows ``phc_``/``phx_``; a failure
    message like ``auth failed for sk-proj-...`` carries someone else's key.
    Rather than enumerate every vendor's format — an arms race that fails
    quietly in both directions — this reuses the SDK's own detector
    (``exception_utils._looks_like_secret``: entropy, known formats such as AWS
    key ids, PEM markers), which the code-variables path already ships.

    Applied per whitespace-separated token, not to the whole string: redacting
    an entire exception message would destroy the diagnostic value that
    ``$mcp_error_message`` exists to provide, and ordinary prose is left alone
    because no single word in it looks like a credential.
    """
    if " " not in value:
        return _REDACTED_VALUE if _is_secret(value) else value
    return " ".join(
        _REDACTED_VALUE if _is_secret(word) else word for word in value.split(" ")
    )


def _is_secret(word: str) -> bool:
    try:
        from posthog.exception_utils import _looks_like_secret

        return bool(word) and _looks_like_secret(word)
    except Exception:  # noqa: BLE001 - redaction must never break capture
        return False


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
    """Sanitize an event's response, parameters, user_intent and error. Returns
    a new shallow copy; does not mutate the input."""
    result = {**event}

    if result.get("response") is not None:
        result["response"] = _sanitize_response(result["response"])

    if result.get("parameters") is not None:
        result["parameters"] = sanitize_captured_value(result["parameters"])

    # The intent comes straight from an agent-narrated `context` string, so it
    # can contain a secret the LLM read aloud. Redact it like any other value.
    if result.get("user_intent") is not None:
        result["user_intent"] = sanitize_captured_value(result["user_intent"])

    # An exception message is free text a server wrote, and it reaches PostHog
    # on the $exception sibling and — since it is also surfaced as
    # $mcp_error_message — on the primary event, so run it through the same
    # sanitizer as every other captured value.
    #
    # That sanitizer redacts PostHog tokens and sensitive-looking keys; it is
    # deliberately not a general credential scrubber, because enumerating every
    # vendor's key format is an arms race that fails quietly in both directions.
    # A host with strict requirements should gate free text in `before_send`.
    # Same scope as @posthog/mcp's sanitizeCapturedValue.
    if result.get("error") is not None:
        result["error"] = _sanitize_exception_values(result["error"])

    return result


def _sanitize_exception_values(error: Any) -> Any:
    """Redact the ``value`` of every frame in an ``$exception_list``, leaving
    the rest of the error-tracking shape untouched."""
    if not isinstance(error, dict):
        return error
    exception_list = error.get("$exception_list")
    if not isinstance(exception_list, list):
        return error
    return {
        **error,
        "$exception_list": [
            {**exception, "value": sanitize_captured_value(exception.get("value"))}
            if isinstance(exception, dict)
            else exception
            for exception in exception_list
        ],
    }


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
        if key in _INJECTED_ARGUMENT_NAMES:
            continue
        captured[key] = sanitize_captured_value(value)
    return captured
