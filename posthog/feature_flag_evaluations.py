"""FeatureFlagEvaluations — a snapshot of feature flag values for a single distinct_id.

Returned by Client.evaluate_flags(). Branch on .is_enabled() / .get_flag(), then pass
the same snapshot to capture() via the `flags` option so events carry the exact flag
values the code branched on, with no additional /flags request.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from posthog.types import FlagValue


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


@dataclass
class _FeatureFlagEvaluationsHost:
    """Callbacks the evaluations object uses to talk back to the client.

    Kept as a plain dataclass of callables so the class stays decoupled from the
    full Client surface — this also makes it trivial to construct a fake host in tests.
    """

    capture_flag_called_event_if_needed: Callable[..., None]
    log_warning: Callable[[str], None]


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
        groups: Optional[Dict[str, str]] = None,
        disable_geoip: Optional[bool] = None,
        request_id: Optional[str] = None,
        evaluated_at: Optional[int] = None,
        accessed: Optional[Set[str]] = None,
    ) -> None:
        """Internal — instances are created by the SDK via ``Client.evaluate_flags()``."""
        self._host = host
        self._distinct_id = distinct_id
        self._flags = flags
        self._groups: Dict[str, str] = groups or {}
        self._disable_geoip = disable_geoip
        self._request_id = request_id
        self._evaluated_at = evaluated_at
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

        **Empty-access fallback:** if no flags have been accessed yet, this method logs
        a warning and returns a copy with *all* evaluated flags. This avoids silently
        dropping every flag from the captured event when ``only_accessed()`` is called
        out of order. Pre-access before calling this if you want a guaranteed-empty
        result.
        """
        if not self._accessed:
            self._host.log_warning(
                "FeatureFlagEvaluations.only_accessed() was called before any flags were accessed — "
                "attaching all evaluated flags as a fallback. See "
                "https://posthog.com/docs/feature-flags/server-sdks for details."
            )
            return self._clone_with(self._flags)
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
    def _internal_groups(self) -> Dict[str, str]:
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

        properties: Dict[str, Any] = {
            "$feature_flag": key,
            "$feature_flag_response": response,
            "locally_evaluated": flag.locally_evaluated if flag else False,
            f"$feature/{key}": response,
        }

        if flag is not None:
            if flag.payload is not None:
                properties["$feature_flag_payload"] = flag.payload
            if flag.id:
                properties["$feature_flag_id"] = flag.id
            if flag.version:
                properties["$feature_flag_version"] = flag.version
            if flag.reason:
                properties["$feature_flag_reason"] = flag.reason

        if self._request_id:
            properties["$feature_flag_request_id"] = self._request_id
        if self._evaluated_at and not (flag and flag.locally_evaluated):
            properties["$feature_flag_evaluated_at"] = self._evaluated_at
        if flag is None:
            properties["$feature_flag_error"] = "flag_missing"

        self._host.capture_flag_called_event_if_needed(
            distinct_id=self._distinct_id,
            key=key,
            response=response,
            groups=self._groups,
            disable_geoip=self._disable_geoip,
            properties=properties,
        )
