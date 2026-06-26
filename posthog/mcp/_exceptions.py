# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Build PostHog error-tracking properties (``$exception_list`` /
``$exception_level``) from arbitrary thrown values, reusing posthog-python's own
``exceptions_from_error_tuple`` so MCP tool failures group and symbolicate the
same way as exceptions from any other PostHog SDK.
"""

from __future__ import annotations

from typing import Any, List

from posthog.exception_utils import exceptions_from_error_tuple

from .types import ErrorProperties


def capture_exception(error: Any) -> ErrorProperties:
    """Return the ``$exception_list`` shape for any thrown value (Exception,
    string, CallToolResult, or arbitrary object)."""
    # MCP SDK converts tool errors to a CallToolResult, which carries only a
    # human-readable message — extract it so the exception still says something.
    if _is_call_tool_result(error):
        return _from_message(_extract_call_tool_result_message(error))

    if isinstance(error, BaseException):
        exc_info = (type(error), error, error.__traceback__)
        return {
            "$exception_list": exceptions_from_error_tuple(exc_info),
            "$exception_level": "error",
        }

    if isinstance(error, str):
        return _from_message(error)

    return _from_message(_safe_str(error))


def _from_message(message: str) -> ErrorProperties:
    return {
        "$exception_list": [
            {
                "mechanism": {"type": "generic", "handled": True},
                "type": "Error",
                "value": message,
            }
        ],
        "$exception_level": "error",
    }


def _is_call_tool_result(value: Any) -> bool:
    """Detect a CallToolResult error (``{isError, content: [...]}``), whether a
    dict or a pydantic model from the ``mcp`` SDK."""
    if isinstance(value, dict):
        return "isError" in value and isinstance(value.get("content"), list)
    return hasattr(value, "isError") and isinstance(
        getattr(value, "content", None), list
    )


def _extract_call_tool_result_message(result: Any) -> str:
    content = (
        result.get("content")
        if isinstance(result, dict)
        else getattr(result, "content", [])
    )
    texts: List[str] = []
    for part in content or []:
        part_type = (
            part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
        )
        text = (
            part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        )
        if part_type == "text" and isinstance(text, str):
            texts.append(text)
    return " ".join(texts).strip() or "Unknown error"


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return "Unknown error"
