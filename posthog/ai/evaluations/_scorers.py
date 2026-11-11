"""Scorer management, with identical synchronous and asynchronous interfaces."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any, Callable, Literal, Sequence, TypeVar, cast, overload
from uuid import UUID

from ._scorer_types import (
    BooleanScorerConfig,
    CategoricalScorerConfig,
    NumericScorerConfig,
    Scorer,
    ScorerCalculation,
    ScorerComposition,
    ScorerConfig,
    ScorerInput,
    ScorerKind,
    ScorerOrder,
    ScorerPage,
    ScorerVersion,
    ScorerVersionPage,
)
from ._serialization import _integer, _next_offset, _parse_response, _text
from ._tags import _response_tags, _tag_names, _tag_params
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


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _kind(value: Any) -> ScorerKind:
    if value not in ("boolean", "numeric", "categorical"):
        raise ValueError("Unsupported scorer kind.")
    return cast(ScorerKind, value)


def _calculation(value: Any) -> ScorerCalculation:
    if value not in ("reported", "composite"):
        raise ValueError("Unsupported scorer calculation.")
    return cast(ScorerCalculation, value)


def _alias(value: Any) -> str:
    value = _text(value)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) or value in {
        "number",
        "min",
        "max",
        "abs",
        "and",
        "or",
        "not",
        "if",
        "else",
        "in",
        "is",
        "lambda",
        "for",
        "await",
    }:
        raise ValueError("Invalid composite input alias.")
    return value


def _composition(value: Any) -> ScorerComposition:
    data = _mapping(value)
    expression = _text(data.get("expression"))
    if not expression.strip() or len(expression) > 1024:
        raise ValueError("A formula must contain 1–1024 characters.")
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 16:
        raise ValueError("A composite requires 1–16 reported inputs.")
    prepared = [
        {
            "alias": _alias(_mapping(row).get("alias")),
            "scorer_version_id": _uuid(row.get("scorer_version_id")),
        }
        for row in inputs
    ]
    if len({row["alias"] for row in prepared}) != len(prepared):
        raise ValueError("Composite input aliases must be unique.")
    return cast(ScorerComposition, {"expression": expression, "inputs": prepared})


def _scoring(
    data: dict[str, Any],
) -> tuple[ScorerCalculation, ScorerComposition | None]:
    calculation = _calculation(data.get("calculation", "reported"))
    composition = data.get("composition")
    if calculation == "composite":
        return calculation, _composition(composition)
    if composition is not None:
        raise ValueError("Reported scorers cannot contain a composition.")
    return calculation, None


def _input_scorers(value: Any) -> tuple[ScorerInput, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected input scorer metadata.")
    return tuple(
        ScorerInput(
            alias=_alias(_mapping(row)["alias"]),
            scorer_version_id=_uuid(row["scorer_version_id"]),
            definition_id=_uuid(row["definition_id"]),
            name=_text(row["name"]),
            kind=_kind(row["kind"]),
            version=_integer(row["version"], minimum=1),
            config=cast(ScorerConfig, _mapping(row["config"])),
            latest_version_id=None
            if row["latest_version_id"] is None
            else _uuid(row["latest_version_id"]),
            latest_version=None
            if row["latest_version"] is None
            else _integer(row["latest_version"], minimum=1),
        )
        for row in value
    )


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
    calculation, composition = _scoring(data)
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
        tags=_response_tags(data.get("tags", [])),
        calculation=calculation,
        composition=composition,
        input_scorers=_input_scorers(data.get("input_scorers", [])),
        dependent_composite_count=_integer(
            data.get("dependent_composite_count", 0), minimum=0
        ),
    )


def _version(data: dict[str, Any]) -> ScorerVersion[ScorerConfig]:
    calculation, composition = _scoring(data)
    created_by = data["created_by"]
    return ScorerVersion(
        id=_uuid(data["id"]),
        definition_id=_uuid(data["definition_id"]),
        version=_integer(data["version"], minimum=1),
        kind=_kind(data["kind"]),
        config=cast(ScorerConfig, _mapping(data["config"])),
        created_at=_timestamp(data["created_at"]),
        created_by=None if created_by is None else _mapping(created_by),
        calculation=calculation,
        composition=composition,
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
    return ScorerPage(
        count=_integer(data["count"], minimum=0),
        results=tuple(_scorer(row) for row in _results(data)),
        next=next_url,
        previous=_optional_text(data["previous"]),
        next_offset=_next_offset(next_url),
    )


def _version_page(data: dict[str, Any]) -> ScorerVersionPage:
    return ScorerVersionPage(
        count=_integer(data["count"], minimum=0),
        results=tuple(_version(row) for row in _results(data)),
        next_cursor=_optional_text(data["next_cursor"]),
    )


def _parse(
    parser: Callable[[dict[str, Any]], _ResultT], data: dict[str, Any]
) -> _ResultT:
    return _parse_response(
        parser, data, detail="The scorer API returned an invalid response."
    )


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
    tags: Sequence[str] | _Omitted,
    calculation: ScorerCalculation,
    composition: ScorerComposition | None,
) -> bytes:
    data: dict[str, Any] = {
        "name": name,
        "kind": _kind(kind),
        "config": _mapping(config),
    }
    if _calculation(calculation) == "composite":
        data["calculation"] = calculation
        data["composition"] = _composition(composition)
        _composite_config(kind, config)
    elif composition is not None:
        raise ValueError("Only composite scorers accept a composition.")
    if not isinstance(description, _Omitted):
        data["description"] = description
    if not isinstance(tags, _Omitted):
        data["tags"] = _tag_names(tags)
    return _encode(data)


def _metadata(
    name: str | _Omitted,
    description: str | None | _Omitted,
    archived: bool | _Omitted = _OMITTED,
    tags: Sequence[str] | _Omitted = _OMITTED,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        key: value
        for key, value in {
            "name": name,
            "description": description,
            "archived": archived,
        }.items()
        if not isinstance(value, _Omitted)
    }
    if not isinstance(tags, _Omitted):
        data["tags"] = _tag_names(tags)
    return data


def _version_body(
    config: ScorerConfig,
    base_version: int | None,
    name: str | _Omitted,
    description: str | None | _Omitted,
    composition: ScorerComposition | None,
) -> bytes:
    data = _metadata(name, description)
    data["config"] = _mapping(config)
    if composition is not None:
        data["composition"] = _composition(composition)
        if config.get("step") is not None:
            raise ValueError("Numeric composite scorers do not accept a step.")
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
    tags: Sequence[str] | None,
    tag_match: Literal["any", "all"],
    calculation: ScorerCalculation | None,
    input_definition_id: str | UUID | None,
) -> dict[str, str | int]:
    tag_params = _tag_params(tags, tag_match)
    params: dict[str, str | int] = {
        "limit": _integer(limit, minimum=1),
        "offset": _integer(offset, minimum=0),
    }
    if search is not None:
        params["search"] = search
    if kind is not None:
        params["kind"] = _kind(kind)
    if calculation is not None:
        params["calculation"] = _calculation(calculation)
    if input_definition_id is not None:
        params["input_definition_id"] = _uuid(input_definition_id)
    if archived is not None:
        if not isinstance(archived, bool):
            raise ValueError("archived must be a boolean.")
        params["archived"] = "true" if archived else "false"
    if order_by is not None:
        params["order_by"] = order_by
    params.update(tag_params)
    return params


def _composite_config(kind: ScorerKind, config: ScorerConfig) -> None:
    if kind == "numeric" and config.get("step") is not None:
        raise ValueError("Numeric composite scorers do not accept a step.")


def _validation_body(
    kind: ScorerKind, composition: ScorerComposition, config: ScorerConfig | None
) -> bytes:
    data: dict[str, Any] = {
        "kind": _kind(kind),
        "composition": _composition(composition),
    }
    if config is not None:
        data["config"] = _mapping(config)
        _composite_config(kind, config)
    return _encode(data)


def _validation(data: dict[str, Any]) -> bool:
    if data.get("valid") is not True:
        raise ValueError("Expected successful formula validation.")
    return True


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
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[BooleanScorerConfig]: ...

    @overload
    def create(
        self,
        *,
        name: str,
        kind: Literal["numeric"],
        config: NumericScorerConfig,
        description: str | None | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[NumericScorerConfig]: ...

    @overload
    def create(
        self,
        *,
        name: str,
        kind: Literal["categorical"],
        config: CategoricalScorerConfig,
        description: str | None | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[CategoricalScorerConfig]: ...

    def create(
        self,
        *,
        name: str,
        kind: ScorerKind,
        config: ScorerConfig,
        description: str | None | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[ScorerConfig]:
        """Create a scorer and its first version, optionally assigning tags.

        Tag names are trimmed, lowercased, deduplicated, and sorted by the server.
        Set calculation="composite" with a composition to calculate item scores
        on the server. Calculation and output kind cannot change after creation.
        """
        return _parse(
            _scorer,
            self._transport.request(
                "POST",
                self._base,
                body=_create_body(
                    name, kind, config, description, tags, calculation, composition
                ),
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
        tags: Sequence[str] | None = None,
        tag_match: Literal["any", "all"] = "any",
        calculation: ScorerCalculation | None = None,
        input_definition_id: str | UUID | None = None,
    ) -> ScorerPage:
        """Fetch one page. Omit ``archived`` to list active scorers.

        Keep the same filters and pass the returned ``next_offset`` to continue.
        Search matches names and descriptions; scorer names are not unique.
        ``tags`` accepts up to 20 names. ``tag_match`` matches any (default) or
        all selected tags; an empty sequence disables tag filtering.
        Use calculation to select reported/composite scorers, and
        input_definition_id to find composites depending on a source scorer.
        """
        return _parse(
            _scorer_page,
            self._transport.request(
                "GET",
                self._base,
                params=_list_params(
                    limit,
                    offset,
                    search,
                    kind,
                    archived,
                    order_by,
                    tags,
                    tag_match,
                    calculation,
                    input_definition_id,
                ),
            ),
        )

    def update(
        self,
        scorer_id: str | UUID,
        *,
        name: str | _Omitted = _OMITTED,
        description: str | None | _Omitted = _OMITTED,
        archived: bool | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
    ) -> Scorer[ScorerConfig]:
        """Update metadata. Use ``create_version`` to change configuration.

        Omitted fields are preserved. An explicit null description clears it.
        ``tags`` replaces the current tags; pass [] to clear them. Tag changes
        do not create a scorer version.
        """
        return _parse(
            _scorer,
            self._transport.request(
                "PATCH",
                self._path(scorer_id),
                body=_encode(_metadata(name, description, archived, tags)),
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
        composition: ScorerComposition | None = None,
    ) -> Scorer[_ConfigT]:
        """Create an immutable version and return the updated scorer.

        Pass the observed ``current_version`` as ``base_version`` to detect
        concurrent writes. Conflicts are surfaced without automatic refresh.
        Identical configuration is allowed, for example after a prompt change.
        Composite versions require composition, including every pinned input,
        even when only changing the output configuration.
        """
        return cast(
            Scorer[_ConfigT],
            _parse(
                _scorer,
                self._transport.request(
                    "POST",
                    self._path(scorer_id) + "new_version/",
                    body=_version_body(
                        config, base_version, name, description, composition
                    ),
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

    def validate_composition(
        self,
        *,
        kind: ScorerKind,
        composition: ScorerComposition,
        config: ScorerConfig | None = None,
    ) -> bool:
        """Validate a formula without saving, using llm_analytics:read access.

        Returns True on validation success; invalid formulas raise
        EvaluationAPIError. Omit config to check syntax and types only.
        Runtime values and output bounds are checked during calculation.
        """
        return _parse(
            _validation,
            self._transport.request(
                "POST",
                self._base + "validate_composition/",
                body=_validation_body(kind, composition, config),
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
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[BooleanScorerConfig]: ...

    @overload
    async def create(
        self,
        *,
        name: str,
        kind: Literal["numeric"],
        config: NumericScorerConfig,
        description: str | None | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[NumericScorerConfig]: ...

    @overload
    async def create(
        self,
        *,
        name: str,
        kind: Literal["categorical"],
        config: CategoricalScorerConfig,
        description: str | None | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[CategoricalScorerConfig]: ...

    async def create(
        self,
        *,
        name: str,
        kind: ScorerKind,
        config: ScorerConfig,
        description: str | None | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
        calculation: ScorerCalculation = "reported",
        composition: ScorerComposition | None = None,
    ) -> Scorer[ScorerConfig]:
        """Create a scorer with optional tags. See Scorers.create."""
        return _parse(
            _scorer,
            await self._transport.request(
                "POST",
                self._base,
                body=_create_body(
                    name, kind, config, description, tags, calculation, composition
                ),
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
        tags: Sequence[str] | None = None,
        tag_match: Literal["any", "all"] = "any",
        calculation: ScorerCalculation | None = None,
        input_definition_id: str | UUID | None = None,
    ) -> ScorerPage:
        """Fetch one page with optional tag filters. See Scorers.list."""
        return _parse(
            _scorer_page,
            await self._transport.request(
                "GET",
                self._base,
                params=_list_params(
                    limit,
                    offset,
                    search,
                    kind,
                    archived,
                    order_by,
                    tags,
                    tag_match,
                    calculation,
                    input_definition_id,
                ),
            ),
        )

    async def update(
        self,
        scorer_id: str | UUID,
        *,
        name: str | _Omitted = _OMITTED,
        description: str | None | _Omitted = _OMITTED,
        archived: bool | _Omitted = _OMITTED,
        tags: Sequence[str] | _Omitted = _OMITTED,
    ) -> Scorer[ScorerConfig]:
        """Update metadata, including replacement tags. See Scorers.update."""
        return _parse(
            _scorer,
            await self._transport.request(
                "PATCH",
                self._path(scorer_id),
                body=_encode(_metadata(name, description, archived, tags)),
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
        composition: ScorerComposition | None = None,
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
                    body=_version_body(
                        config, base_version, name, description, composition
                    ),
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

    async def validate_composition(
        self,
        *,
        kind: ScorerKind,
        composition: ScorerComposition,
        config: ScorerConfig | None = None,
    ) -> bool:
        """Validate without saving. See Scorers.validate_composition."""
        return _parse(
            _validation,
            await self._transport.request(
                "POST",
                self._base + "validate_composition/",
                body=_validation_body(kind, composition, config),
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
