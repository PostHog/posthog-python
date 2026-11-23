from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from types import TracebackType
from typing import Any, Sequence
from uuid import UUID, uuid4

from typing_extensions import Unpack

from ._async_datasets import AsyncDatasets
from ._datasets import Datasets
from ._errors import EvaluationAPIError
from ._mutations import (
    ItemUpdate,
    ResultUpdate,
    _item_changes,
    _item_path,
    _item_payload_changes,
    _patch_body,
    _response_revision,
    _result_changes,
    _result_path,
    _result_payload_changes,
)
from ._scorers import AsyncScorers, Scorers
from ._serialization import (
    JSONValue,
    _MAX_ITEMS,
    _MAX_REQUEST_BYTES,
    _MAX_RESULTS,
    _count,
    _encode,
    _identifier,
    _integer,
    _timestamp,
    _uuid,
)
from ._tags import _response_tags, _tag_names
from ._transport import AsyncTransport, SyncTransport
from ._types import (
    BulkUploadError,
    BulkUploadReceipt,
    EvaluationItem,
    EvaluationResult,
    ExperimentOptions,
    ExperimentReceipt,
    ResultOptions,
    UploadReceipt,
    _experiment_receipt,
    _upload_receipt,
)


def _experiment_tags(data: dict[str, Any], identity: str) -> tuple[str, ...]:
    try:
        if _uuid(data["id"]) != identity:
            raise ValueError("Mismatched experiment ID.")
        return _response_tags(data["tags"])
    except (KeyError, TypeError, ValueError):
        raise EvaluationAPIError(
            code="invalid_response",
            detail="The experiment API returned invalid tag metadata.",
            response=data,
            persistence="unknown",
        ) from None


def _experiment_body(name: str, options: ExperimentOptions) -> bytes:
    allowed = set(ExperimentOptions.__annotations__)
    if set(options) - allowed:
        raise TypeError("Unsupported experiment option.")
    data: dict[str, Any] = {
        "name": _identifier(name, "name", 400),
        "id": _uuid(options.get("id", uuid4())),
        "started_at": _timestamp(options.get("started_at", datetime.now(timezone.utc))),
    }
    if name is None:
        raise ValueError("name is required.")
    for key, value in options.items():
        if key in ("id", "started_at"):
            continue
        data[key] = _experiment_option(key, value)
    return _encode(data)


def _experiment_option(key: str, value: Any) -> Any:
    if key == "run_source":
        if value is not None and value not in ("ci", "local", "scheduled"):
            raise ValueError("run_source must be ci, local, scheduled, or None.")
        return value
    elif key in ("expected_item_count", "expected_result_count"):
        return _count(value, key)
    elif key == "dataset_revision_id":
        return None if value is None else _uuid(value)
    else:
        return _identifier(value, key)


@dataclass(frozen=True)
class _Chunk:
    start: int
    results: tuple[EvaluationResult, ...]
    body: bytes


def _upload_body(items: list[bytes], results: list[bytes]) -> bytes:
    return (
        b'{"items":[' + b",".join(items) + b'],"results":[' + b",".join(results) + b"]}"
    )


def _result_declarations(results: tuple[EvaluationResult, ...]) -> dict[str, bytes]:
    identities: set[tuple[str, str]] = set()
    declarations: dict[str, bytes] = {}
    for result in results:
        if not isinstance(result, EvaluationResult):
            raise TypeError("upload_results expects EvaluationResult objects.")
        key = (result.item_id, result.scorer_version_id)
        if key in identities:
            raise ValueError(
                "Each item/scorer-version pair must occur only once in a bulk upload."
            )
        identities.add(key)
        if isinstance(result.item, EvaluationItem):
            previous = declarations.setdefault(result.item_id, result.item._json)
            if previous != result.item._json:
                raise ValueError("Conflicting declarations for the same item UUID.")

    return declarations


def _group_ends(results: tuple[EvaluationResult, ...]) -> list[int]:
    # A calculation only names a version, so ingestion-only clients cannot
    # discover its dependencies. Keep every supplied result for that item in
    # one request. Merge overlapping spans to retain the original order.
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    composite_items: set[str] = set()
    for index, result in enumerate(results):
        first.setdefault(result.item_id, index)
        last[result.item_id] = index + 1
        if result.calculate:
            composite_items.add(result.item_id)
    spans = {first[item_id]: last[item_id] for item_id in composite_items}
    ends: list[int] = []
    index = 0
    while index < len(results):
        end = index + 1
        while index < end:
            end = max(end, spans.get(index, index + 1))
            index += 1
        ends.append(end)
    return ends


