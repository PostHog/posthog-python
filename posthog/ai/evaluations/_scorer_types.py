"""Typed scorer configurations and immutable scorer metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from typing_extensions import Required, TypedDict


ScorerKind = Literal["boolean", "numeric", "categorical"]
ScorerOrder = Literal[
    "name",
    "-name",
    "kind",
    "-kind",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
    "current_version",
    "-current_version",
]


class BooleanScorerConfig(TypedDict, total=False):
    """Boolean labels and polarity, pinned to each scorer version.

    ``true_is_failure=True`` means false passes. Omitted, false, or null means
    true passes. Labels change presentation without changing stored values.
    """

    true_is_failure: bool | None
    true_label: str
    false_label: str


class NumericPassingRule(TypedDict):
    """Pass at or above (``gte``), or at or below (``lte``), the threshold."""

    operator: Literal["gte", "lte"]
    threshold: float


class NumericScorerConfig(TypedDict, total=False):
    """Inclusive bounds, input step, and optional numeric passing rule.

    An omitted or null passing rule leaves the score neutral. A configured
    threshold must be finite and within the configured bounds.
    """

    min: float | None
    max: float | None
    step: float | None
    passing_rule: NumericPassingRule | None


class CategoricalScorerOption(TypedDict):
    """Stable category key and its human-readable label."""

    key: str
    label: str


class CategoricalPassingRule(TypedDict):
    """Every returned category must be in ``categories`` for a score to pass."""

    categories: list[str]


class CategoricalScorerConfig(TypedDict, total=False):
    """Ordered options, selection constraints, and optional passing categories.

    ``options`` is required. Selection defaults to ``single``; minimum and
    maximum selections only apply to ``multiple``. An omitted or null passing
    rule leaves the score neutral. An empty passing list is supported only for
    multiple selection and makes every accepted offline result fail.
    """

    options: Required[list[CategoricalScorerOption]]
    selection_mode: Literal["single", "multiple"]
    min_selections: int | None
    max_selections: int | None
    passing_rule: CategoricalPassingRule | None


ScorerConfig = BooleanScorerConfig | NumericScorerConfig | CategoricalScorerConfig
_ConfigT = TypeVar("_ConfigT", bound=ScorerConfig, covariant=True)


@dataclass(frozen=True)
class Scorer(Generic[_ConfigT]):
    """Scorer metadata and the configuration of its current immutable version.

    Read ``current_version_id`` once to pin the version for an experiment.
    Names are not unique; use ``id`` for subsequent management operations.
    Configuration dictionaries are local snapshots, not live server state.
    """

    id: str
    name: str
    description: str
    kind: ScorerKind
    archived: bool
    current_version: int
    current_version_id: str
    config: _ConfigT
    created_at: datetime
    updated_at: datetime
    created_by: dict[str, Any] | None
    team: int


@dataclass(frozen=True)
class ScorerVersion(Generic[_ConfigT]):
    """An exact immutable scorer version, including its original polarity."""

    id: str
    definition_id: str
    version: int
    kind: ScorerKind
    config: _ConfigT
    created_at: datetime
    created_by: dict[str, Any] | None


@dataclass(frozen=True)
class ScorerPage:
    """One scorer page; pass ``next_offset`` to ``scorers.list`` to continue."""

    count: int
    results: tuple[Scorer[ScorerConfig], ...]
    next: str | None
    previous: str | None
    next_offset: int | None


@dataclass(frozen=True)
class ScorerVersionPage:
    """One version page; pass ``next_cursor`` to ``list_versions`` to continue."""

    count: int
    results: tuple[ScorerVersion[ScorerConfig], ...]
    next_cursor: str | None
