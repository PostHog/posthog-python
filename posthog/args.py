from typing import (
    TYPE_CHECKING,
    TypedDict,
    Optional,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    NamedTuple,
    Union,
    Tuple,
    Type,
)
from types import TracebackType
from typing_extensions import NotRequired  # For Python < 3.11 compatibility
from datetime import datetime
import numbers
from uuid import UUID

from posthog.types import SendFeatureFlagsOptions

if TYPE_CHECKING:
    from posthog.feature_flag_evaluations import FeatureFlagEvaluations

ID_TYPES = Union[numbers.Number, str, UUID, int]


class OptionalCaptureArgs(TypedDict):
    """Optional arguments for the capture method.

    Args:
        distinct_id: Unique identifier for the person associated with this event. If not set, the context
            distinct_id is used, if available, otherwise a UUID is generated, and the event is marked
            as personless. Setting context-level distinct_id's is recommended.
        properties: Dictionary of properties to track with the event
        timestamp: When the event occurred (defaults to current time). UTC is
            preferred; non-UTC datetimes and parseable ISO timestamp strings are
            converted to UTC.
        uuid: Unique identifier for this specific event. If not provided, one is generated. The event
            UUID is returned, so you can correlate it with actions in your app (like showing users an
            error ID if you capture an exception). If provided, it must be a valid UUID string or
            uuid.UUID instance; invalid values are ignored and replaced with a newly generated UUID.
        groups: Group identifiers to associate with this event (format: {group_type: group_key})
        flags: A ``FeatureFlagEvaluations`` snapshot from ``evaluate_flags()``. The exact flag
            values from the snapshot are attached to the event with no additional network call —
            prefer this over ``send_feature_flags``.
        send_feature_flags: Deprecated — prefer ``flags`` with a ``FeatureFlagEvaluations``
            snapshot. Whether to include currently active feature flags in the event properties.
            Can be a boolean or a SendFeatureFlagsOptions object. Defaults to False. Fires a
            hidden ``/flags`` request on capture and may return different values than the ones
            the code branched on.
        disable_geoip: Whether to disable GeoIP lookup for this event. Defaults to False.
        options: Capture options for this event, such as ``{"process_person_profile": False}``.
            Sent as given, for PostHog to validate. An option wins over its legacy ``$`` property,
            such as ``$process_person_profile``. A value that is not a dict is logged and ignored.
            An identify-style event, such as ``$identify`` or ``$set``, gets
            ``process_person_profile: True`` when this call leaves it unset, unless the SDK
            generated its distinct ID.
    """

    distinct_id: NotRequired[Optional[ID_TYPES]]
    properties: NotRequired[Optional[Dict[str, Any]]]
    timestamp: NotRequired[Optional[Union[datetime, str]]]
    uuid: NotRequired[Optional[Union[str, UUID]]]
    options: NotRequired[Optional[Dict[str, Any]]]
    groups: NotRequired[Optional[Dict[str, str]]]
    flags: NotRequired[Optional["FeatureFlagEvaluations"]]
    send_feature_flags: NotRequired[
        Optional[Union[bool, SendFeatureFlagsOptions]]
    ]  # Updated to support both boolean and options object
    disable_geoip: NotRequired[
        Optional[bool]
    ]  # As above, optional so we can tell if the user is intentionally overriding a client setting or not
    _property_allowlist: NotRequired[
        Optional[FrozenSet[str]]
    ]  # Internal: strict allowlist applied to the fully-enriched event properties. Used by minimal $feature_flag_called events.
    _capture_trace_context: NotRequired[
        bool
    ]  # Internal: add the active OpenTelemetry span's ids even when capture_trace_context is off. Used by capture_exception.
    _pinned_properties: NotRequired[
        Optional["_PinnedProperties"]
    ]  # Internal: properties fixed after super properties are merged. Used by config version 2 flag events.
    _before_send_drops: NotRequired[
        Optional[List[str]]
    ]  # Internal: gets the event name when before_send drops the event. Used by flag event dedupe.


class _PinnedProperties(NamedTuple):
    """Internal: event properties that super properties cannot change.

    Applied to the fully-enriched properties, after super properties are merged and
    before any allowlist: ``removed`` names are dropped, then ``values`` are set.
    """

    removed: FrozenSet[str]
    values: Mapping[str, Any]

    def apply(self, properties: Dict[str, Any]) -> Dict[str, Any]:
        kept = {k: v for k, v in properties.items() if k not in self.removed}
        return {**kept, **self.values}


class OptionalSetArgs(TypedDict):
    """Optional arguments for the set method.

    Args:
        distinct_id: Unique identifier for the user to set properties on. If not set, the context
            distinct_id is used, if available, otherwise this function does nothing. Setting
            context-level distinct_id's is recommended.
        properties: Dictionary of properties to set on the person
        timestamp: When the properties were set (defaults to current time). UTC
            is preferred; non-UTC datetimes and parseable ISO timestamp strings
            are converted to UTC.
        uuid: Unique identifier for this operation. If not provided, one is generated. This
            UUID is returned, so you can correlate it with actions in your app. If provided,
            it must be a valid UUID string or uuid.UUID instance; invalid values are ignored
            and replaced with a newly generated UUID.
        disable_geoip: Whether to disable GeoIP lookup for this operation. Defaults to False.
        options: Capture options for this event, sent as given. See ``OptionalCaptureArgs``.
            ``process_person_profile`` is ``True`` unless this call sets it.
    """

    distinct_id: NotRequired[Optional[ID_TYPES]]
    properties: NotRequired[Optional[Dict[str, Any]]]
    timestamp: NotRequired[Optional[Union[datetime, str]]]
    uuid: NotRequired[Optional[Union[str, UUID]]]
    disable_geoip: NotRequired[Optional[bool]]
    options: NotRequired[Optional[Dict[str, Any]]]


ExcInfo = Union[
    Tuple[Type[BaseException], BaseException, Optional[TracebackType]],
    Tuple[None, None, None],
]

ExceptionArg = Union[BaseException, ExcInfo]
