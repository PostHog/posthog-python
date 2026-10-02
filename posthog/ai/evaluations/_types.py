from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping
from uuid import UUID, uuid4

from typing_extensions import TypedDict

from ._errors import EvaluationAPIError
from ._serialization import (
    JSONValue,
    _MAX_ITEM_BYTES,
    _MAX_RESULT_BYTES,
    _UNSET,
    _Unset,
    _boolean,
    _date,
    _encode,
    _identifier,
    _object,
    _payload,
    _response_count,
    _score,
    _timestamp,
    _uuid,
)

RunSource = Literal["ci", "local", "scheduled"]
ResultStatus = Literal["ok", "error", "skipped", "not_applicable"]
ScoreValue = bool | int | float | list[str]


class ExperimentOptions(TypedDict, total=False):
    """Optional experiment creation fields. Persist these for exact replay.

    UUID and start time are generated once if omitted. ``run_source`` defaults
    to unspecified. Expected counts include every outcome status. Dataset and
    application identifiers describe the original execution, not upload time.
    """

    id: str | UUID
    started_at: datetime | str
    run_source: RunSource | None
    expected_item_count: int | None
    expected_result_count: int | None
    suite_key: str | None
    dataset_source: str | None
    dataset_identifier: str | None
    dataset_revision_identifier: str | None
    dataset_revision_id: str | UUID | None
    application_version: str | None
    model_version: str | None
    prompt_version: str | None


class ResultOptions(TypedDict, total=False):
    """Score outcome, trace references, and optional result payload fields.

    ``value`` is required for ``ok`` (the default). ``error_code`` and
    ``error_message`` are permitted only for ``error``. Use either ``payload``
    or individual payload fields; omitted values differ from explicit None.
    """

    value: ScoreValue | None
    status: ResultStatus
    error_code: str | None
    reasoning: str | None
    error_message: str | None
    metadata: dict[str, JSONValue] | None
    evaluator_trace_id: str | None
    evaluated_at: datetime | str | None
    payload: dict[str, JSONValue]


@dataclass(frozen=True, init=False)
class EvaluationItem:
    """An immutable input/output execution, shared by all its scorer results.

    Construction is local and generates a UUID unless ``id`` is supplied.
    Payload values are copied as JSON. Omitted fields differ from explicit None.
    Use ``payload={}`` for an explicitly empty payload. Save ``to_dict()`` to
    restore exactly the same declaration after restarting the process.
    """

    id: str
    _json: bytes = field(repr=False)

    def __init__(
        self,
        *,
        id: str | UUID | None = None,
        input: JSONValue | _Unset = _UNSET,
        output: JSONValue | _Unset = _UNSET,
        expected_output: JSONValue | _Unset = _UNSET,
        metadata: dict[str, JSONValue] | None | _Unset = _UNSET,
        case_key: str | None = None,
        trial: str | None = None,
        dataset_item_identifier: str | None = None,
        dataset_item_version_identifier: str | None = None,
        dataset_item_version_id: str | UUID | None = None,
        application_trace_id: str | None = None,
        payload: dict[str, JSONValue] | _Unset = _UNSET,
    ) -> None:
        identity = _uuid(uuid4() if id is None else id)
        data: dict[str, Any] = {"id": identity}
        for key, value in {
            "case_key": case_key,
            "trial": trial,
            "dataset_item_identifier": dataset_item_identifier,
            "dataset_item_version_identifier": dataset_item_version_identifier,
            "application_trace_id": application_trace_id,
        }.items():
            if value is not None:
                data[key] = _identifier(value, key)
        if dataset_item_version_id is not None:
            data["dataset_item_version_id"] = _uuid(dataset_item_version_id)
        prepared = _payload(
            payload,
            {
                "input": input,
                "output": output,
                "expected_output": expected_output,
                "metadata": metadata,
            },
            _MAX_ITEM_BYTES,
        )
        if not isinstance(prepared, _Unset):
            data["payload"] = prepared
        object.__setattr__(self, "id", identity)
        object.__setattr__(self, "_json", _encode(data))

    def to_dict(self) -> dict[str, Any]:
        """Return a defensive, JSON-serializable copy of the complete declaration."""
        return json.loads(self._json)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvaluationItem:
        """Restore a saved declaration, including its required original UUID."""
        if "id" not in data or data["id"] is None:
            raise ValueError("Restoring an item requires its original id.")
        return cls(**dict(data))


