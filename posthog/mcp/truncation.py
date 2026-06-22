# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Layered truncation so an event fits within a byte budget before capture:

1. Field-level string limits (user_intent, resource_name, metadata fields).
2. Error frame limiting + message caps on the ``$exception_list`` shape.
3. Response content text limits (32KB per text block).
4. Recursive normalization of user-controlled fields (depth/breadth/string caps).
5. Size-targeted truncation: progressive depth reduction, then trimming the
   largest string fields until under MAX_EVENT_BYTES.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_DEPTH = 10
MAX_BREADTH = 100
MAX_STRING_LENGTH = 32_768  # 32KB
MAX_EVENT_BYTES = 102_400  # 100KB

_MAX_USER_INTENT_LENGTH = 2048
_MAX_ERROR_MESSAGE_LENGTH = 2048
_MAX_RESOURCE_NAME_LENGTH = 256
_MAX_METADATA_LENGTH = 256
_MAX_STACK_FRAMES = 50
_MAX_CONTENT_TEXT_LENGTH = 32_768

_TRUNCATION_SUFFIX = "..."

_METADATA_FIELDS = (
    ("user_intent", _MAX_USER_INTENT_LENGTH),
    ("resource_name", _MAX_RESOURCE_NAME_LENGTH),
    ("server_name", _MAX_METADATA_LENGTH),
    ("server_version", _MAX_METADATA_LENGTH),
    ("client_name", _MAX_METADATA_LENGTH),
    ("client_version", _MAX_METADATA_LENGTH),
)

_NORMALIZED_FIELDS = ("parameters", "response", "identify_actor_data", "error")


# --- normalize ---------------------------------------------------------------


def normalize(
    value: Any,
    depth: int = MAX_DEPTH,
    max_breadth: int = MAX_BREADTH,
    max_string_length: int = MAX_STRING_LENGTH,
) -> Any:
    """Recursively normalize a value: cap strings, coerce non-serializable
    values, convert datetimes, detect cycles, and bound depth/breadth."""
    return _visit(value, depth, max_breadth, max_string_length, set())


def _visit(
    value: Any,
    remaining_depth: int,
    max_breadth: int,
    max_string_length: int,
    memo: set,
) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):  # before int — bool is an int subclass
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            if math.isnan(value):
                return "[NaN]"
            if math.isinf(value):
                return "[Infinity]" if value > 0 else "[-Infinity]"
        return value
    if isinstance(value, str):
        if len(value) > max_string_length:
            return value[:max_string_length] + _TRUNCATION_SUFFIX
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if callable(value):
        return f"[Function: {getattr(value, '__name__', '') or '<anonymous>'}]"

    if isinstance(value, (list, tuple)):
        oid = id(value)
        if oid in memo:
            return "[Circular ~]"
        if remaining_depth <= 0:
            return "[Array]"
        memo.add(oid)
        result = _visit_array(
            list(value), remaining_depth - 1, max_breadth, max_string_length, memo
        )
        memo.discard(oid)
        return result

    if isinstance(value, dict):
        oid = id(value)
        if oid in memo:
            return "[Circular ~]"
        if remaining_depth <= 0:
            return "[Object]"
        memo.add(oid)
        result = _visit_object(
            value, remaining_depth - 1, max_breadth, max_string_length, memo
        )
        memo.discard(oid)
        return result

    return str(value)


def _visit_array(
    arr: List[Any],
    remaining_depth: int,
    max_breadth: int,
    max_string_length: int,
    memo: set,
) -> List[Any]:
    result: List[Any] = []
    for i, item in enumerate(arr):
        if i >= max_breadth:
            result.append("[MaxProperties ~]")
            break
        result.append(
            _visit(item, remaining_depth, max_breadth, max_string_length, memo)
        )
    return result


def _visit_object(
    obj: Dict[Any, Any],
    remaining_depth: int,
    max_breadth: int,
    max_string_length: int,
    memo: set,
) -> Dict[Any, Any]:
    result: Dict[Any, Any] = {}
    count = 0
    for key, val in obj.items():
        if count >= max_breadth:
            result["..."] = "[MaxProperties ~]"
            break
        result[key] = _visit(val, remaining_depth, max_breadth, max_string_length, memo)
        count += 1
    return result


# --- field-level helpers -----------------------------------------------------


def _truncate_string(value: Optional[str], max_length: int) -> Optional[str]:
    if not isinstance(value, str):
        return value
    if len(value) <= max_length:
        return value
    return value[:max_length] + _TRUNCATION_SUFFIX


def _truncate_stack_frames(frames: Optional[List[Any]]) -> Optional[List[Any]]:
    if not frames or len(frames) <= _MAX_STACK_FRAMES:
        return frames
    half = _MAX_STACK_FRAMES // 2
    return frames[:half] + frames[-half:]


