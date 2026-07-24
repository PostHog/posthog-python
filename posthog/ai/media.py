import base64
import dataclasses
import json
from typing import Any

_CAMEL_TO_SNAKE = {
    "inlineData": "inline_data",
    "fileData": "file_data",
    "mimeType": "mime_type",
    "fileUri": "file_uri",
    "functionCall": "function_call",
    "functionResponse": "function_response",
    "videoMetadata": "video_metadata",
}


def to_plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if v is not None}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(exclude_none=True)
        except TypeError:
            return {k: v for k, v in model_dump().items() if v is not None}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: v for k, v in dataclasses.asdict(obj).items() if v is not None}
    return obj


def ensure_serializable(obj: Any) -> Any:
    """Ensure an object is JSON-serializable, converting to str as fallback.

    Recurses into dicts/lists/tuples so one non-serializable leaf doesn't
    collapse the whole structure to a string. Bytes pass through untouched -
    finalize_ai_content is responsible for redacting or base64-encoding them.
    Non-string dict keys are coerced to str so json.dumps can't TypeError at
    send time on tuple/object keys. Guards against reference cycles the same
    way redact_media does, so a self-referencing structure can't blow the
    stack.
    """
    stack: set = set()

    def walk(node: Any) -> Any:
        if node is None or isinstance(node, bytes):
            return node
        if isinstance(node, dict):
            if id(node) in stack:
                return "<circular>"
            stack.add(id(node))
            try:
                return {
                    (k if isinstance(k, str) else str(k)): walk(v)
                    for k, v in node.items()
                }
            finally:
                stack.discard(id(node))
        if isinstance(node, (list, tuple)):
            if id(node) in stack:
                return "<circular>"
            stack.add(id(node))
            try:
                return [walk(v) for v in node]
            finally:
                stack.discard(id(node))
        try:
            json.dumps(node)
            return node
        except (TypeError, ValueError):
            return str(node)

    return walk(obj)


def bytes_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def normalize_part_keys(d: dict) -> dict:
    out = {}
    for key, value in d.items():
        snake = _CAMEL_TO_SNAKE.get(key, key)
        if isinstance(value, dict):
            value = {_CAMEL_TO_SNAKE.get(k, k): v for k, v in value.items()}
        out[snake] = value
    return out