@dataclass(frozen=True, init=False)
class EvaluationResult:
    """One scorer outcome for an item and an exact scorer-version UUID.

    ``ok`` means the evaluator succeeded; the scorer's pinned configuration
    determines whether the value passes. Other statuses contain no score.
    Pass an item object to declare it, or an item UUID already accepted by the
    experiment. Payloads and values are snapshotted during construction.
    """

    item: EvaluationItem | str
    scorer_version_id: str
    _json: bytes = field(repr=False)

    def __init__(
        self,
        *,
        item: EvaluationItem | str | UUID,
        scorer_version_id: str | UUID,
        value: ScoreValue | None = None,
        status: ResultStatus = "ok",
        error_code: str | None = None,
        reasoning: str | None | _Unset = _UNSET,
        error_message: str | None | _Unset = _UNSET,
        metadata: dict[str, JSONValue] | None | _Unset = _UNSET,
        evaluator_trace_id: str | None = None,
        evaluated_at: datetime | str | None = None,
        payload: dict[str, JSONValue] | _Unset = _UNSET,
    ) -> None:
        item_id = item.id if isinstance(item, EvaluationItem) else _uuid(item)
        version_id = _uuid(scorer_version_id)
        if status not in ("ok", "error", "skipped", "not_applicable"):
            raise ValueError("Unsupported result status.")
        if status != "ok" and value is not None:
            raise ValueError("Only an ok result may contain a score.")
        if status != "error" and error_code is not None:
            raise ValueError("Only an error result may contain an error_code.")
        data: dict[str, Any] = {
            "item_id": item_id,
            "scorer_version_id": version_id,
            "status": status,
        }
        if status == "ok":
            data["value"] = _score(value)
        if error_code is not None:
            data["error_code"] = _identifier(error_code, "error_code", 128)
        if evaluator_trace_id is not None:
            data["evaluator_trace_id"] = _identifier(
                evaluator_trace_id, "evaluator_trace_id"
            )
        if evaluated_at is not None:
            data["evaluated_at"] = _timestamp(evaluated_at)
        prepared = _payload(
            payload,
            {
                "reasoning": reasoning,
                "error_message": error_message,
                "metadata": metadata,
            },
            _MAX_RESULT_BYTES,
        )
        if not isinstance(prepared, _Unset):
            if status != "error" and prepared.get("error_message") is not None:
                raise ValueError("Only an error result may contain an error_message.")
            data["payload"] = prepared
        object.__setattr__(
            self, "item", item if isinstance(item, EvaluationItem) else item_id
        )
        object.__setattr__(self, "scorer_version_id", version_id)
        object.__setattr__(self, "_json", _encode(data))

    @property
    def item_id(self) -> str:
        """The stable item UUID used in this result's identity."""
        return self.item.id if isinstance(self.item, EvaluationItem) else self.item

    def to_dict(self) -> dict[str, Any]:
        """Return the result declaration; save the item's declaration separately."""
        return json.loads(self._json)

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, item: EvaluationItem | None = None
    ) -> EvaluationResult:
        """Restore a result, optionally attaching its original reusable item."""
        fields = dict(data)
        item_id = _uuid(fields.pop("item_id"))
        if item is not None and item.id != item_id:
            raise ValueError("The restored result and item IDs do not match.")
        return cls(item=item if item is not None else item_id, **fields)


@dataclass(frozen=True)
class ExperimentReceipt:
    """Server acknowledgment of creation or closure, including accepted counts."""

    id: str
    status: Literal["uploading", "completed", "failed"]
    created: bool
    started_at: datetime
    created_at: datetime
    finished_at: datetime | None
    expected_item_count: int | None
    expected_result_count: int | None
    accepted_item_count: int
    accepted_result_count: int


@dataclass(frozen=True)
class ItemReceipt:
    """Persisted item identity; ``created=False`` acknowledges an exact replay."""

    id: str
    created: bool
    accepted_at: datetime


@dataclass(frozen=True)
class ResultReceipt(ItemReceipt):
    """Persisted result UUID and the item/scorer-version pair it acknowledges."""

    item_id: str
    scorer_version_id: str