def _group_extra(
    group_results: list[bytes],
    new_items: list[bytes],
    encoded_results: list[bytes],
    items: list[bytes],
) -> int:
    extra = sum(map(len, group_results)) + len(group_results) - (not encoded_results)
    if new_items:
        extra += sum(map(len, new_items)) + len(new_items) - (not items)
    return extra


def _encoded_group(
    group: tuple[EvaluationResult, ...], declarations: dict[str, bytes]
) -> tuple[list[bytes], dict[str, bytes]]:
    return (
        [result._json for result in group],
        {
            result.item_id: declarations[result.item_id]
            for result in group
            if result.item_id in declarations
        },
    )


def _group_fits(result_count: int, item_count: int, size: int) -> bool:
    return (
        result_count <= _MAX_RESULTS
        and item_count <= _MAX_ITEMS
        and size <= _MAX_REQUEST_BYTES
    )


def _chunks(results: tuple[EvaluationResult, ...]) -> list[_Chunk]:
    # Validate the finite submission before sending any chunk. Receipts and
    # prepared bodies use memory proportional to this caller-sized sequence.
    declarations = _result_declarations(results)
    chunks: list[_Chunk] = []
    item_ids: set[str] = set()
    items: list[bytes] = []
    encoded_results: list[bytes] = []
    start = 0
    size = len(_upload_body([], []))
    group_start = 0
    for end in _group_ends(results):
        group = results[group_start:end]
        group_results, group_items = _encoded_group(group, declarations)
        while True:
            new_items = [body for id, body in group_items.items() if id not in item_ids]
            extra = _group_extra(group_results, new_items, encoded_results, items)
            if _group_fits(
                len(encoded_results) + len(group),
                len(items) + len(new_items),
                size + extra,
            ):
                break
            if not encoded_results:
                raise ValueError(
                    "One result or composite item group exceeds the upload request limit."
                )
            chunks.append(
                _Chunk(
                    start,
                    results[start:group_start],
                    _upload_body(items, encoded_results),
                )
            )
            start, size = group_start, len(_upload_body([], []))
            item_ids, items, encoded_results = set(), [], []
        # Repeat available declarations in every request for exact recovery.
        item_ids.update(group_items)
        items.extend(new_items)
        encoded_results.extend(group_results)
        size += extra
        group_start = end
    if encoded_results:
        chunks.append(
            _Chunk(start, results[start:], _upload_body(items, encoded_results))
        )
    return chunks


