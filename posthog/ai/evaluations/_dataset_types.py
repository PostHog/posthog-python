"""Typed dataset metadata, immutable version references, and paginated reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ._serialization import JSONValue

DatasetOrder = Literal["created_at", "-created_at", "updated_at", "-updated_at"]


@dataclass(frozen=True)
class Dataset:
    """A dataset's current metadata, separate from its immutable item revisions.

    Names are unique within a project. Use ``id`` for subsequent operations.
    Both revision fields are None until the first item is created. Metadata
    dictionaries are local copies, not live server state.
    """

    id: str
    name: str
    description: str
    metadata: dict[str, JSONValue] = field(repr=False)
    tags: tuple[str, ...]
    archived: bool
    current_revision: int | None
    current_revision_id: str | None
    created_at: datetime
    updated_at: datetime | None
    created_by: dict[str, Any] | None
    team_id: int


@dataclass(frozen=True)
class DatasetRevision:
    """An immutable dataset revision. Reads use ``revision``; experiments use ``id``."""

    id: str
    dataset_id: str
    revision: int
    created_at: datetime
    created_by: dict[str, Any] | None
    team_id: int


@dataclass(frozen=True)
class DatasetItem:
    """One version of a dataset case, with stable ``id`` and immutable ``version_id``.

    ``dataset_revision`` and ``dataset_revision_id`` identify the revision that
    introduced this version, which may precede the snapshot being read. Use
    the snapshot's revision ID when creating an experiment. ``source_output``
    is the original trace's output, not a new execution or expected answer.
    JSON values are local copies; changing them does not update the dataset.
    """

    id: str
    dataset_id: str
    client_item_id: str | None
    version: int
    version_id: str
    dataset_revision: int
    dataset_revision_id: str
    archived: bool
    input: JSONValue = field(repr=False)
    expected_output: JSONValue = field(repr=False)
    source_output: JSONValue = field(repr=False)
    metadata: dict[str, JSONValue] = field(repr=False)
    source_trace_id: str | None
    source_event_id: str | None
    source_timestamp: datetime | None
    created_at: datetime
    updated_at: datetime | None
    created_by: dict[str, Any] | None
    version_created_at: datetime
    version_created_by: dict[str, Any] | None
    team_id: int


@dataclass(frozen=True)
class DatasetPage:
    """One dataset page. Keep filters and pass ``next_offset`` to continue."""

    count: int
    results: tuple[Dataset, ...]
    next_offset: int | None


@dataclass(frozen=True)
class DatasetRevisionPage:
    """One page of revisions, newest first. None ``next_offset`` ends the list."""

    count: int
    results: tuple[DatasetRevision, ...]
    next_offset: int | None


@dataclass(frozen=True)
class DatasetItemPage:
    """One page of items or item history. None ``next_offset`` ends the list.

    Keep the dataset, revision, and archive filter unchanged when continuing
    an item listing. For a complete pinned read, prefer ``snapshot.iter_items``.
    """

    count: int
    results: tuple[DatasetItem, ...]
    next_offset: int | None
