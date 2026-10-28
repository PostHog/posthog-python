"""Dataset wire validation shared by the synchronous and asynchronous clients."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Sequence, TypeVar, cast

from ._dataset_types import Dataset, DatasetItem, DatasetOrder, DatasetRevision
from ._serialization import (
    JSONValue,
    _Unset,
    _boolean,
    _date,
    _encode,
    _identifier,
    _integer,
    _next_offset,
    _object,
    _parse_response,
    _text,
    _timestamp,
    _uuid,
    _validate_json,
)
from ._tags import _response_tags, _tag_names, _tag_params

_T = TypeVar("_T")


def _json(value: Any) -> JSONValue:
    return cast(JSONValue, json.loads(_encode(value, sort_keys=False)))


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
    return _parse_response(
        parser, data, detail="The dataset API returned an invalid response."
    )


def _page(
    data: dict[str, Any], parser: Callable[[dict[str, Any]], _T], offset: int
) -> tuple[int, tuple[_T, ...], int | None]:
    rows = data["results"]
    if not isinstance(rows, list):
        raise ValueError("Expected a list of dataset results.")
    results = tuple(parser(_object(row)) for row in rows)
    count = _integer(data["count"], minimum=0)
    next_offset = _next_offset(data["next"], minimum=offset + 1)
    if next_offset is not None and not results:
        raise ValueError("An empty page must not have a continuation.")
    return count, results, next_offset


def _page_params(limit: int, offset: int, maximum: int) -> dict[str, str | int]:
    if _integer(limit) > maximum:
        raise ValueError(f"Page limit must be between 1 and {maximum}.")
    return {"limit": limit, "offset": _integer(offset, minimum=0)}


def _list_params(
    limit: int,
    offset: int,
    search: str | None,
    archived: bool,
    order_by: DatasetOrder,
    tags: Sequence[str] | None,
    tag_match: str,
) -> dict[str, str | int]:
    params = _page_params(limit, offset, 100)
    params.update(_tag_params(tags, tag_match))
    params["archived"] = "true" if _boolean(archived) else "false"
    if order_by not in ("created_at", "-created_at", "updated_at", "-updated_at"):
        raise ValueError("Unsupported dataset order.")
    params["order_by"] = order_by
    if search is not None:
        if not isinstance(search, str) or not search.strip():
            raise ValueError("search must be a nonempty string.")
        params["search"] = search
    return params


def _item_list_params(
    dataset_id: str, revision: int | None, archived: bool, limit: int, offset: int
) -> dict[str, str | int]:
    params = _page_params(limit, offset, 25)
    params["dataset"] = dataset_id
    params["archived"] = "true" if _boolean(archived) else "false"
    if revision is not None:
        params["revision"] = _integer(revision)
    return params


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


def _create_item_body(
    dataset_id: str,
    *,
    input: JSONValue,
    expected_output: JSONValue | _Unset,
    source_output: JSONValue | _Unset,
    metadata: dict[str, JSONValue] | _Unset,
    client_item_id: str | None,
    source_trace_id: str | None,
    source_event_id: str | None,
    source_timestamp: datetime | str | None,
) -> bytes:
    client_item_id = _identifier(client_item_id, "client_item_id")
    source_trace_id = _identifier(source_trace_id, "source_trace_id")
    source_event_id = _identifier(source_event_id, "source_event_id")
    if (source_trace_id is None) != (source_timestamp is None) or (
        source_trace_id is None and source_event_id is not None
    ):
        raise ValueError(
            "Source provenance requires both source_trace_id and source_timestamp."
        )
    return _body(
        dict(
            dataset=dataset_id,
            input=input,
            expected_output=expected_output,
            source_output=source_output,
            metadata=metadata,
            client_item_id=client_item_id,
            source_trace_id=source_trace_id,
            source_event_id=source_event_id,
            source_timestamp=None
            if source_timestamp is None
            else _timestamp(source_timestamp),
        )
    )