class Experiment:
    """A project-bound experiment handle. Closing its client does not close the run.

    Calls return only after server acknowledgment. Complete after all intended
    uploads succeed; failed uploads remain resumable with the same identities.
    Operations on this handle are serialized, including all chunks of a bulk
    upload. Other handles and processes remain governed by server concurrency.
    """

    def __init__(
        self,
        transport: SyncTransport,
        project_id: int,
        id: str,
        *,
        receipt: ExperimentReceipt | None = None,
        submission: bytes | None = None,
    ) -> None:
        self._id = id
        self.receipt = receipt
        self._submission = submission
        self._transport = transport
        self._path = (
            f"/api/projects/{project_id}/ai_observability/offline_experiments/{id}/"
        )
        self._lock = RLock()

    @property
    def id(self) -> str:
        """Immutable experiment UUID used by every operation on this handle."""
        return self._id

    @property
    def submission(self) -> dict[str, Any] | None:
        """Copy of original creation fields; unavailable on a resumed handle."""
        return None if self._submission is None else json.loads(self._submission)

    def get_tags(self) -> tuple[str, ...]:
        """Fetch current tags using a personal key with ``evaluation:read``."""
        with self._lock:
            data = self._transport.request("GET", self._path)
            return _experiment_tags(data, self.id)

    def set_tags(self, tags: Sequence[str]) -> tuple[str, ...]:
        """Replace tags and return the server's normalized names; [] clears them.

        Requires a personal key with ``evaluation:write``. Tags can change after
        completion and are separate from the immutable creation ``submission``.
        Updates are not retried automatically: inspect tags after a lost response.
        """
        body = _encode({"tags": _tag_names(tags)})
        with self._lock:
            data = self._transport.request(
                "PATCH", self._path, body=body, retry_safe=False
            )
            return _experiment_tags(data, self.id)

    def get_item_revision(self, item_id: str | UUID) -> int:
        """Read the current revision shared by an item and its payload."""
        path = self._path + _item_path(item_id)
        with self._lock:
            data = self._transport.request("GET", path)
            return _response_revision(data, identity=item_id)

    def get_result_revision(self, result_id: str | UUID) -> int:
        """Read the current result revision from its payload endpoint."""
        path = self._path + _result_path(result_id) + "payload/"
        with self._lock:
            data = self._transport.request("GET", path)
            return _response_revision(data, identity=result_id)

    def _patch(self, path: str, revision: int, changes: dict[str, Any]) -> int:
        body = _patch_body(revision, changes)
        with self._lock:
            data = self._transport.request(
                "PATCH", self._path + path, body=body, retry_safe=False
            )
            return _response_revision(data, minimum=revision)

    def _delete(self, path: str, revision: int) -> None:
        params: dict[str, str | int] = {"revision": _integer(revision, minimum=0)}
        with self._lock:
            self._transport.request(
                "DELETE", self._path + path, params=params, retry_safe=False
            )

    def update_item(
        self, item_id: str | UUID, *, revision: int, **fields: Unpack[ItemUpdate]
    ) -> int:
        """Update item identifiers at the observed revision and return the current revision."""
        return self._patch(_item_path(item_id), revision, _item_changes(fields))

    def update_result(
        self, result_id: str | UUID, *, revision: int, **fields: Unpack[ResultUpdate]
    ) -> int:
        """Correct a reported score and return its revision. Dependent composites are recalculated."""
        return self._patch(_result_path(result_id), revision, _result_changes(fields))

    def update_item_payload(
        self, item_id: str | UUID, *, revision: int, payload: dict[str, JSONValue]
    ) -> int:
        """Patch input, output, expected_output, or metadata and return the item revision."""
        return self._patch(
            _item_path(item_id) + "payload/", revision, _item_payload_changes(payload)
        )

    def update_result_payload(
        self, result_id: str | UUID, *, revision: int, payload: dict[str, JSONValue]
    ) -> int:
        """Patch reasoning, error_message, or metadata and return the result revision."""
        return self._patch(
            _result_path(result_id) + "payload/",
            revision,
            _result_payload_changes(payload),
        )

    def delete_item(self, item_id: str | UUID, *, revision: int) -> None:
        """Permanently delete an item and all its results and payloads."""
        self._delete(_item_path(item_id), revision)

    def delete_result(self, result_id: str | UUID, *, revision: int) -> None:
        """Permanently delete a result, its payload, and dependent composites."""
        self._delete(_result_path(result_id), revision)

    def delete_item_payload(self, item_id: str | UUID, *, revision: int) -> None:
        """Permanently remove the stored payload while retaining the item and scores."""
        self._delete(_item_path(item_id) + "payload/", revision)

    def delete_result_payload(self, result_id: str | UUID, *, revision: int) -> None:
        """Permanently remove the stored payload while retaining its result."""
        self._delete(_result_path(result_id) + "payload/", revision)

    def upload_result(
        self,
        *,
        item: EvaluationItem | str | UUID,
        scorer_version_id: str | UUID,
        **options: Unpack[ResultOptions],
    ) -> UploadReceipt:
        """Persist one result and its optional item declaration, with a receipt.

        Use the same item object for multiple scorers. A UUID reference requires
        an already accepted item. ``ok`` is execution success, not score polarity.
        Raises EvaluationAPIError on rejection or an unknown persistence outcome.
        """
        result = EvaluationResult(
            item=item, scorer_version_id=scorer_version_id, **options
        )
        chunk = _chunks((result,))[0]
        with self._lock:
            data = self._transport.request(
                "POST", self._path + "upload/", body=chunk.body
            )
            return _upload_receipt(data, chunk.results)

    def upload_results(self, results: Sequence[EvaluationResult]) -> BulkUploadReceipt:
        """Eagerly upload a finite sequence, chunked by count and encoded bytes.

        Returns per-chunk receipts; an empty sequence makes no requests. Memory
        scales with the caller's sequence. For large datasets, call in batches.
        BulkUploadError exposes committed chunks and pending_results to retry.
        Items with calculations keep all their supplied results in one request.
        Interleaved items may form a larger indivisible group; an oversized
        group fails before any I/O. Group each item's results together when
        preparing large batches. Stored inputs require evaluation:read and
        llm_analytics:read; ingestion-only keys must supply every pinned input.
        """
        prepared = tuple(results)
        chunks = _chunks(prepared)
        receipts: list[UploadReceipt] = []
        with self._lock:
            for chunk in chunks:
                try:
                    data = self._transport.request(
                        "POST", self._path + "upload/", body=chunk.body
                    )
                    receipts.append(_upload_receipt(data, chunk.results))
                except EvaluationAPIError as exc:
                    raise BulkUploadError(
                        exc,
                        completed=BulkUploadReceipt(tuple(receipts)),
                        pending_results=prepared[chunk.start :],
                        failed_index=chunk.start,
                        failed_body=chunk.body,
                    ) from exc
        return BulkUploadReceipt(tuple(receipts))

    def _close(self, action: str) -> ExperimentReceipt:
        with self._lock:
            data = self._transport.request(
                "POST", self._path + action + "/", body=b"{}"
            )
            receipt = _experiment_receipt(data, self.id)
            if receipt.status != {"complete": "completed", "fail": "failed"}[action]:
                raise EvaluationAPIError(code="invalid_response", persistence="unknown")
            self.receipt = receipt
            return receipt

    def complete(self) -> ExperimentReceipt:
        """Close as completed. Server-declared expected counts must match.

        Repeating completion is safe. A count or terminal-state conflict raises
        EvaluationAPIError with the server's count/error details.
        """
        return self._close("complete")

    def fail(self) -> ExperimentReceipt:
        """Explicitly close as failed, retaining accepted data without count checks."""
        return self._close("fail")