def _truncate_exception_list(error: Dict[str, Any]) -> Dict[str, Any]:
    exception_list = error.get("$exception_list")
    if not isinstance(exception_list, list):
        return error
    result = {**error}
    truncated = []
    for exception in exception_list:
        nxt = {**exception}
        if isinstance(nxt.get("value"), str):
            nxt["value"] = _truncate_string(nxt["value"], _MAX_ERROR_MESSAGE_LENGTH)
        stacktrace = nxt.get("stacktrace")
        if isinstance(stacktrace, dict) and stacktrace.get("frames"):
            nxt["stacktrace"] = {
                **stacktrace,
                "frames": _truncate_stack_frames(stacktrace["frames"]),
            }
        truncated.append(nxt)
    result["$exception_list"] = truncated
    return result


def _truncate_response_content(response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    result = {**response}
    content = result.get("content")
    if isinstance(content, list):
        new_content = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and len(block["text"]) > _MAX_CONTENT_TEXT_LENGTH
            ):
                new_content.append(
                    {
                        **block,
                        "text": block["text"][:_MAX_CONTENT_TEXT_LENGTH]
                        + _TRUNCATION_SUFFIX,
                    }
                )
            else:
                new_content.append(block)
        result["content"] = new_content
    return result


# --- size-targeted truncation ------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _json_byte_size(value: Any) -> int:
    return len(
        json.dumps(value, default=_json_default, separators=(",", ":")).encode("utf-8")
    )


def _collect_string_paths(
    obj: Any, current_path: List[str], results: List[Dict[str, Any]]
) -> None:
    if isinstance(obj, str):
        if len(obj) > 100:
            results.append({"path": list(current_path), "length": len(obj)})
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            _collect_string_paths(item, current_path + [str(i)], results)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            _collect_string_paths(value, current_path + [str(key)], results)


def _get_nested_value(obj: Any, path: List[str]) -> Any:
    current = obj
    for key in path:
        if isinstance(current, list):
            current = current[int(key)]
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _set_nested_value(obj: Any, path: List[str], value: Any) -> None:
    current = obj
    for key in path[:-1]:
        if isinstance(current, list):
            current = current[int(key)]
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            return
    final_key = path[-1]
    if isinstance(current, list):
        current[int(final_key)] = value
    elif isinstance(current, dict):
        current[final_key] = value


def _truncate_largest_fields(obj: Any, max_bytes: int) -> Any:
    result = copy.deepcopy(obj)

    for _ in range(10):
        current_size = _json_byte_size(result)
        if current_size <= max_bytes:
            return result
        excess = current_size - max_bytes

        string_paths: List[Dict[str, Any]] = []
        _collect_string_paths(result, [], string_paths)
        string_paths.sort(key=lambda p: p["length"], reverse=True)
        if not string_paths:
            break

        remaining = excess + 200  # buffer for JSON overhead from added "..." suffixes
        truncated = False
        for entry in string_paths:
            if remaining <= 0:
                break
            length = entry["length"]
            reduction = min(remaining, length // 2)
            if reduction < 10:
                continue
            new_length = length - reduction
            current_value = _get_nested_value(result, entry["path"])
            if not isinstance(current_value, str):
                continue
            _set_nested_value(
                result, entry["path"], current_value[:new_length] + _TRUNCATION_SUFFIX
            )
            remaining -= reduction
            truncated = True

        if not truncated:
            break

    return result


def _truncate_to_size(event: Dict[str, Any]) -> Dict[str, Any]:
    if _json_byte_size(event) <= MAX_EVENT_BYTES:
        return event

    for depth in range(MAX_DEPTH - 1, 0, -1):
        reduced = {**event}
        for field in _NORMALIZED_FIELDS:
            if reduced.get(field) is not None:
                reduced[field] = normalize(reduced[field], depth)
        if _json_byte_size(reduced) <= MAX_EVENT_BYTES:
            return reduced

    minimal = {**event}
    for field in _NORMALIZED_FIELDS:
        if minimal.get(field) is not None:
            minimal[field] = normalize(minimal[field], 1)
    return _truncate_largest_fields(minimal, MAX_EVENT_BYTES)


def truncate_event(event: Dict[str, Any]) -> Dict[str, Any]:
    result = {**event}

    # Layer 1: field-level string limits
    for key, max_length in _METADATA_FIELDS:
        if isinstance(result.get(key), str):
            result[key] = _truncate_string(result[key], max_length)

    if isinstance(result.get("error"), dict):
        result["error"] = _truncate_exception_list(result["error"])

    if result.get("response") is not None:
        result["response"] = _truncate_response_content(result["response"])

    # Layer 2: recursive normalization on user-controlled fields
    for field in _NORMALIZED_FIELDS:
        if result.get(field) is not None:
            result[field] = normalize(result[field])

    # Layer 3: size-targeted normalization
    return _truncate_to_size(result)
