"""FeatureFlagEvaluations — a snapshot of feature flag values for a single distinct_id.

Returned by Client.evaluate_flags(). Branch on .is_enabled() / .get_flag(), then pass
the same snapshot to capture() via the `flags` option so events carry the exact flag
values the code branched on, with no additional /flags request.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Union

from .types import (
    FeatureFlag as _FeatureFlag,
    FeatureFlagError as _FeatureFlagError,
    FlagMetadata as _FlagMetadata,
    FlagsAndPayloads as _FlagsAndPayloads,
    FlagsResponse as _FlagsResponse,
    FlagValue,
)


@dataclass(frozen=True)
class _EvaluatedFlagRecord:
    """Internal per-flag record stored by a FeatureFlagEvaluations instance."""

    key: str
    enabled: bool
    variant: Optional[str]
    payload: Optional[Any]
    id: Optional[int]
    version: Optional[int]
    reason: Optional[str]
    locally_evaluated: bool
    # Server-reported signal for whether the flag is linked to an experiment.
    # ``None`` when the server did not report it (older deployments).
    has_experiment: Optional[bool] = None


@dataclass
class _FeatureFlagEvaluationsHost:
    """Callbacks the evaluations object uses to talk back to the client.

    Kept as a plain dataclass of callables so the class stays decoupled from the
    full Client surface — this also makes it trivial to construct a fake host in tests.
    """

    capture_flag_called_event_if_needed: Callable[..., None]
    log_warning: Callable[[str], None]


def _parse_evaluation_payload(raw_payload: Any) -> Optional[Any]:
    if isinstance(raw_payload, str) and raw_payload:
        try:
            return json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            return raw_payload
    if raw_payload is not None:
        return raw_payload
    return None


def _flag_details_metadata(
    flag_details: Optional[_FeatureFlag],
) -> tuple[Optional[int], Optional[int], Optional[str]]:
    if not isinstance(flag_details, _FeatureFlag):
        return None, None, None

    flag_id: Optional[int] = None
    flag_version: Optional[int] = None
    if isinstance(flag_details.metadata, _FlagMetadata):
        flag_id = flag_details.metadata.id
        flag_version = flag_details.metadata.version
    flag_reason = (
        flag_details.reason.description
        if flag_details.reason and flag_details.reason.description
        else None
    )
    return flag_id, flag_version, flag_reason


def _feature_flag_called_properties(
    *,
    key: str,
    response: Optional[FlagValue],
    locally_evaluated: bool,
    payload: Optional[Any] = None,
    request_id: Optional[str] = None,
    evaluated_at: Optional[int] = None,
    flag_id: Optional[int] = None,
    flag_version: Optional[int] = None,
    flag_reason: Optional[str] = None,
    feature_flag_error: Optional[str] = None,
) -> Dict[str, Any]:
    properties: Dict[str, Any] = {
        "$feature_flag": key,
        "$feature_flag_response": response,
        "locally_evaluated": locally_evaluated,
        f"$feature/{key}": response,
    }
    if payload is not None:
        properties["$feature_flag_payload"] = payload
    if request_id:
        properties["$feature_flag_request_id"] = request_id
    if evaluated_at:
        properties["$feature_flag_evaluated_at"] = evaluated_at
    if flag_id:
        properties["$feature_flag_id"] = flag_id
    if flag_version:
        properties["$feature_flag_version"] = flag_version
    if flag_reason:
        properties["$feature_flag_reason"] = flag_reason
    if feature_flag_error:
        properties["$feature_flag_error"] = feature_flag_error
    return properties


def _local_evaluation_records(
    local_result: _FlagsAndPayloads, feature_flags_by_key: Mapping[str, Any]
) -> tuple[Dict[str, _EvaluatedFlagRecord], set[str]]:
    records: Dict[str, _EvaluatedFlagRecord] = {}
    locally_evaluated_keys: set[str] = set()
    local_flags = local_result.get("featureFlags") or {}
    local_payloads = local_result.get("featureFlagPayloads") or {}
    for key, value in local_flags.items():
        flag_def = feature_flags_by_key.get(key) or {}
        records[key] = _EvaluatedFlagRecord(
            key=key,
            enabled=value is not False,
            variant=value if isinstance(value, str) else None,
            payload=local_payloads.get(key),
            id=flag_def.get("id"),
            # The local-evaluation flag definition does not carry a version field;
            # only the remote ``/flags`` response does via ``metadata.version``.
            version=None,
            reason="Evaluated locally",
            locally_evaluated=True,
            has_experiment=(
                flag_def.get("has_experiment")
                if isinstance(flag_def.get("has_experiment"), bool)
                else None
            ),
        )
        locally_evaluated_keys.add(key)
    return records, locally_evaluated_keys


def _remote_evaluation_records(
    response: _FlagsResponse, excluded_keys: Set[str]
) -> tuple[Dict[str, _EvaluatedFlagRecord], Optional[str], Optional[int], bool]:
    records: Dict[str, _EvaluatedFlagRecord] = {}
    for key, detail in response.get("flags", {}).items():
        if key in excluded_keys:
            continue
        flag_id, flag_version, flag_reason = _flag_details_metadata(detail)
        raw_payload = (
            detail.metadata.payload
            if isinstance(detail.metadata, _FlagMetadata)
            else getattr(detail.metadata, "payload", None)
        )
        records[key] = _EvaluatedFlagRecord(
            key=key,
            enabled=detail.enabled,
            variant=detail.variant,
            payload=_parse_evaluation_payload(raw_payload),
            id=flag_id,
            version=flag_version,
            reason=flag_reason,
            locally_evaluated=False,
            has_experiment=(
                detail.metadata.has_experiment
                if isinstance(detail.metadata, _FlagMetadata)
                else None
            ),
        )

    raw_evaluated_at = response.get("evaluatedAt")
    evaluated_at = raw_evaluated_at if isinstance(raw_evaluated_at, int) else None
    return (
        records,
        response.get("requestId"),
        evaluated_at,
        bool(response.get("errorsWhileComputingFlags", False)),
    )


class FeatureFlagEvaluations:
    """A point-in-time snapshot of feature flag evaluations for a single distinct_id.

    Returned by :meth:`Client.evaluate_flags` — branch on :meth:`is_enabled` /
    :meth:`get_flag` and pass the same object to :meth:`Client.capture` via the
    ``flags`` option so the captured event carries the exact flag values the code
    branched on.

    Example::

        flags = posthog.evaluate_flags(distinct_id, person_properties={"plan": "enterprise"})
        if flags.is_enabled("new-dashboard"):
            render_new_dashboard()
        posthog.capture("page_viewed", distinct_id=distinct_id, flags=flags)

    To narrow the set of flags that get attached to a captured event, use the in-memory
    helpers :meth:`only` and :meth:`only_accessed`. To narrow the set of flags requested
    from the server in the first place, pass ``flag_keys`` to :meth:`Client.evaluate_flags`.
    """

    def __init__(
        self,
        host: _FeatureFlagEvaluationsHost,
        distinct_id: str,
        flags: Dict[str, _EvaluatedFlagRecord],
        groups: Optional[Mapping[str, Union[str, int]]] = None,
        disable_geoip: Optional[bool] = None,
        request_id: Optional[str] = None,
        evaluated_at: Optional[int] = None,
        errors_while_computing: bool = False,
        quota_limited: bool = False,
        minimal_flag_called_events: bool = False,
        accessed: Optional[Set[str]] = None,
    ) -> None:
        """Internal — instances are created by the SDK via ``Client.evaluate_flags()``."""
        self._host = host
        self._distinct_id = distinct_id
        self._flags = flags
        self._groups: Dict[str, Union[str, int]] = dict(groups or {})
        self._disable_geoip = disable_geoip
        self._request_id = request_id
        self._evaluated_at = evaluated_at
        self._errors_while_computing = errors_while_computing
        self._quota_limited = quota_limited
        # Pinned at snapshot creation: the gate value from the evaluation that produced
        # these records. Deferred flag accesses fire events shaped by THIS evaluation's
        # server response, not whatever the client-wide gate happens to be at send time.
        self._minimal_flag_called_events = minimal_flag_called_events
        self._accessed: Set[str] = set(accessed) if accessed is not None else set()

    def is_enabled(self, key: str) -> bool:
        """Return whether the flag is enabled. Fires ``$feature_flag_called`` on the
        first access per (distinct_id, flag, value) tuple, deduped via the SDK's cache.

        Flags that were not returned from the underlying evaluation are treated as
        disabled (returns ``False``).
        """
        flag = self._flags.get(key)
        self._record_access(key)
        return bool(flag.enabled) if flag else False

    def get_flag(self, key: str) -> Optional[FlagValue]:
        """Return the flag value. Fires ``$feature_flag_called`` on first access.

        Returns the variant string for multivariate flags, ``True`` for enabled flags
        without a variant, ``False`` for disabled flags, and ``None`` for flags that
        were not returned by the evaluation.
        """
        flag = self._flags.get(key)
        self._record_access(key)
        if not flag:
            return None
        if not flag.enabled:
            return False
        return flag.variant if flag.variant is not None else True

    def get_flag_payload(self, key: str) -> Optional[Any]:
        """Return the payload associated with a flag.

        Does not count as an access for :meth:`only_accessed` and does not fire any event.
        """
        flag = self._flags.get(key)
        return flag.payload if flag else None

    def only_accessed(self) -> "FeatureFlagEvaluations":
        """Return a filtered copy containing only flags accessed via :meth:`is_enabled`
        or :meth:`get_flag` before this call.

        Order-dependent: if nothing has been accessed yet, the returned snapshot is
        empty. The method honors its name — pre-access if you want a populated result.
        """
        filtered = {k: self._flags[k] for k in self._accessed if k in self._flags}
        return self._clone_with(filtered)

    def only(self, keys: List[str]) -> "FeatureFlagEvaluations":
        """Return a filtered copy containing only flags with the given keys. Keys that
        are not present in the evaluation are dropped and logged as a warning.
        """
        filtered: Dict[str, _EvaluatedFlagRecord] = {}
        missing: List[str] = []
        for key in keys:
            flag = self._flags.get(key)
            if flag is not None:
                filtered[key] = flag
            else:
                missing.append(key)
        if missing:
            self._host.log_warning(
                "FeatureFlagEvaluations.only() was called with flag keys that are not in the "
                f"evaluation set and will be dropped: {', '.join(missing)}"
            )
        return self._clone_with(filtered)

    @property
    def keys(self) -> List[str]:
        """Return the flag keys that are part of this evaluation."""
        return list(self._flags.keys())

    # --- Internal -------------------------------------------------------------

    def _get_event_properties(self) -> Dict[str, Any]:
        """Build the ``$feature/*`` and ``$active_feature_flags`` properties for an event.

        Internal — called by capture() when an event is captured with ``flags=...``.
        """
        properties: Dict[str, Any] = {}
        active_flags: List[str] = []
        for key, flag in self._flags.items():
            value: FlagValue = (
                False
                if not flag.enabled
                else (flag.variant if flag.variant is not None else True)
            )
            properties[f"$feature/{key}"] = value
            if flag.enabled:
                active_flags.append(key)
        if active_flags:
            properties["$active_feature_flags"] = sorted(active_flags)
        return properties

    @property
    def _internal_distinct_id(self) -> str:
        return self._distinct_id

    @property
    def _internal_groups(self) -> Dict[str, Union[str, int]]:
        return self._groups

    def _clone_with(
        self, flags: Dict[str, _EvaluatedFlagRecord]
    ) -> "FeatureFlagEvaluations":
        return FeatureFlagEvaluations(
            host=self._host,
            distinct_id=self._distinct_id,
            flags=flags,
            groups=self._groups,
            disable_geoip=self._disable_geoip,
            request_id=self._request_id,
            evaluated_at=self._evaluated_at,
            errors_while_computing=self._errors_while_computing,
            quota_limited=self._quota_limited,
            minimal_flag_called_events=self._minimal_flag_called_events,
            # Copy the accessed set so the child tracks further access independently
            # of the parent. Callers expect ``only_accessed()`` on the parent to reflect
            # only what the parent saw, not what happened on filtered views.
            accessed=set(self._accessed),
        )

    def _record_access(self, key: str) -> None:
        self._accessed.add(key)

        # Empty snapshots (no resolvable distinct_id) are returned by ``evaluate_flags()``
        # as a safety fallback. Firing $feature_flag_called for them would emit events
        # with an empty distinct_id, polluting analytics — short-circuit here.
        if not self._distinct_id:
            return

        flag = self._flags.get(key)
        if flag is None:
            response: Optional[FlagValue] = None
        elif not flag.enabled:
            response = False
        else:
            response = flag.variant if flag.variant is not None else True

        # Build the comma-joined `$feature_flag_error` matching the single-flag path's
        # granularity: response-level errors (errors-while-computing, quota-limited) are
        # combined with per-flag errors (flag-missing) so consumers can filter by type.
        errors: List[str] = []
        if self._errors_while_computing:
            errors.append(_FeatureFlagError.ERRORS_WHILE_COMPUTING)
        if self._quota_limited:
            errors.append(_FeatureFlagError.QUOTA_LIMITED)
        if flag is None:
            errors.append(_FeatureFlagError.FLAG_MISSING)

        properties = _feature_flag_called_properties(
            key=key,
            response=response,
            locally_evaluated=flag.locally_evaluated if flag else False,
            payload=flag.payload if flag else None,
            request_id=self._request_id,
            evaluated_at=(
                self._evaluated_at
                if self._evaluated_at and not (flag and flag.locally_evaluated)
                else None
            ),
            flag_id=flag.id if flag else None,
            flag_version=flag.version if flag else None,
            flag_reason=flag.reason if flag else None,
            feature_flag_error=",".join(errors) if errors else None,
        )

        self._host.capture_flag_called_event_if_needed(
            distinct_id=self._distinct_id,
            key=key,
            response=response,
            groups=self._groups,
            disable_geoip=self._disable_geoip,
            properties=properties,
            has_experiment=flag.has_experiment if flag else None,
            minimal_flag_called_events=self._minimal_flag_called_events,
        )