class AsyncExperiment:
    """Async experiment handle with the same persistence contract as Experiment.

    Cancellation propagates and never closes the experiment. Replay the same
    items/results to recover an interrupted upload, including uncertain commits.
    Operations on a handle are serialized; use it on its owning event loop.
    """

    def __init__(
        self,
        transport: AsyncTransport,
        project_id: int,
        id: str,
        *,
        receipt: ExperimentReceipt | None = None,
        submission: bytes | None = None,
    ) -> None:
        self._id = id
        self.receipt = receipt
        self._submission = submission
        self._transport = transport
        self._path = (
            f"/api/projects/{project_id}/ai_observability/offline_experiments/{id}/"
        )
        self._lock = asyncio.Lock()

    @property
    def id(self) -> str:
        """Immutable experiment UUID used by every operation on this handle."""
        return self._id

    @property
    def submission(self) -> dict[str, Any] | None:
        """Copy of original creation fields; unavailable on a resumed handle."""
        return None if self._submission is None else json.loads(self._submission)

    async def get_tags(self) -> tuple[str, ...]:
        """Fetch current tags. See Experiment.get_tags for key requirements."""
        async with self._lock:
            data = await self._transport.request("GET", self._path)
            return _experiment_tags(data, self.id)

    async def set_tags(self, tags: Sequence[str]) -> tuple[str, ...]:
        """Replace tags. See Experiment.set_tags for permissions and recovery."""
        body = _encode({"tags": _tag_names(tags)})
        async with self._lock:
            data = await self._transport.request(
                "PATCH", self._path, body=body, retry_safe=False
            )
            return _experiment_tags(data, self.id)

    async def get_item_revision(self, item_id: str | UUID) -> int:
        """Read the current revision shared by an item and its payload."""
        path = self._path + _item_path(item_id)
        async with self._lock:
            data = await self._transport.request("GET", path)
            return _response_revision(data, identity=item_id)

    async def get_result_revision(self, result_id: str | UUID) -> int:
        """Read the current result revision from its payload endpoint."""
        path = self._path + _result_path(result_id) + "payload/"
        async with self._lock:
            data = await self._transport.request("GET", path)
            return _response_revision(data, identity=result_id)

    async def _patch(self, path: str, revision: int, changes: dict[str, Any]) -> int:
        body = _patch_body(revision, changes)
        async with self._lock:
            data = await self._transport.request(
                "PATCH", self._path + path, body=body, retry_safe=False
            )
            return _response_revision(data, minimum=revision)

    async def _delete(self, path: str, revision: int) -> None:
        params: dict[str, str | int] = {"revision": _integer(revision, minimum=0)}
        async with self._lock:
            await self._transport.request(
                "DELETE", self._path + path, params=params, retry_safe=False
            )

    async def update_item(
        self, item_id: str | UUID, *, revision: int, **fields: Unpack[ItemUpdate]
    ) -> int:
        """Update item identifiers at the observed revision and return the current revision."""
        return await self._patch(_item_path(item_id), revision, _item_changes(fields))

    async def update_result(
        self, result_id: str | UUID, *, revision: int, **fields: Unpack[ResultUpdate]
    ) -> int:
        """Correct a reported score and return its revision. Dependent composites are recalculated."""
        return await self._patch(
            _result_path(result_id), revision, _result_changes(fields)
        )

    async def update_item_payload(
        self, item_id: str | UUID, *, revision: int, payload: dict[str, JSONValue]
    ) -> int:
        """Patch input, output, expected_output, or metadata and return the item revision."""
        return await self._patch(
            _item_path(item_id) + "payload/", revision, _item_payload_changes(payload)
        )

    async def update_result_payload(
        self, result_id: str | UUID, *, revision: int, payload: dict[str, JSONValue]
    ) -> int:
        """Patch reasoning, error_message, or metadata and return the result revision."""
        return await self._patch(
            _result_path(result_id) + "payload/",
            revision,
            _result_payload_changes(payload),
        )

    async def delete_item(self, item_id: str | UUID, *, revision: int) -> None:
        """Permanently delete an item and all its results and payloads."""
        await self._delete(_item_path(item_id), revision)

    async def delete_result(self, result_id: str | UUID, *, revision: int) -> None:
        """Permanently delete a result, its payload, and dependent composites."""
        await self._delete(_result_path(result_id), revision)

    async def delete_item_payload(self, item_id: str | UUID, *, revision: int) -> None:
        """Permanently remove the stored payload while retaining the item and scores."""
        await self._delete(_item_path(item_id) + "payload/", revision)

    async def delete_result_payload(
        self, result_id: str | UUID, *, revision: int
    ) -> None:
        """Permanently remove the stored payload while retaining its result."""
        await self._delete(_result_path(result_id) + "payload/", revision)

    async def upload_result(
        self,
        *,
        item: EvaluationItem | str | UUID,
        scorer_version_id: str | UUID,
        **options: Unpack[ResultOptions],
    ) -> UploadReceipt:
        """Await persistence of one result. See Experiment.upload_result."""
        result = EvaluationResult(
            item=item, scorer_version_id=scorer_version_id, **options
        )
        chunk = _chunks((result,))[0]
        async with self._lock:
            data = await self._transport.request(
                "POST", self._path + "upload/", body=chunk.body
            )
            return _upload_receipt(data, chunk.results)

    async def upload_results(
        self, results: Sequence[EvaluationResult]
    ) -> BulkUploadReceipt:
        """Await sequential bounded uploads. See Experiment.upload_results."""
        prepared = tuple(results)
        chunks = _chunks(prepared)
        receipts: list[UploadReceipt] = []
        async with self._lock:
            for chunk in chunks:
                try:
                    data = await self._transport.request(
                        "POST", self._path + "upload/", body=chunk.body
                    )
                    receipts.append(_upload_receipt(data, chunk.results))
                except EvaluationAPIError as exc:
                    raise BulkUploadError(
                        exc,
                        completed=BulkUploadReceipt(tuple(receipts)),
                        pending_results=prepared[chunk.start :],
                        failed_index=chunk.start,
                        failed_body=chunk.body,
                    ) from exc
        return BulkUploadReceipt(tuple(receipts))

    async def _close(self, action: str) -> ExperimentReceipt:
        async with self._lock:
            data = await self._transport.request(
                "POST", self._path + action + "/", body=b"{}"
            )
            receipt = _experiment_receipt(data, self.id)
            if receipt.status != {"complete": "completed", "fail": "failed"}[action]:
                raise EvaluationAPIError(code="invalid_response", persistence="unknown")
            self.receipt = receipt
            return receipt

    async def complete(self) -> ExperimentReceipt:
        """Await completed closure, checking declared counts on the server."""
        return await self._close("complete")

    async def fail(self) -> ExperimentReceipt:
        """Await explicit failed closure, retaining all accepted data."""
        return await self._close("fail")


