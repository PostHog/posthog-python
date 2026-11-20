from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from typing_extensions import TypedDict

from ._errors import EvaluationAPIError
from ._serialization import (
    JSONValue,
    _MAX_ITEM_BYTES,
    _MAX_RESULT_BYTES,
    _checked_payload,
    _encode,
    _identifier,
    _integer,
    _score,
    _uuid,
)
from ._types import ResultStatus, ScoreValue, _validate_result_status


class ItemUpdate(TypedDict, total=False):
    """Editable experiment item fields. An explicit None clears the field."""

    case_key: str | None
    trial: str | None
    model_version: str | None
    application_trace_id: str | None


class ResultUpdate(TypedDict, total=False):
    """Fields for correcting the outcome of a reported result."""

    status: ResultStatus
    value: ScoreValue | None
    error_code: str | None


def _item_path(item_id: str | UUID) -> str:
    return f"items/{_uuid(item_id)}/"


def _result_path(result_id: str | UUID) -> str:
    return f"results/{_uuid(result_id)}/"


def _item_changes(fields: ItemUpdate) -> dict[str, Any]:
    if fields.keys() - ItemUpdate.__annotations__:
        raise TypeError("Unsupported item update field.")
    return {key: _identifier(value, key) for key, value in fields.items()}


def _result_changes(fields: ResultUpdate) -> dict[str, Any]:
    if fields.keys() - ResultUpdate.__annotations__:
        raise TypeError("Unsupported result update field.")
    changes: dict[str, Any] = dict(fields)
    value = fields.get("value")
    error_code = fields.get("error_code")
    if "status" in fields:
        status = fields["status"]
        _validate_result_status(status, value, error_code)
        if status == "ok" and "value" in fields and value is None:
            raise ValueError("An ok result requires a score.")
    elif value is not None and error_code is not None:
        raise ValueError("A result cannot contain both a score and an error_code.")
    if value is not None:
        changes["value"] = _score(value)
    if "error_code" in fields:
        changes["error_code"] = _identifier(error_code, "error_code", 128)
    return changes


def _item_payload_changes(payload: dict[str, JSONValue]) -> dict[str, Any]:
    fields = ("input", "output", "expected_output", "metadata")
    return {"payload": _checked_payload(payload, fields, _MAX_ITEM_BYTES)}


def _result_payload_changes(payload: dict[str, JSONValue]) -> dict[str, Any]:
    fields = ("reasoning", "error_message", "metadata")
    return {"payload": _checked_payload(payload, fields, _MAX_RESULT_BYTES)}


def _patch_body(revision: int, changes: Mapping[str, Any]) -> bytes:
    if not changes:
        raise ValueError("Supply at least one field to update.")
    return _encode({"revision": _integer(revision, minimum=0), **changes})


def _response_revision(
    data: dict[str, Any], *, identity: str | UUID | None = None, minimum: int = 0
) -> int:
    try:
        if identity is not None and _uuid(data["id"]) != _uuid(identity):
            raise ValueError("Mismatched resource ID.")
        return _integer(data["revision"], minimum=minimum)
    except (KeyError, TypeError, ValueError):
        raise EvaluationAPIError(
            code="invalid_response",
            detail="Expected the current revision of the requested resource.",
        ) from None
