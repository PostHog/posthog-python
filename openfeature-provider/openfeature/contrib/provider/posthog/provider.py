"""Official PostHog provider for the OpenFeature Python SDK.

This wraps a configured :class:`posthog.Posthog` client and exposes flag
evaluation through OpenFeature's :class:`~openfeature.provider.AbstractProvider`
contract, using the modern, single-call ``Client.get_feature_flag_result`` API.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar, Union

from openfeature.evaluation_context import EvaluationContext
from openfeature.exception import (
    FlagNotFoundError,
    TargetingKeyMissingError,
    TypeMismatchError,
)
from openfeature.flag_evaluation import FlagResolutionDetails, Reason
from openfeature.provider import AbstractProvider
from openfeature.provider.metadata import Metadata

import posthog
from posthog.types import FeatureFlagResult

# Reserved evaluation-context attribute keys. Every other attribute in
# ``evaluation_context.attributes`` is forwarded as a PostHog person property.
GROUPS_KEY = "groups"
GROUP_PROPERTIES_KEY = "group_properties"
_RESERVED_KEYS = frozenset({GROUPS_KEY, GROUP_PROPERTIES_KEY})

ObjectValue = Union[Sequence[Any], Mapping[str, Any]]

_N = TypeVar("_N", int, float)


class PostHogProvider(AbstractProvider):
    """OpenFeature provider backed by a configured :class:`posthog.Posthog` client.

    The caller owns the PostHog client lifecycle: construct and configure the
    client yourself (project key, ``personal_api_key`` for local evaluation,
    ``host``, ...), then hand it to this provider.

    Evaluation-context mapping:
        * ``targeting_key``               -> PostHog ``distinct_id``
        * reserved attr ``groups``        -> PostHog ``groups``
        * reserved attr ``group_properties`` -> PostHog ``group_properties``
        * every other attribute           -> PostHog ``person_properties``

    Flag-type mapping (all via ``get_feature_flag_result``):
        * boolean -> ``enabled``
        * string  -> the multivariate ``variant`` key
        * int/float -> the ``variant`` parsed to a number
        * object  -> the flag's JSON ``payload``

    Args:
        client: A configured :class:`posthog.Posthog` instance.
        default_distinct_id: Distinct ID to use when the evaluation context has
            no ``targeting_key``. If ``None`` (default), a missing targeting key
            raises :class:`~openfeature.exception.TargetingKeyMissingError`,
            which is OpenFeature-idiomatic. Set a value (e.g. ``"anonymous"``)
            to opt into anonymous evaluation.
        send_feature_flag_events: Forwarded to ``get_feature_flag_result`` to
            control ``$feature_flag_called`` capture. Defaults to ``True`` so
            PostHog flag analytics (and experiments) keep working.
    """

    def __init__(
        self,
        client: posthog.Posthog,
        *,
        default_distinct_id: Optional[str] = None,
        send_feature_flag_events: bool = True,
    ) -> None:
        super().__init__()
        self._client = client
        self._default_distinct_id = default_distinct_id
        self._send_feature_flag_events = send_feature_flag_events

    # -- metadata / lifecycle -------------------------------------------------

    def get_metadata(self) -> Metadata:
        return Metadata(name="PostHogProvider")

    def initialize(self, evaluation_context: EvaluationContext) -> None:
        # Preload locally-evaluated flag definitions only when the injected
        # client is configured for local evaluation. We do not otherwise mutate
        # the caller-owned client, and a preload failure must not make the
        # OpenFeature client un-ready (remote evaluation still works).
        if getattr(self._client, "personal_api_key", None):
            try:
                self._client.load_feature_flags()
            except Exception:
                pass

    def shutdown(self) -> None:
        # The provider does not own the injected client's lifecycle, so this is
        # deliberately a no-op. Callers shut down their own ``Posthog`` client.
        return None

    # -- core resolution ------------------------------------------------------

    def _resolve(
        self,
        flag_key: str,
        evaluation_context: Optional[EvaluationContext],
    ) -> FeatureFlagResult:
        distinct_id = self._distinct_id(evaluation_context)
        person_properties, groups, group_properties = self._split_context(
            evaluation_context
        )
        result = self._client.get_feature_flag_result(
            flag_key,
            distinct_id,
            groups=groups or None,
            person_properties=person_properties or None,
            group_properties=group_properties or None,
            send_feature_flag_events=self._send_feature_flag_events,
        )
        if result is None:
            raise FlagNotFoundError(f"Flag '{flag_key}' not found or disabled.")
        return result

    # -- typed resolvers ------------------------------------------------------

    def resolve_boolean_details(
        self,
        flag_key: str,
        default_value: bool,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[bool]:
        result = self._resolve(flag_key, evaluation_context)
        return FlagResolutionDetails(
            value=result.enabled,
            variant=result.variant,
            reason=self._map_reason(result),
            flag_metadata=self._flag_metadata(result),
        )

    def resolve_string_details(
        self,
        flag_key: str,
        default_value: str,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[str]:
        result = self._resolve(flag_key, evaluation_context)
        if result.variant is None:
            # A boolean flag has no string variant. Surface a type mismatch so
            # the caller gets its default value (per the OpenFeature spec)
            # rather than a surprising "True"/"False" string.
            raise TypeMismatchError(
                f"Flag '{flag_key}' has no string variant (boolean flag)."
            )
        return FlagResolutionDetails(
            value=result.variant,
            variant=result.variant,
            reason=self._map_reason(result),
            flag_metadata=self._flag_metadata(result),
        )

    def resolve_integer_details(
        self,
        flag_key: str,
        default_value: int,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[int]:
        return self._resolve_number(flag_key, evaluation_context, int)

    def resolve_float_details(
        self,
        flag_key: str,
        default_value: float,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[float]:
        return self._resolve_number(flag_key, evaluation_context, float)

    def resolve_object_details(
        self,
        flag_key: str,
        default_value: ObjectValue,
        evaluation_context: Optional[EvaluationContext] = None,
    ) -> FlagResolutionDetails[ObjectValue]:
        result = self._resolve(flag_key, evaluation_context)
        payload = result.payload  # already JSON-deserialized by posthog
        if not isinstance(payload, (dict, list)):
            raise TypeMismatchError(f"Flag '{flag_key}' has no object/JSON payload.")
        return FlagResolutionDetails(
            value=payload,
            variant=result.variant,
            reason=self._map_reason(result),
            flag_metadata=self._flag_metadata(result),
        )

    def _resolve_number(
        self,
        flag_key: str,
        evaluation_context: Optional[EvaluationContext],
        ctor: Callable[[str], _N],
    ) -> FlagResolutionDetails[_N]:
        result = self._resolve(flag_key, evaluation_context)
        if result.variant is None:
            raise TypeMismatchError(
                f"Flag '{flag_key}' has no variant to parse as {ctor.__name__}."
            )
        try:
            value = ctor(result.variant)
        except (TypeError, ValueError) as exc:
            raise TypeMismatchError(
                f"Flag '{flag_key}' variant '{result.variant}' is not a valid "
                f"{ctor.__name__}."
            ) from exc
        return FlagResolutionDetails(
            value=value,
            variant=result.variant,
            reason=self._map_reason(result),
            flag_metadata=self._flag_metadata(result),
        )

    # -- helpers --------------------------------------------------------------

    def _distinct_id(self, evaluation_context: Optional[EvaluationContext]) -> str:
        if evaluation_context is not None and evaluation_context.targeting_key:
            return evaluation_context.targeting_key
        if self._default_distinct_id is not None:
            return self._default_distinct_id
        raise TargetingKeyMissingError(
            "No targeting_key in evaluation context and no default_distinct_id "
            "configured."
        )

    @staticmethod
    def _split_context(
        evaluation_context: Optional[EvaluationContext],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if evaluation_context is None or not evaluation_context.attributes:
            return {}, {}, {}
        attrs = evaluation_context.attributes
        groups = attrs.get(GROUPS_KEY) or {}
        group_properties = attrs.get(GROUP_PROPERTIES_KEY) or {}
        person_properties = {k: v for k, v in attrs.items() if k not in _RESERVED_KEYS}
        groups = groups if isinstance(groups, dict) else {}
        group_properties = (
            group_properties if isinstance(group_properties, dict) else {}
        )
        return person_properties, groups, group_properties

    @staticmethod
    def _map_reason(result: FeatureFlagResult) -> Reason:
        """Map PostHog's free-text reason / enabled state to an OpenFeature Reason."""
        if not result.enabled:
            return Reason.DISABLED
        text = (result.reason or "").lower()
        if "condition" in text or "match" in text or "variant" in text:
            return Reason.TARGETING_MATCH
        if "default" in text:
            return Reason.DEFAULT
        # Enabled, but no recognizable reason text: treat an absent reason as a
        # targeting match, and an unfamiliar reason string as unknown.
        return Reason.TARGETING_MATCH if result.reason is None else Reason.UNKNOWN

    @staticmethod
    def _flag_metadata(result: FeatureFlagResult) -> Mapping[str, Any]:
        meta: dict[str, Any] = {}
        if result.reason is not None:
            meta["posthog_reason"] = result.reason
        return meta