class OfflineEvaluations:
    """Upload offline evaluations and manage scorers and datasets in one project.

    ``secret_key`` accepts personal or project secret keys with
    offline_evaluation_ingestion:write. Scorer reads/writes require a personal
    key with llm_analytics:read/write; datasets require dataset:read/write.
    Editing accepted data requires an evaluation editor's personal key with
    evaluation:write. Result operations and item deletion also need scorer access.
    Each operation requires its corresponding product feature to be enabled.
    ``host`` defaults to the US app API; known ingestion hosts are mapped to
    their app hosts. Custom hosts are preserved. Requests use a 15 second
    timeout and at most three retries by default. Scorer creation/version bumps
    are never automatically retried. There is no background upload queue.
    """

    def __init__(
        self,
        *,
        project_id: int,
        secret_key: str,
        host: str | None = None,
        timeout: float = 15,
        max_retries: int = 3,
    ) -> None:
        self._transport = SyncTransport(
            project_id, secret_key, host, timeout, max_retries
        )
        self._project_id = project_id
        self.scorers = Scorers(self._transport, project_id)
        self.datasets = Datasets(self._transport, project_id)
        self._path = f"/api/projects/{project_id}/ai_observability/offline_experiments/"

    def create_experiment(
        self, *, name: str, **options: Unpack[ExperimentOptions]
    ) -> Experiment:
        """Create and acknowledge one execution; generate UUID/start time once.

        To resume creation, reuse id, started_at and every original field.
        EvaluationAPIError.submission includes the complete prepared request.
        Save explicit fields before calling if recovery from process death is
        required; this SDK does not persist upload state to disk.
        """
        body = _experiment_body(name, options)
        submission = json.loads(body)
        try:
            data = self._transport.request("POST", self._path, body=body)
            receipt = _experiment_receipt(data, submission["id"])
        except EvaluationAPIError as exc:
            exc.submission = submission
            raise
        return Experiment(
            self._transport,
            self._project_id,
            receipt.id,
            receipt=receipt,
            submission=body,
        )

    def resume_experiment(self, id: str | UUID) -> Experiment:
        """Return a local handle to an existing UUID, without a read request.

        Existence and permissions are checked by the next server operation.
        This does not recreate an experiment whose creation was never accepted.
        """
        return Experiment(self._transport, self._project_id, _uuid(id))

    def close(self) -> None:
        """Release HTTP connections. Experiments retain their server state."""
        self._transport.close()

    def __enter__(self) -> OfflineEvaluations:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class AsyncOfflineEvaluations:
    """Native async counterpart to OfflineEvaluations; requires posthog[async].

    Configuration, receipts and errors match the sync client. Use ``async with``
    or ``await aclose()`` to release connections. Cancellation does not close an
    experiment. Use each client on a single event loop.
    """

    def __init__(
        self,
        *,
        project_id: int,
        secret_key: str,
        host: str | None = None,
        timeout: float = 15,
        max_retries: int = 3,
    ) -> None:
        self._transport = AsyncTransport(
            project_id, secret_key, host, timeout, max_retries
        )
        self._project_id = project_id
        self.scorers = AsyncScorers(self._transport, project_id)
        self.datasets = AsyncDatasets(self._transport, project_id)
        self._path = f"/api/projects/{project_id}/ai_observability/offline_experiments/"

    async def create_experiment(
        self, *, name: str, **options: Unpack[ExperimentOptions]
    ) -> AsyncExperiment:
        """Await creation. See OfflineEvaluations.create_experiment for replay."""
        body = _experiment_body(name, options)
        submission = json.loads(body)
        try:
            data = await self._transport.request("POST", self._path, body=body)
            receipt = _experiment_receipt(data, submission["id"])
        except EvaluationAPIError as exc:
            exc.submission = submission
            raise
        return AsyncExperiment(
            self._transport,
            self._project_id,
            receipt.id,
            receipt=receipt,
            submission=body,
        )

    def resume_experiment(self, id: str | UUID) -> AsyncExperiment:
        """Construct a handle locally; no await or read permission is needed."""
        return AsyncExperiment(self._transport, self._project_id, _uuid(id))

    async def aclose(self) -> None:
        """Release async HTTP connections without changing experiment state."""
        await self._transport.aclose()

    async def __aenter__(self) -> AsyncOfflineEvaluations:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
