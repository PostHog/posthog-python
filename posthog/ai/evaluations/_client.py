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

from ._errors import EvaluationAPIError
from ._scorers import AsyncScorers, Scorers
from ._serialization import (
    _MAX_ITEMS,
    _MAX_REQUEST_BYTES,
    _MAX_RESULTS,
    _count,
    _encode,
    _identifier,
    _timestamp,
    _uuid,
)
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
        if key == "run_source":
            if value is not None and value not in ("ci", "local", "scheduled"):
                raise ValueError("run_source must be ci, local, scheduled, or None.")
            data[key] = value
        elif key in ("expected_item_count", "expected_result_count"):
            data[key] = _count(value, key)
        elif key == "dataset_revision_id":
            data[key] = None if value is None else _uuid(value)
        else:
            data[key] = _identifier(value, key)
    return _encode(data)


@dataclass(frozen=True)
class _Chunk:
    start: int
    results: tuple[EvaluationResult, ...]
    body: bytes


def _upload_body(items: list[bytes], results: list[bytes]) -> bytes:
    return (
        b'{"items":[' + b",".join(items) + b'],"results":[' + b",".join(results) + b"]}"
    )


def _chunks(results: tuple[EvaluationResult, ...]) -> list[_Chunk]:
    # Validate the finite submission before sending any chunk. Receipts and
    # prepared bodies use memory proportional to this caller-sized sequence.
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

    chunks: list[_Chunk] = []
    item_ids: set[str] = set()
    items: list[bytes] = []
    encoded_results: list[bytes] = []
    start = 0
    size = len(_upload_body([], []))
    for index, result in enumerate(results):
        # Include declarations wherever their results occur. This makes every
        # chunk independently replayable even after a lost acknowledgment.
        item = (
            declarations.get(result.item_id) if result.item_id not in item_ids else None
        )
        extra = len(result._json) + bool(encoded_results)
        if item is not None:
            extra += len(item) + bool(items)
        if encoded_results and (
            len(encoded_results) >= _MAX_RESULTS
            or len(items) + (item is not None) > _MAX_ITEMS
            or size + extra > _MAX_REQUEST_BYTES
        ):
            chunks.append(
                _Chunk(
                    start, results[start:index], _upload_body(items, encoded_results)
                )
            )
            start, size = index, len(_upload_body([], []))
            item_ids, items, encoded_results = set(), [], []
            item = declarations.get(result.item_id)
            extra = len(result._json) + (len(item) if item is not None else 0)
        if size + extra > _MAX_REQUEST_BYTES:
            raise ValueError(
                "One result and its item exceed the upload request byte limit."
            )
        if item is not None:
            item_ids.add(result.item_id)
            items.append(item)
        encoded_results.append(result._json)
        size += extra
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
    """Upload offline evaluations and manage scorers in one project/environment.

    ``secret_key`` accepts personal or project secret keys with
    offline_evaluation_ingestion:write. Scorer reads/writes require a personal
    key with llm_analytics:read/write. The offline feature must be enabled.
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
