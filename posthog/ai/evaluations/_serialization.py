from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Union, cast
from uuid import UUID

JSONValue = Union[
    None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]
]


class _Unset:
    def __repr__(self) -> str:
        return "<not supplied>"


_UNSET = _Unset()
_MAX_ITEMS = 1000
_MAX_RESULTS = 1000
_MAX_REQUEST_BYTES = 5 * 1024 * 1024
_MAX_ITEM_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 256 * 1024


def _uuid(value: object) -> str:
    if not isinstance(value, (str, UUID)):
        raise ValueError("Provide a UUID or a valid UUID string.")
    try:
        return str(UUID(str(value)))
    except ValueError as exc:
        raise ValueError("Provide a valid UUID.") from exc


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone.")
    return value.astimezone(timezone.utc).isoformat()


def _identifier(value: object, field: str, limit: int = 255) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not value or len(value) > limit or "\x00" in value
    ):
        raise ValueError(
            f"{field} must be a nonempty string of at most {limit} characters."
        )
    return value


def _encode(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(
            "Provide JSON values with finite numbers and valid Unicode."
        ) from exc


def _validate_json(value: object, depth: int = 1) -> None:
    if isinstance(value, (dict, list)):
        if depth > 32:
            raise ValueError("Payload JSON nesting must not exceed 32 levels.")
        children: Iterable[object]
        if isinstance(value, dict):
            if any(not isinstance(key, str) or "\x00" in key for key in value):
                raise ValueError(
                    "JSON object keys must be strings without null characters."
                )
            children = value.values()
        else:
            children = value
        for child in children:
            _validate_json(child, depth + 1)
    elif isinstance(value, str):
        if "\x00" in value:
            raise ValueError("JSON strings must not contain null characters.")
    elif value is not None and type(value) not in (bool, int, float):
        raise ValueError("Provide JSON values, without automatic object conversion.")


def _payload(
    explicit: dict[str, JSONValue] | _Unset,
    fields: dict[str, JSONValue | _Unset],
    limit: int,
) -> dict[str, JSONValue] | _Unset:
    supplied = {
        key: value for key, value in fields.items() if not isinstance(value, _Unset)
    }
    if not isinstance(explicit, _Unset):
        if supplied:
            raise ValueError("Supply payload or individual payload fields, not both.")
        if not isinstance(explicit, dict) or set(explicit) - set(fields):
            raise ValueError(
                "payload must be an object containing supported fields only."
            )
        supplied = explicit
    elif not supplied:
        return _UNSET
    metadata = supplied.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object or None.")
    for field in ("reasoning", "error_message"):
        if supplied.get(field) is not None and not isinstance(supplied[field], str):
            raise ValueError(f"{field} must be a string or None.")
    _validate_json(supplied)
    encoded = _encode(supplied)
    if len(encoded) > limit:
        raise ValueError(f"Payload exceeds {limit} bytes of UTF-8 JSON.")
    return json.loads(encoded)


def _score(value: object) -> bool | float | list[str]:
    if type(value) is bool:
        return value
    if type(value) in (int, float):
        try:
            number = float(cast(Union[int, float], value))
        except OverflowError as exc:
            raise ValueError(
                "Numeric scores must fit a finite binary64 number."
            ) from exc
        if not math.isfinite(number):
            raise ValueError("Numeric scores must be finite.")
        return number
    if (
        isinstance(value, list)
        and value
        and all(
            isinstance(key, str) and key and len(key) <= 128 and "\x00" not in key
            for key in value
        )
    ):
        if len(set(value)) != len(value):
            raise ValueError("Categorical scores must contain distinct keys.")
        return sorted(value)
    raise ValueError(
        "A score must be a boolean, finite number, or nonempty list of category keys."
    )


def _count(value: object, field: str) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise ValueError(
            f"{field} must be a nonnegative signed 64-bit integer or None."
        )
    return value


def _object(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Expected a response object.")
    return data


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("Expected a boolean.")
    return value


def _response_count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative count.")
    return value


def _date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp.")
    return datetime.fromisoformat(_timestamp(value))
