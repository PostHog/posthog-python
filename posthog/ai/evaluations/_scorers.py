"""Scorer management, with identical synchronous and asynchronous interfaces."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any, Callable, Literal, TypeVar, cast, overload
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from ._errors import EvaluationAPIError
from ._scorer_types import (
    BooleanScorerConfig,
    CategoricalScorerConfig,
    NumericScorerConfig,
    Scorer,
    ScorerConfig,
    ScorerKind,
    ScorerOrder,
    ScorerPage,
    ScorerVersion,
    ScorerVersionPage,
)
from ._transport import AsyncTransport, SyncTransport


class _Omitted:
    def __repr__(self) -> str:
        return "<omitted>"


_OMITTED = _Omitted()
_ResultT = TypeVar("_ResultT")
_ConfigT = TypeVar("_ConfigT", bound=ScorerConfig)


def _uuid(value: str | UUID) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise ValueError("Scorer and version IDs must be UUIDs.")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("Scorer and version IDs must be UUIDs.") from None


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Expected an integer within the supported range.")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string.")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _kind(value: Any) -> ScorerKind:
    if value not in ("boolean", "numeric", "categorical"):
        raise ValueError("Unsupported scorer kind.")
    return cast(ScorerKind, value)


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Expected a timestamp with a timezone.")
    return parsed


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object.")
    return copy.deepcopy(value)


def _scorer(data: dict[str, Any]) -> Scorer[ScorerConfig]:
    archived = data["archived"]
    if not isinstance(archived, bool):
        raise ValueError("Expected boolean archived state.")
    version_id = data["current_version_id"]
    created_by = data["created_by"]
    return Scorer(
        id=_uuid(data["id"]),
        name=_text(data["name"]),
        description=_text(data["description"]),
        kind=_kind(data["kind"]),
        archived=archived,
        current_version=_integer(data["current_version"], minimum=1),
        current_version_id=_uuid(version_id),
        config=cast(ScorerConfig, _mapping(data["config"])),
        created_at=_timestamp(data["created_at"]),
        updated_at=_timestamp(data["updated_at"]),
        created_by=None if created_by is None else _mapping(created_by),
        team=_integer(data["team"], minimum=1),
    )


def _version(data: dict[str, Any]) -> ScorerVersion[ScorerConfig]:
    created_by = data["created_by"]
    return ScorerVersion(
        id=_uuid(data["id"]),
        definition_id=_uuid(data["definition_id"]),
        version=_integer(data["version"], minimum=1),
        kind=_kind(data["kind"]),
        config=cast(ScorerConfig, _mapping(data["config"])),
        created_at=_timestamp(data["created_at"]),
        created_by=None if created_by is None else _mapping(created_by),
    )


def _results(data: dict[str, Any]) -> list[dict[str, Any]]:
    results = data["results"]
    if not isinstance(results, list) or any(
        not isinstance(row, dict) for row in results
    ):
        raise ValueError("Expected a list of objects.")
    return results


def _scorer_page(data: dict[str, Any]) -> ScorerPage:
    next_url = _optional_text(data["next"])
    next_offset = None
    if next_url is not None:
        offsets = parse_qs(urlsplit(next_url).query).get("offset")
        if offsets is None or len(offsets) != 1:
            raise ValueError("Missing scorer page offset.")
        next_offset = _integer(int(offsets[0]))
    return ScorerPage(
        count=_integer(data["count"]),
        results=tuple(_scorer(row) for row in _results(data)),
        next=next_url,
        previous=_optional_text(data["previous"]),
        next_offset=next_offset,
    )


def _version_page(data: dict[str, Any]) -> ScorerVersionPage:
    return ScorerVersionPage(
        count=_integer(data["count"]),
        results=tuple(_version(row) for row in _results(data)),
        next_cursor=_optional_text(data["next_cursor"]),
    )


def _parse(
    parser: Callable[[dict[str, Any]], _ResultT], data: dict[str, Any]
) -> _ResultT:
    try:
        return parser(data)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise EvaluationAPIError(
            code="invalid_response",
            detail="The scorer API returned an invalid response.",
            response=data,
            persistence="unknown",
        ) from None


def _encode(data: dict[str, Any]) -> bytes:
    # Preparing bytes before transport I/O snapshots nested caller-owned values.
    return json.dumps(
        data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _create_body(
    name: str,
    kind: ScorerKind,
    config: ScorerConfig,
    description: str | None | _Omitted,
) -> bytes:
    data: dict[str, Any] = {
        "name": name,
        "kind": _kind(kind),
        "config": _mapping(config),
    }
    if not isinstance(description, _Omitted):
        data["description"] = description
    return _encode(data)


def _metadata(
    name: str | _Omitted,
    description: str | None | _Omitted,
    archived: bool | _Omitted = _OMITTED,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "name": name,
            "description": description,
            "archived": archived,
        }.items()
        if not isinstance(value, _Omitted)
    }


def _version_body(
    config: ScorerConfig,
    base_version: int | None,
    name: str | _Omitted,
    description: str | None | _Omitted,
) -> bytes:
    data = _metadata(name, description)
    data["config"] = _mapping(config)
    if base_version is not None:
        data["base_version"] = _integer(base_version, minimum=1)
    return _encode(data)


def _list_params(
    limit: int,
    offset: int,
    search: str | None,
    kind: ScorerKind | None,
    archived: bool | None,
    order_by: ScorerOrder | None,
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "limit": _integer(limit, minimum=1),
        "offset": _integer(offset),
    }
    if search is not None:
        params["search"] = search
    if kind is not None:
        params["kind"] = _kind(kind)
    if archived is not None:
        if not isinstance(archived, bool):
            raise ValueError("archived must be a boolean.")
        params["archived"] = "true" if archived else "false"
    if order_by is not None:
        params["order_by"] = order_by
    return params


def _version_params(limit: int, cursor: str | None) -> dict[str, str | int]:
    if _integer(limit, minimum=1) > 100:
        raise ValueError("Version page limit must be between 1 and 100.")
    params: dict[str, str | int] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    return params


class Scorers:
    """Manage scorers using a personal key with ``llm_analytics`` scopes.

    Access through ``OfflineEvaluations.scorers``. Creating a scorer or version
    is not idempotent: mutations are never retried automatically. If a response
    is lost, inspect server state before repeating the operation.
    """

    def __init__(self, transport: SyncTransport, project_id: int) -> None:
        self._transport = transport
        self._base = f"/api/projects/{project_id}/llm_analytics/score_definitions/"

    def _path(self, scorer_id: str | UUID) -> str:
        return f"{self._base}{_uuid(scorer_id)}/"

    @overload
    def create(
        self,
        *,
        name: str,
        kind: Literal["boolean"],
        config: BooleanScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[BooleanScorerConfig]: ...

    @overload
    def create(
        self,
        *,
        name: str,
        kind: Literal["numeric"],
        config: NumericScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[NumericScorerConfig]: ...

    @overload
    def create(
        self,
        *,
        name: str,
        kind: Literal["categorical"],
        config: CategoricalScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[CategoricalScorerConfig]: ...

    def create(
        self,
        *,
        name: str,
        kind: ScorerKind,
        config: ScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[ScorerConfig]:
        """Create an active scorer and its first immutable configuration version."""
        return _parse(
            _scorer,
            self._transport.request(
                "POST",
                self._base,
                body=_create_body(name, kind, config, description),
                retry_safe=False,
            ),
        )

    def get(self, scorer_id: str | UUID) -> Scorer[ScorerConfig]:
        """Fetch a scorer by its stable UUID, including ``current_version_id``."""
        return _parse(_scorer, self._transport.request("GET", self._path(scorer_id)))

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        kind: ScorerKind | None = None,
        archived: bool | None = None,
        order_by: ScorerOrder | None = None,
    ) -> ScorerPage:
        """Fetch one page. Omit ``archived`` to list active scorers.

        Keep the same filters and pass the returned ``next_offset`` to continue.
        Search matches names and descriptions; scorer names are not unique.
        """
        return _parse(
            _scorer_page,
            self._transport.request(
                "GET",
                self._base,
                params=_list_params(limit, offset, search, kind, archived, order_by),
            ),
        )

    def update(
        self,
        scorer_id: str | UUID,
        *,
        name: str | _Omitted = _OMITTED,
        description: str | None | _Omitted = _OMITTED,
        archived: bool | _Omitted = _OMITTED,
    ) -> Scorer[ScorerConfig]:
        """Update metadata. Use ``create_version`` to change configuration.

        Omitted fields are preserved. An explicit null description clears it.
        """
        return _parse(
            _scorer,
            self._transport.request(
                "PATCH",
                self._path(scorer_id),
                body=_encode(_metadata(name, description, archived)),
                retry_safe=False,
            ),
        )

    def create_version(
        self,
        scorer_id: str | UUID,
        *,
        config: _ConfigT,
        base_version: int | None = None,
        name: str | _Omitted = _OMITTED,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[_ConfigT]:
        """Create an immutable version and return the updated scorer.

        Pass the observed ``current_version`` as ``base_version`` to detect
        concurrent writes. Conflicts are surfaced without automatic refresh.
        Identical configuration is allowed, for example after a prompt change.
        """
        return cast(
            Scorer[_ConfigT],
            _parse(
                _scorer,
                self._transport.request(
                    "POST",
                    self._path(scorer_id) + "new_version/",
                    body=_version_body(config, base_version, name, description),
                    retry_safe=False,
                ),
            ),
        )

    def get_version(
        self, scorer_id: str | UUID, version_id: str | UUID
    ) -> ScorerVersion[ScorerConfig]:
        """Fetch an exact version UUID belonging to the specified scorer."""
        return _parse(
            _version,
            self._transport.request(
                "GET", self._path(scorer_id) + f"versions/{_uuid(version_id)}/"
            ),
        )

    def list_versions(
        self, scorer_id: str | UUID, *, limit: int = 50, cursor: str | None = None
    ) -> ScorerVersionPage:
        """Fetch versions newest first, using the returned cursor to continue."""
        return _parse(
            _version_page,
            self._transport.request(
                "GET",
                self._path(scorer_id) + "versions/",
                params=_version_params(limit, cursor),
            ),
        )


class AsyncScorers:
    """Async counterpart of :class:`Scorers`, exposed by AsyncOfflineEvaluations."""

    def __init__(self, transport: AsyncTransport, project_id: int) -> None:
        self._transport = transport
        self._base = f"/api/projects/{project_id}/llm_analytics/score_definitions/"

    def _path(self, scorer_id: str | UUID) -> str:
        return f"{self._base}{_uuid(scorer_id)}/"

    @overload
    async def create(
        self,
        *,
        name: str,
        kind: Literal["boolean"],
        config: BooleanScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[BooleanScorerConfig]: ...

    @overload
    async def create(
        self,
        *,
        name: str,
        kind: Literal["numeric"],
        config: NumericScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[NumericScorerConfig]: ...

    @overload
    async def create(
        self,
        *,
        name: str,
        kind: Literal["categorical"],
        config: CategoricalScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[CategoricalScorerConfig]: ...

    async def create(
        self,
        *,
        name: str,
        kind: ScorerKind,
        config: ScorerConfig,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[ScorerConfig]:
        """Create an active scorer and its first version; never retry mutations."""
        return _parse(
            _scorer,
            await self._transport.request(
                "POST",
                self._base,
                body=_create_body(name, kind, config, description),
                retry_safe=False,
            ),
        )

    async def get(self, scorer_id: str | UUID) -> Scorer[ScorerConfig]:
        """Fetch a scorer by stable UUID, including its current version UUID."""
        return _parse(
            _scorer, await self._transport.request("GET", self._path(scorer_id))
        )

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
        kind: ScorerKind | None = None,
        archived: bool | None = None,
        order_by: ScorerOrder | None = None,
    ) -> ScorerPage:
        """Fetch one page; preserve filters and use ``next_offset`` to continue."""
        return _parse(
            _scorer_page,
            await self._transport.request(
                "GET",
                self._base,
                params=_list_params(limit, offset, search, kind, archived, order_by),
            ),
        )

    async def update(
        self,
        scorer_id: str | UUID,
        *,
        name: str | _Omitted = _OMITTED,
        description: str | None | _Omitted = _OMITTED,
        archived: bool | _Omitted = _OMITTED,
    ) -> Scorer[ScorerConfig]:
        """Update metadata; omission preserves values and null clears description."""
        return _parse(
            _scorer,
            await self._transport.request(
                "PATCH",
                self._path(scorer_id),
                body=_encode(_metadata(name, description, archived)),
                retry_safe=False,
            ),
        )

    async def create_version(
        self,
        scorer_id: str | UUID,
        *,
        config: _ConfigT,
        base_version: int | None = None,
        name: str | _Omitted = _OMITTED,
        description: str | None | _Omitted = _OMITTED,
    ) -> Scorer[_ConfigT]:
        """Create a version; ``base_version`` detects concurrent configuration edits.

        Returns the updated scorer. Identical configuration is allowed.
        """
        return cast(
            Scorer[_ConfigT],
            _parse(
                _scorer,
                await self._transport.request(
                    "POST",
                    self._path(scorer_id) + "new_version/",
                    body=_version_body(config, base_version, name, description),
                    retry_safe=False,
                ),
            ),
        )

    async def get_version(
        self, scorer_id: str | UUID, version_id: str | UUID
    ) -> ScorerVersion[ScorerConfig]:
        """Fetch an exact immutable version by scorer and version UUIDs."""
        return _parse(
            _version,
            await self._transport.request(
                "GET", self._path(scorer_id) + f"versions/{_uuid(version_id)}/"
            ),
        )

    async def list_versions(
        self, scorer_id: str | UUID, *, limit: int = 50, cursor: str | None = None
    ) -> ScorerVersionPage:
        """Fetch versions newest first, using the returned cursor to continue."""
        return _parse(
            _version_page,
            await self._transport.request(
                "GET",
                self._path(scorer_id) + "versions/",
                params=_version_params(limit, cursor),
            ),
        )
