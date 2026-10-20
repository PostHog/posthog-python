"""Dataset wire validation shared by the synchronous and asynchronous clients."""

from __future__ import annotations

import json
from typing import Any, Callable, Sequence, TypeVar, cast
from urllib.parse import parse_qs, urlsplit

from ._dataset_types import Dataset, DatasetItem, DatasetRevision
from ._errors import EvaluationAPIError
from ._serialization import (
    JSONValue,
    _Unset,
    _boolean,
    _date,
    _encode,
    _identifier,
    _object,
    _uuid,
    _validate_json,
)
from ._tags import _response_tags, _tag_names

_T = TypeVar("_T")


def _integer(value: Any, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"Expected an integer greater than or equal to {minimum}.")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string.")
    return value


def _json(value: Any) -> JSONValue:
    return cast(JSONValue, json.loads(_encode(value)))


def _metadata(value: Any) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], _json(_object(value)))


def _user(value: Any) -> dict[str, Any] | None:
    return None if value is None else _metadata(value)


def _owner(data: dict[str, Any], project_id: int) -> int:
    if _integer(data["team_id"]) != project_id:
        raise ValueError("The dataset response belongs to another project.")
    return project_id


def _dataset(data: dict[str, Any], project_id: int, id: str | None = None) -> Dataset:
    identity = _uuid(data["id"])
    if id is not None and identity != id:
        raise ValueError("Mismatched dataset ID.")
    revision = data["current_revision"]
    revision_id = data["current_revision_id"]
    if (revision is None) != (revision_id is None):
        raise ValueError("Incomplete dataset revision reference.")
    return Dataset(
        id=identity,
        name=_text(data["name"]),
        description=_text(data["description"]),
        metadata=_metadata(data["metadata"]),
        tags=_response_tags(data.get("tags", [])),
        archived=_boolean(data["archived"]),
        current_revision=None if revision is None else _integer(revision),
        current_revision_id=None if revision_id is None else _uuid(revision_id),
        created_at=_date(data["created_at"]),
        updated_at=None if data["updated_at"] is None else _date(data["updated_at"]),
        created_by=_user(data["created_by"]),
        team_id=_owner(data, project_id),
    )


def _revision(
    data: dict[str, Any], project_id: int, dataset_id: str
) -> DatasetRevision:
    if _uuid(data["dataset_id"]) != dataset_id:
        raise ValueError("Mismatched revision dataset ID.")
    return DatasetRevision(
        id=_uuid(data["id"]),
        dataset_id=dataset_id,
        revision=_integer(data["revision"]),
        created_at=_date(data["created_at"]),
        created_by=_user(data["created_by"]),
        team_id=_owner(data, project_id),
    )


def _item(
    data: dict[str, Any],
    project_id: int,
    *,
    id: str | None = None,
    dataset_id: str | None = None,
    revision: int | None = None,
    archived: bool | None = None,
) -> DatasetItem:
    identity, owner = _uuid(data["id"]), _uuid(data["dataset"])
    introduced = _integer(data["dataset_revision"])
    is_archived = _boolean(data["archived"])
    if (id is not None and identity != id) or (
        dataset_id is not None and owner != dataset_id
    ):
        raise ValueError("Mismatched dataset item identity.")
    if revision is not None and introduced > revision:
        raise ValueError("The item version is newer than the requested revision.")
    if archived is not None and is_archived != archived:
        raise ValueError("The item does not match the requested archive filter.")
    if data["input"] is None:
        raise ValueError("Dataset inputs cannot be null.")
    return DatasetItem(
        id=identity,
        dataset_id=owner,
        client_item_id=_identifier(data["client_item_id"], "client_item_id"),
        version=_integer(data["version"]),
        version_id=_uuid(data["version_id"]),
        dataset_revision=introduced,
        dataset_revision_id=_uuid(data["dataset_revision_id"]),
        archived=is_archived,
        input=_json(data["input"]),
        expected_output=_json(data["expected_output"]),
        source_output=_json(data["source_output"]),
        metadata=_metadata(data["metadata"]),
        source_trace_id=_identifier(data["source_trace_id"], "source_trace_id"),
        source_event_id=_identifier(data["source_event_id"], "source_event_id"),
        source_timestamp=None
        if data["source_timestamp"] is None
        else _date(data["source_timestamp"]),
        created_at=_date(data["created_at"]),
        updated_at=None if data["updated_at"] is None else _date(data["updated_at"]),
        created_by=_user(data["created_by"]),
        version_created_at=_date(data["version_created_at"]),
        version_created_by=_user(data["version_created_by"]),
        team_id=_owner(data, project_id),
    )


def _parse(parser: Callable[[dict[str, Any]], _T], data: dict[str, Any]) -> _T:
    try:
        return parser(data)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise EvaluationAPIError(
            code="invalid_response",
            detail="The dataset API returned an invalid response.",
            response=data,
            persistence="unknown",
        ) from None


def _page(
    data: dict[str, Any], parser: Callable[[dict[str, Any]], _T], offset: int
) -> tuple[int, tuple[_T, ...], int | None]:
    rows = data["results"]
    if not isinstance(rows, list):
        raise ValueError("Expected a list of dataset results.")
    results = tuple(parser(_object(row)) for row in rows)
    count = _integer(data["count"], 0)
    next_offset = None
    if data["next"] is not None:
        # Extract only the continuation. Never send credentials to a response URL.
        offsets = parse_qs(urlsplit(_text(data["next"])).query).get("offset")
        if offsets is None or len(offsets) != 1:
            raise ValueError("Missing dataset page offset.")
        next_offset = _integer(int(offsets[0]), offset + 1)
        if not results:
            raise ValueError("An empty page must not have a continuation.")
    return count, results, next_offset


def _page_params(limit: int, offset: int, maximum: int) -> dict[str, str | int]:
    if _integer(limit) > maximum:
        raise ValueError(f"Page limit must be between 1 and {maximum}.")
    return {"limit": limit, "offset": _integer(offset, 0)}


def _body(fields: dict[str, Any]) -> bytes:
    data = {
        key: value for key, value in fields.items() if not isinstance(value, _Unset)
    }
    for key in ("name", "description"):
        if key in data:
            value = _text(data[key])
            maximum = 400 if key == "name" else 10_000
            if len(value) > maximum or (key == "name" and not value.strip()):
                raise ValueError(f"Invalid dataset {key}.")
    if "input" in data and data["input"] is None:
        raise ValueError("Dataset inputs cannot be null.")
    if "metadata" in data:
        _object(data["metadata"])
    if "tags" in data:
        data["tags"] = _tag_names(data["tags"])
    _validate_json(data)
    return _encode(data)


def _tag_params(tags: Sequence[str] | None, tag_match: str) -> dict[str, str | int]:
    if tag_match not in ("any", "all"):
        raise ValueError("tag_match must be any or all.")
    params: dict[str, str | int] = {"tag_match": tag_match}
    if tags is not None:
        params["tags"] = json.dumps(_tag_names(tags, limit=20), ensure_ascii=False)
    return params
