"""Dataset management and revision-pinned iteration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Literal, Sequence
from uuid import UUID

from ._dataset_serialization import (
    _body,
    _create_item_body,
    _dataset,
    _item,
    _item_list_params,
    _list_params,
    _page,
    _page_params,
    _parse,
    _revision,
)
from ._dataset_types import (
    Dataset,
    DatasetItem,
    DatasetItemPage,
    DatasetOrder,
    DatasetPage,
    DatasetRevisionPage,
)
from ._serialization import (
    JSONValue,
    _UNSET,
    _Unset,
    _integer,
    _uuid,
)
from ._transport import SyncTransport


class Datasets:
    """Manage datasets through ``OfflineEvaluations.datasets``.

    Requires a personal key with ``dataset:read`` or ``dataset:write`` and the
    datasets feature enabled. Mutations return after acknowledgment; only item
    creates with a caller-supplied ``client_item_id`` are automatically retried.
    Other lost write responses require inspecting state before retrying.
    """

    def __init__(self, transport: SyncTransport, project_id: int) -> None:
        self._transport = transport
        self._project_id = project_id
        self._base = f"/api/projects/{project_id}/datasets/"
        self._items = f"/api/projects/{project_id}/dataset_items/"

    def create(
        self,
        *,
        name: str,
        description: str | _Unset = _UNSET,
        metadata: dict[str, JSONValue] | _Unset = _UNSET,
        tags: Sequence[str] | _Unset = _UNSET,
    ) -> Dataset:
        """Create an empty dataset. Names must be unique within the project.

        No revision exists until the first item is created. This operation is
        not automatically retried, including after an ambiguous acknowledgment.
        """
        return _parse(
            lambda data: _dataset(data, self._project_id),
            self._transport.request(
                "POST",
                self._base,
                body=_body(
                    dict(
                        name=name, description=description, metadata=metadata, tags=tags
                    )
                ),
                retry_safe=False,
            ),
        )

    def get(self, dataset_id: str | UUID) -> Dataset:
        """Fetch current metadata, including both current revision identifiers."""
        identity = _uuid(dataset_id)
        return _parse(
            lambda data: _dataset(data, self._project_id, identity),
            self._transport.request("GET", f"{self._base}{identity}/"),
        )

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        archived: bool = False,
        order_by: DatasetOrder = "-created_at",
        tags: Sequence[str] | None = None,
        tag_match: Literal["any", "all"] = "any",
    ) -> DatasetPage:
        """Fetch one page of active datasets, or archived datasets if requested.

        Search matches names, descriptions, and metadata. Keep filters and pass
        ``next_offset`` to continue. Maximum limit is 100; tags accept 20 names.
        """
        params = _list_params(
            limit, offset, search, archived, order_by, tags, tag_match
        )
        return _parse(
            lambda data: DatasetPage(
                *_page(data, lambda row: _dataset(row, self._project_id), offset)
            ),
            self._transport.request("GET", self._base, params=params),
        )

    def update(
        self,
        dataset_id: str | UUID,
        *,
        name: str | _Unset = _UNSET,
        description: str | _Unset = _UNSET,
        metadata: dict[str, JSONValue] | _Unset = _UNSET,
        tags: Sequence[str] | _Unset = _UNSET,
    ) -> Dataset:
        """Replace supplied metadata fields; omitted fields remain unchanged.

        Use description="", metadata={}, or tags=[] to clear them. Metadata
        updates do not create revisions. Use archive/restore for archive state.
        """
        identity = _uuid(dataset_id)
        return _parse(
            lambda data: _dataset(data, self._project_id, identity),
            self._transport.request(
                "PATCH",
                f"{self._base}{identity}/",
                body=_body(
                    dict(
                        name=name, description=description, metadata=metadata, tags=tags
                    )
                ),
                retry_safe=False,
            ),
        )

    def archive(self, dataset_id: str | UUID) -> Dataset:
        """Archive a dataset. It stays readable but rejects item mutations."""
        identity = _uuid(dataset_id)
        return _parse(
            lambda data: _dataset(data, self._project_id, identity),
            self._transport.request(
                "POST", f"{self._base}{identity}/archive/", body=b"{}", retry_safe=False
            ),
        )

    def restore(self, dataset_id: str | UUID) -> Dataset:
        """Restore an archived dataset without changing individual item states."""
        identity = _uuid(dataset_id)
        return _parse(
            lambda data: _dataset(data, self._project_id, identity),
            self._transport.request(
                "POST", f"{self._base}{identity}/restore/", body=b"{}", retry_safe=False
            ),
        )

    def list_revisions(
        self, dataset_id: str | UUID, *, limit: int = 50, offset: int = 0
    ) -> DatasetRevisionPage:
        """List immutable revisions newest first. Maximum page limit is 100."""
        identity = _uuid(dataset_id)
        params = _page_params(limit, offset, 100)
        return _parse(
            lambda data: DatasetRevisionPage(
                *_page(
                    data, lambda row: _revision(row, self._project_id, identity), offset
                )
            ),
            self._transport.request(
                "GET", f"{self._base}{identity}/revisions/", params=params
            ),
        )

    def list_items(
        self,
        dataset_id: str | UUID,
        *,
        revision: int | None = None,
        archived: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> DatasetItemPage:
        """Fetch one item page, optionally at an exact numeric dataset revision.

        Omitted revision reads current state for this request. Use ``snapshot``
        for consistent multi-page evaluation reads. Archived items are excluded
        by default; archived=True selects only archived items. Maximum limit is 25.
        """
        identity = _uuid(dataset_id)
        params = _item_list_params(identity, revision, archived, limit, offset)
        return _parse(
            lambda data: DatasetItemPage(
                *_page(
                    data,
                    lambda row: _item(
                        row,
                        self._project_id,
                        dataset_id=identity,
                        revision=revision,
                        archived=archived,
                    ),
                    offset,
                )
            ),
            self._transport.request("GET", self._items, params=params),
        )

    def get_item(
        self, item_id: str | UUID, *, revision: int | None = None
    ) -> DatasetItem:
        """Fetch an item by stable UUID, optionally at a dataset revision number."""
        identity = _uuid(item_id)
        params: dict[str, str | int] = (
            {} if revision is None else {"revision": _integer(revision)}
        )
        return _parse(
            lambda data: _item(data, self._project_id, id=identity, revision=revision),
            self._transport.request("GET", f"{self._items}{identity}/", params=params),
        )

    def create_item(
        self,
        dataset_id: str | UUID,
        *,
        input: JSONValue,
        expected_output: JSONValue | _Unset = _UNSET,
        source_output: JSONValue | _Unset = _UNSET,
        metadata: dict[str, JSONValue] | _Unset = _UNSET,
        client_item_id: str | None = None,
        source_trace_id: str | None = None,
        source_event_id: str | None = None,
        source_timestamp: datetime | str | None = None,
    ) -> DatasetItem:
        """Add a case, creating its first version and a dataset revision.

        ``input`` accepts any non-null JSON value. Supply a stable, case-sensitive
        ``client_item_id`` to make identical creates retryable across processes.
        A matching key with different current content or an archived item raises
        a conflict. Source trace ID and timestamp must be supplied together;
        source event ID additionally identifies an event within that trace.
        """
        identity = _uuid(dataset_id)
        body = _create_item_body(
            identity,
            input=input,
            expected_output=expected_output,
            source_output=source_output,
            metadata=metadata,
            client_item_id=client_item_id,
            source_trace_id=source_trace_id,
            source_event_id=source_event_id,
            source_timestamp=source_timestamp,
        )
        return _parse(
            lambda data: _item(
                data, self._project_id, dataset_id=identity, archived=False
            ),
            self._transport.request(
                "POST", self._items, body=body, retry_safe=client_item_id is not None
            ),
        )

    def update_item(
        self,
        item_id: str | UUID,
        *,
        base_version: int,
        input: JSONValue | _Unset = _UNSET,
        expected_output: JSONValue | _Unset = _UNSET,
        metadata: dict[str, JSONValue] | _Unset = _UNSET,
    ) -> DatasetItem:
        """Update editable content using the observed item ``version``.

        Changed content creates a version and dataset revision; identical content
        is a no-op. Omit fields to preserve them, use expected_output=None to clear
        the answer, or metadata={} to clear metadata. Source fields are immutable.
        Stale versions raise a conflict without automatic refresh or retry.
        """
        identity = _uuid(item_id)
        body = _body(
            dict(
                base_version=_integer(base_version),
                input=input,
                expected_output=expected_output,
                metadata=metadata,
            )
        )
        return _parse(
            lambda data: _item(data, self._project_id, id=identity, archived=False),
            self._transport.request(
                "PATCH", f"{self._items}{identity}/", body=body, retry_safe=False
            ),
        )

    def archive_item(self, item_id: str | UUID, *, base_version: int) -> DatasetItem:
        """Archive an active item by creating a version; require its observed version."""
        identity = _uuid(item_id)
        body = _body(dict(base_version=_integer(base_version)))
        return _parse(
            lambda data: _item(data, self._project_id, id=identity, archived=True),
            self._transport.request(
                "POST", f"{self._items}{identity}/archive/", body=body, retry_safe=False
            ),
        )

    def restore_item(
        self,
        item_id: str | UUID,
        *,
        base_version: int,
        source_version: int | None = None,
    ) -> DatasetItem:
        """Restore an archived item, optionally copying a historical item version.

        ``base_version`` is the observed current version, even when copying an
        older ``source_version``. Restoration creates a new immutable version.
        """
        identity = _uuid(item_id)
        body = _body(
            dict(
                base_version=_integer(base_version),
                source_version=None
                if source_version is None
                else _integer(source_version),
            )
        )
        return _parse(
            lambda data: _item(data, self._project_id, id=identity, archived=False),
            self._transport.request(
                "POST", f"{self._items}{identity}/restore/", body=body, retry_safe=False
            ),
        )

    def list_item_versions(
        self, item_id: str | UUID, *, limit: int = 25, offset: int = 0
    ) -> DatasetItemPage:
        """List every item version, newest first. Maximum page limit is 100."""
        identity = _uuid(item_id)
        params = _page_params(limit, offset, 100)
        return _parse(
            lambda data: DatasetItemPage(
                *_page(
                    data, lambda row: _item(row, self._project_id, id=identity), offset
                )
            ),
            self._transport.request(
                "GET", f"{self._items}{identity}/versions/", params=params
            ),
        )

    def snapshot(
        self, dataset_id: str | UUID, *, revision: int | None = None
    ) -> DatasetSnapshot:
        """Pin a revision now; item iteration subsequently fetches that exact revision.

        Omit revision to resolve latest once. Historical revisions are resolved
        through paginated revision metadata. No items are fetched here. Raises
        ValueError for a never-populated dataset or a nonexistent revision.
        Keep the owning client open while iterating the returned snapshot.
        """
        if revision is not None:
            _integer(revision)
        dataset = self.get(dataset_id)
        selected = dataset.current_revision if revision is None else revision
        if selected is None:
            raise ValueError(
                "This dataset has no revision. Add an item before taking a snapshot."
            )
        if dataset.current_revision is None or selected > dataset.current_revision:
            raise ValueError(f"Dataset revision {selected} does not exist.")
        if (
            selected == dataset.current_revision
            and dataset.current_revision_id is not None
        ):
            return DatasetSnapshot(dataset, selected, dataset.current_revision_id, self)
        # Revisions are consecutive and newest first. Concurrent writes can
        # shift their offsets, so verify the result and scan on a miss.
        page = self.list_revisions(
            dataset.id, limit=1, offset=dataset.current_revision - selected
        )
        for candidate in page.results:
            if candidate.revision == selected:
                return DatasetSnapshot(dataset, selected, candidate.id, self)
        offset = 0
        while True:
            page = self.list_revisions(dataset.id, limit=100, offset=offset)
            for candidate in page.results:
                if candidate.revision == selected:
                    return DatasetSnapshot(dataset, selected, candidate.id, self)
            if page.next_offset is None or (
                page.results and page.results[-1].revision < selected
            ):
                raise ValueError(f"Dataset revision {selected} does not exist.")
            offset = page.next_offset


@dataclass(frozen=True)
class DatasetSnapshot:
    """A revision-pinned read handle owned by an open OfflineEvaluations client.

    ``dataset`` is metadata observed when resolving the snapshot; descriptive
    metadata is not historically versioned. Use ``revision_id`` when creating
    an experiment, not the introducing revision on an individual DatasetItem.
    Repeated iteration fetches the same active item versions without caching.
    """

    dataset: Dataset
    revision: int
    revision_id: str
    _datasets: Datasets = field(repr=False, compare=False)

    def iter_items(self, *, page_size: int = 25) -> Iterator[DatasetItem]:
        """Lazily fetch active items at this revision, at most 25 per request.

        Each iteration starts at the first page. Memory is bounded by one page;
        fetch failures propagate so callers cannot mistake a partial read for a
        complete dataset. An existing revision with no active items yields none.
        """
        _page_params(page_size, 0, 25)
        offset = 0
        while True:
            page = self._datasets.list_items(
                self.dataset.id, revision=self.revision, limit=page_size, offset=offset
            )
            yield from page.results
            if page.next_offset is None:
                return
            offset = page.next_offset