@dataclass(frozen=True)
class UploadReceipt:
    """Acknowledgment of one atomic HTTP upload, including duplicate records."""

    items: tuple[ItemReceipt, ...]
    results: tuple[ResultReceipt, ...]


@dataclass(frozen=True)
class BulkUploadReceipt:
    """Acknowledged chunks in input order. Separate chunks commit independently."""

    chunks: tuple[UploadReceipt, ...]


class BulkUploadError(EvaluationAPIError):
    """A stopped bulk upload with acknowledged chunks and reusable pending work.

    Retry ``pending_results`` using the same experiment. A persistence value of
    ``unknown`` means the failed chunk may have committed; exact replay is safe.
    ``failed_index`` is the index of its first result in the original sequence.
    """

    def __init__(
        self,
        cause: EvaluationAPIError,
        *,
        completed: BulkUploadReceipt,
        pending_results: tuple[EvaluationResult, ...],
        failed_index: int,
        failed_body: bytes,
    ) -> None:
        super().__init__(
            status=cause.status,
            code=cause.code,
            detail=cause.detail,
            attr=cause.attr,
            errors=cause.errors,
            response=cause.response,
            retry_after=cause.retry_after,
            persistence=cause.persistence,
        )
        self.completed = completed
        self.pending_results = pending_results
        self.failed_index = failed_index
        self._failed_body = failed_body

    @property
    def failed_chunk(self) -> dict[str, Any]:
        """A defensive copy of the exact request whose acknowledgment is missing."""
        return json.loads(self._failed_body)


def _experiment_receipt(data: dict[str, Any], identity: str) -> ExperimentReceipt:
    try:
        if _uuid(data["id"]) != identity or data["status"] not in (
            "uploading",
            "completed",
            "failed",
        ):
            raise ValueError("Mismatched experiment receipt.")
        return ExperimentReceipt(
            id=identity,
            status=data["status"],
            created=_boolean(data["created"]),
            started_at=_date(data["started_at"]),
            created_at=_date(data["created_at"]),
            finished_at=None
            if data["finished_at"] is None
            else _date(data["finished_at"]),
            expected_item_count=None
            if data["expected_item_count"] is None
            else _response_count(data["expected_item_count"]),
            expected_result_count=None
            if data["expected_result_count"] is None
            else _response_count(data["expected_result_count"]),
            accepted_item_count=_response_count(data["accepted_item_count"]),
            accepted_result_count=_response_count(data["accepted_result_count"]),
        )
    except (KeyError, TypeError, ValueError):
        raise EvaluationAPIError(
            code="invalid_response", persistence="unknown"
        ) from None


def _upload_receipt(
    data: dict[str, Any], results: tuple[EvaluationResult, ...]
) -> UploadReceipt:
    try:
        if not isinstance(data["items"], list) or not isinstance(data["results"], list):
            raise ValueError("Expected acknowledgment lists.")
        items = tuple(
            ItemReceipt(
                id=_uuid(_object(row)["id"]),
                created=_boolean(row["created"]),
                accepted_at=_date(row["accepted_at"]),
            )
            for row in data["items"]
        )
        outcomes = tuple(
            ResultReceipt(
                id=_uuid(_object(row)["id"]),
                created=_boolean(row["created"]),
                accepted_at=_date(row["accepted_at"]),
                item_id=_uuid(row["item_id"]),
                scorer_version_id=_uuid(row["scorer_version_id"]),
            )
            for row in data["results"]
        )
        if len(items) != len({item.id for item in items}) or {
            item.id for item in items
        } != {result.item_id for result in results}:
            raise ValueError("Incomplete item acknowledgments.")
        if [(result.item_id, result.scorer_version_id) for result in outcomes] != [
            (result.item_id, result.scorer_version_id) for result in results
        ]:
            raise ValueError("Incomplete result acknowledgments.")
        if len({result.id for result in outcomes}) != len(outcomes):
            raise ValueError("Duplicate result identities in acknowledgment.")
        return UploadReceipt(items=items, results=outcomes)
    except (KeyError, TypeError, ValueError):
        raise EvaluationAPIError(
            code="invalid_response", persistence="unknown"
        ) from None
