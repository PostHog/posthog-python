import copy
import json
import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    TypedDict,
    TypeVar,
    Union,
    cast,
)

FlagValue = Union[bool, str]

_T = TypeVar("_T")


def _reject_json_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


def _parse_flag_payload(raw_payload: Any, *, decode: bool = True) -> Optional[Any]:
    if isinstance(raw_payload, str):
        try:
            parsed = json.loads(raw_payload, parse_constant=_reject_json_constant)
            # Legacy bulk getters validate but preserve serialized values to avoid
            # breaking existing callers that decode payloads with json.loads().
            return parsed if decode else raw_payload
        except (ValueError, RecursionError):
            logging.getLogger("posthog").debug(
                "[FEATURE FLAGS] Unable to parse flag payload as JSON"
            )
            return None
    return raw_payload


# Type alias for the before_send callback function
# Takes an event dictionary and returns the modified event or None to drop it
BeforeSendCallback = Callable[[dict[str, Any]], Optional[dict[str, Any]]]

# Type alias for the traces before_span_send callback function
# Takes a span dictionary and returns the modified span or None to drop it
BeforeSpanSendCallback = Callable[[dict[str, Any]], Optional[dict[str, Any]]]


# Type alias for the send_feature_flags parameter
class SendFeatureFlagsOptions(TypedDict, total=False):
    """Options for deprecated ``capture(send_feature_flags=...)`` behavior.

    Prefer passing ``flags=posthog.evaluate_flags(...)`` to ``capture()`` for new
    code.

    Args:
        should_send: Whether feature flags should be evaluated and attached to
            the event.
        only_evaluate_locally: Whether to only use local evaluation for feature
            flags. If True, only flags that can be evaluated locally will be
            included. If False, remote evaluation via /flags API will be used
            when needed.
        person_properties: Properties to use for feature flag evaluation specific
            to this event. These properties will be merged with any existing
            person properties.
        group_properties: Group properties to use for feature flag evaluation
            specific to this event. Format: { group_type_name: { group_properties } }
        flag_keys_filter: Optional list of flag keys to evaluate and attach.
    """

    should_send: bool
    only_evaluate_locally: Optional[bool]
    person_properties: Optional[dict[str, Any]]
    group_properties: Optional[dict[str, dict[str, Any]]]
    flag_keys_filter: Optional[list[str]]


class FeatureFlagEvaluationRuntime(str, Enum):
    """Where a feature flag is meant to be evaluated.

    Set per flag in PostHog and carried on every locally cached flag definition.
    ``ALL`` means the flag suits both client-side and server-side evaluation, so
    it matches either runtime. Inheriting from ``str`` keeps the members directly
    comparable to their ``"all"`` / ``"client"`` / ``"server"`` values.
    """

    ALL = "all"
    CLIENT = "client"
    SERVER = "server"

    @classmethod
    def from_value(cls, value: Any) -> "FeatureFlagEvaluationRuntime":
        """Coerce a raw ``evaluation_runtime`` value to a member.

        A missing, null or unrecognized value becomes ``ALL``, which is the
        default PostHog applies to a flag that does not set a runtime.
        """
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        return cls.ALL

    def matches(self, other: "FeatureFlagEvaluationRuntime") -> bool:
        """Whether a flag set to one of these runtimes suits the other runtime.

        ``ALL`` matches every runtime, so the check is symmetric.
        """
        return self is other or FeatureFlagEvaluationRuntime.ALL in (self, other)


class UnresolvedFlagReason(str, Enum):
    """Why local evaluation left a flag without a value.

    Reported by :attr:`FeatureFlagEvaluations.unresolved_flags`. Inheriting from
    ``str`` keeps the members directly comparable to their string values. When more
    than one cause applies, the first reason in the order below is reported.

    Values:
        EXPERIENCE_CONTINUITY: The flag has experience continuity enabled, which
            local evaluation never resolves. Change the flag to fix it.
        UNSUPPORTED_DEFINITION: The flag uses something the local evaluator does not
            support, such as a static cohort, an unknown operator, a malformed value,
            a dependency with no loaded definition or a circular dependency. Change
            the flag or upgrade the SDK to fix it.
        UNRESOLVED_DEPENDENCY: A flag this flag depends on has a loaded definition
            but was unresolved.
        MISSING_CONTEXT: The call did not pass a property, group property or device
            id the flag needs, or passed it in an unusable form. Pass it to fix it.
    """

    EXPERIENCE_CONTINUITY = "experience_continuity"
    UNSUPPORTED_DEFINITION = "unsupported_definition"
    UNRESOLVED_DEPENDENCY = "unresolved_dependency"
    MISSING_CONTEXT = "missing_context"


@dataclass(frozen=True)
class FlagReason:
    """Reason metadata returned by the feature flag API.

    Attributes:
        code: Machine-readable reason code.
        condition_index: Matching condition index, when available.
        description: Human-readable reason description.
    """

    code: str
    condition_index: Optional[int]
    description: str

    @classmethod
    def from_json(cls, resp: Any) -> Optional["FlagReason"]:
        if not resp:
            return None
        return cls(
            code=resp.get("code", ""),
            condition_index=resp.get("condition_index"),
            description=resp.get("description", ""),
        )


@dataclass(frozen=True)
class LegacyFlagMetadata:
    """Legacy feature flag metadata containing only a payload."""

    payload: Any


@dataclass(frozen=True)
class FlagMetadata:
    """Feature flag metadata returned by the feature flag API.

    Attributes:
        id: Numeric feature flag ID, or ``None`` when the record has none.
        payload: Payload configured for the matched flag value, if any. For a
            number or object value it is the value encoded as JSON, the way
            servers before ``/flags?v=4`` sent it.
        version: Feature flag version, or ``None`` when the record has none.
        description: Feature flag description.
        has_experiment: Whether the flag has a linked experiment. ``None`` when
            the server does not report the field (older deployments).
        config_version: Configuration format of the flag. Responses without a
            typed ``value`` always read as ``1``.
        rule_type: Type of the rule that produced the result, when one did.
        rule_id: ID of the rule that produced the result, when one did.
        variant_key: Key of the assigned variant, when one was assigned.
        forced_variant: Whether a condition-level variant override produced the
            result.
    """

    id: Optional[int]
    payload: Optional[str]
    version: Optional[int]
    description: str
    has_experiment: Optional[bool] = None
    config_version: Optional[int] = None
    rule_type: Optional[str] = None
    rule_id: Optional[str] = None
    variant_key: Optional[str] = None
    forced_variant: Optional[bool] = None

    @classmethod
    def from_json(cls, resp: Any) -> Union["FlagMetadata", LegacyFlagMetadata]:
        """Parse the metadata of a record without a typed ``value``.

        Such a record comes from a server before ``/flags?v=4``, so only the
        fields that response carries are read, and ``config_version`` is ``1``.
        """
        if not resp:
            return LegacyFlagMetadata(payload=None)
        raw_has_experiment = resp.get("has_experiment")
        return cls(
            id=resp.get("id"),
            payload=resp.get("payload"),
            version=resp.get("version"),
            description=resp.get("description", ""),
            has_experiment=raw_has_experiment
            if isinstance(raw_has_experiment, bool)
            else None,
            config_version=1,
        )


class FlagEvaluationErrorCode(str, Enum):
    """Why a typed flag accessor returned the caller default instead of a value.

    The values are the OpenFeature error codes.
    """

    FLAG_NOT_FOUND = "FLAG_NOT_FOUND"
    """The flag is not in the evaluation."""
    PARSE_ERROR = "PARSE_ERROR"
    """A field of the flag's record has the wrong JSON type."""
    TYPE_MISMATCH = "TYPE_MISMATCH"
    """The flag's value is not of the requested type."""
    INVALID_CONTEXT = "INVALID_CONTEXT"
    """The flag needs a group the evaluation did not include."""
    GENERAL = "GENERAL"
    """The flag failed to evaluate."""

    def __str__(self) -> str:
        return self.value


class FlagEvaluationReason(str, Enum):
    """Why a typed flag accessor returned its value.

    The values are the OpenFeature reasons. The PostHog reason code is in
    ``flag_metadata["reason_code"]``.
    """

    TARGETING_MATCH = "TARGETING_MATCH"
    """The flag matched its targeting."""
    SPLIT = "SPLIT"
    """The flag assigned an experiment variant."""
    DEFAULT = "DEFAULT"
    """No rule produced a value, so the flag returned its default."""
    DISABLED = "DISABLED"
    """The flag is disabled."""
    UNKNOWN = "UNKNOWN"
    """The server gave a reason this SDK does not know."""
    ERROR = "ERROR"
    """The accessor returned the caller default because of ``error_code``."""

    def __str__(self) -> str:
        return self.value


class _DerivedValue:
    """Default of ``FeatureFlag.value``: derive the value from ``variant`` and ``enabled``."""

    def __repr__(self) -> str:
        return "<derived from variant and enabled>"


_DERIVED_VALUE: Any = _DerivedValue()

# Reason codes that carry an error for a config version 2 flag.
_V2_ERROR_REASON_CODES = {
    "missing_group_key": FlagEvaluationErrorCode.INVALID_CONTEXT,
    "dependency_error": FlagEvaluationErrorCode.GENERAL,
    "error": FlagEvaluationErrorCode.GENERAL,
}


@dataclass(frozen=True)
class FeatureFlag:
    """Detailed feature flag evaluation returned by the flags API.

    ``value``, ``failed``, ``reason`` and ``metadata`` are the evaluation details.
    ``enabled``, ``variant`` and ``metadata.payload`` are the value rendered the way
    responses before ``/flags?v=4`` carried it, which the legacy getters return.

    Attributes:
        key: Feature flag key.
        enabled: Whether the flag is enabled for the evaluated user or group.
        variant: Variant key for multivariate flags, otherwise ``None``.
        reason: Optional reason metadata explaining the result.
        metadata: Payload and other metadata returned by the API.
        value: The typed flag value: a boolean, string, number, object, or
            ``None`` when the flag has no value. Defaults to ``variant``, or
            ``enabled`` when there is no variant.
        failed: Whether the flag failed to evaluate, or its record could not be
            read.
        error_code: Why the flag has no usable value, when it has an error.
        error_message: Human-readable detail for ``error_code``.
    """

    key: str
    enabled: bool
    variant: Optional[str]
    reason: Optional[FlagReason]
    metadata: Union[FlagMetadata, LegacyFlagMetadata]
    value: Any = _DERIVED_VALUE
    failed: bool = False
    error_code: Optional[FlagEvaluationErrorCode] = None
    error_message: Optional[str] = None
    # The wire path of the field that made the record unreadable, such as
    # ``"metadata.id"``. ``None`` unless the record could not be read; a record the
    # server marks failed has ``failed`` set and this ``None``.
    _malformed_field: Optional[str] = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.value is _DERIVED_VALUE:
            object.__setattr__(
                self,
                "value",
                self.variant if self.variant is not None else self.enabled,
            )

    def get_value(self) -> FlagValue:
        return self.variant or self.enabled

    @classmethod
    def from_json(cls, resp: Any) -> "FeatureFlag":
        """Parse one flag record of a ``/flags`` response.

        A record with a ``value`` member is a ``/flags?v=4`` record. A record
        without one comes from an older server: its value is ``variant``, or
        ``enabled`` when there is no variant, and its config version is ``1``.
        """
        return _parse_record(resp.get("key"), resp)

    @classmethod
    def from_value_and_payload(
        cls, key: str, value: FlagValue, payload: Any
    ) -> "FeatureFlag":
        enabled, variant = (True, value) if isinstance(value, str) else (value, None)
        return cls(
            key=key,
            enabled=enabled,
            variant=variant,
            reason=None,
            metadata=LegacyFlagMetadata(
                payload=payload,
            ),
        )


class _MalformedRecord(Exception):
    """A known field of a flag record has the wrong JSON type."""


def _read_int(value: Any, field: str) -> Optional[int]:
    # JSON has one number type, so an integral number such as 2.0 is an integer.
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise _MalformedRecord(field)


def _read_typed(value: Any, field: str, expected: type) -> Any:
    if value is None or isinstance(value, expected):
        return value
    raise _MalformedRecord(field)


def _read_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, dict)):
        return value
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return value
    raise _MalformedRecord("value")


def _record_error(
    failed: bool, reason: Optional[FlagReason], config_version: Optional[int]
) -> tuple[Optional[FlagEvaluationErrorCode], Optional[str]]:
    code = None
    if config_version == 2 and reason is not None:
        code = _V2_ERROR_REASON_CODES.get(reason.code)
    if code is None and failed:
        code = FlagEvaluationErrorCode.GENERAL
    if code is None:
        return None, None
    # The reason description is the error message only for a failed record.
    message = reason.description if failed and reason and reason.description else None
    return code, message


def _legacy_rendering(value: Any) -> tuple[bool, Optional[str], Optional[str]]:
    """Render a typed value as ``(enabled, variant, encoded payload)``.

    A string is the variant, ``None`` is disabled, and a number or object is enabled
    with the value as a JSON-encoded payload, as servers before ``/flags?v=4`` sent it.
    """
    if value is None or isinstance(value, bool):
        return bool(value), None, None
    if isinstance(value, str):
        return True, value, None
    return True, None, json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _parse_typed_record(key: str, resp: Any) -> FeatureFlag:
    """Parse a ``/flags?v=4`` record, which carries a typed ``value``.

    Unknown fields are ignored. A known field with the wrong JSON type fails only
    this record, with ``PARSE_ERROR``. An absent or null field reads as absent,
    except ``value``, where null means the flag has no value.
    """
    try:
        if not isinstance(resp, dict):
            raise _MalformedRecord("record")
        _read_record_key(resp)
        value = _read_value(resp.get("value"))
        failed = _read_typed(resp.get("failed"), "failed", bool) is True

        reason = None
        raw_reason = _read_typed(resp.get("reason"), "reason", dict)
        if raw_reason is not None:
            reason = FlagReason(
                code=_read_typed(raw_reason.get("code"), "reason.code", str) or "",
                condition_index=_read_int(
                    raw_reason.get("condition_index"), "reason.condition_index"
                ),
                description=_read_typed(
                    raw_reason.get("description"), "reason.description", str
                )
                or "",
            )

        if failed:
            # A failed record has no value, whatever it carries.
            value = None
        enabled, variant, encoded_payload = _legacy_rendering(value)

        metadata: Union[FlagMetadata, LegacyFlagMetadata]
        raw_metadata = _read_typed(resp.get("metadata"), "metadata", dict)
        if raw_metadata is None:
            metadata = LegacyFlagMetadata(payload=encoded_payload)
        else:
            metadata = FlagMetadata(
                id=_read_int(raw_metadata.get("id"), "metadata.id"),
                # A number or object value is its own payload; a payload the record
                # also carries is ignored.
                payload=encoded_payload
                if encoded_payload is not None
                else raw_metadata.get("payload"),
                version=_read_int(raw_metadata.get("version"), "metadata.version"),
                description=_read_typed(
                    raw_metadata.get("description", ""), "metadata.description", str
                ),
                has_experiment=_read_typed(
                    raw_metadata.get("has_experiment"), "metadata.has_experiment", bool
                ),
                config_version=_read_int(
                    raw_metadata.get("config_version"), "metadata.config_version"
                ),
                rule_type=_read_typed(
                    raw_metadata.get("rule_type"), "metadata.rule_type", str
                ),
                rule_id=_read_typed(
                    raw_metadata.get("rule_id"), "metadata.rule_id", str
                ),
                variant_key=_read_typed(
                    raw_metadata.get("variant_key"), "metadata.variant_key", str
                ),
                forced_variant=_read_typed(
                    raw_metadata.get("forced_variant"), "metadata.forced_variant", bool
                ),
            )
            # Only config version 1 records carry an experiment ID. It is checked,
            # like every known field, but not kept.
            _read_int(raw_metadata.get("experiment_id"), "metadata.experiment_id")
    except _MalformedRecord as error:
        return _malformed_record(key, str(error))
    except RecursionError:
        # A value nested too deeply to encode as a payload fails only this record.
        return _malformed_record(
            key, "value", "The flag record's value is nested too deeply to read."
        )

    config_version = (
        metadata.config_version if isinstance(metadata, FlagMetadata) else None
    )
    error_code, error_message = _record_error(failed, reason, config_version)
    return FeatureFlag(
        key=key,
        enabled=enabled,
        variant=variant,
        reason=reason,
        metadata=metadata,
        value=value,
        failed=failed,
        error_code=error_code,
        error_message=error_message,
    )


FlagMetadataValue = Union[bool, int, float, str]


@dataclass(frozen=True)
class FlagEvaluationDetails(Generic[_T]):
    """The result of a typed flag accessor such as ``get_boolean_details``.

    The fields match OpenFeature's evaluation details, so an OpenFeature provider
    can pass them through.

    Attributes:
        key: Feature flag key.
        value: The flag value, or the caller default when the flag has no value
            of the requested type.
        reason: Why the accessor returned ``value``, as an OpenFeature reason.
        variant: Key of the assigned variant, when the accessor returned the flag's
            value and the server reported a variant key.
        error_code: Why the caller default was returned, when it was returned
            because of an error. A flag without a value, or with the value
            ``False`` read as another type, returns the default without an error.
        error_message: Human-readable detail for ``error_code``.
        flag_metadata: The flag's ``id``, ``version``, ``config_version``,
            ``reason_code``, ``condition_index``, ``rule_type``, ``rule_id`` and
            ``variant_key``, each when the flag record has it. A new dict on every
            call.
    """

    key: str
    value: _T
    reason: FlagEvaluationReason
    variant: Optional[str] = None
    error_code: Optional[FlagEvaluationErrorCode] = None
    error_message: Optional[str] = None
    flag_metadata: Dict[str, FlagMetadataValue] = field(default_factory=dict)


# Every SDK reads numbers as IEEE-754 doubles, which hold every integer up to this
# magnitude exactly. The integer accessor does not accept larger integers, and the
# number accessor reads them as the nearest double, so all SDKs agree.
_MAX_SAFE_INTEGER = 2**53 - 1


def _read_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _NOT_ACCEPTED
    if isinstance(value, int) and abs(value) > _MAX_SAFE_INTEGER:
        return _read_float(value)
    return value


def _read_integer(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) <= _MAX_SAFE_INTEGER
    ):
        return value
    return _NOT_ACCEPTED


def _read_float(value: Any) -> Any:
    if isinstance(value, float):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return float(value)
        except OverflowError:
            # The nearest double is an infinity, as JavaScript parses the number.
            return math.inf if value > 0 else -math.inf
    return _NOT_ACCEPTED


_NOT_ACCEPTED: Any = object()
# Returned by a reader for a value of the right type that it cannot read.
_UNREADABLE: Any = object()


def _read_object(value: Any) -> Any:
    if not isinstance(value, dict):
        return _NOT_ACCEPTED
    try:
        return copy.deepcopy(value)
    except RecursionError:
        # Too deeply nested to copy. A typed read never throws, so the flag reads
        # as unreadable instead.
        return _UNREADABLE


# Reads a flag value as one accessor type, or returns ``_NOT_ACCEPTED`` or
# ``_UNREADABLE``. A returned object is a copy, so a caller cannot change the
# evaluated flag.
_FLAG_VALUE_READERS: Dict[str, Callable[[Any], Any]] = {
    "boolean": lambda value: value if isinstance(value, bool) else _NOT_ACCEPTED,
    "string": lambda value: value if isinstance(value, str) else _NOT_ACCEPTED,
    "number": _read_number,
    "integer": _read_integer,
    "float": _read_float,
    "object": _read_object,
}

# The contract's OpenFeature reasons for config version 2 reason codes that carry
# no error.
_V2_REASONS = {
    "targeting_match": FlagEvaluationReason.TARGETING_MATCH,
    "experiment_split": FlagEvaluationReason.SPLIT,
    "rollout_miss": FlagEvaluationReason.DEFAULT,
    "experiment_paused": FlagEvaluationReason.DEFAULT,
    "holdout": FlagEvaluationReason.DEFAULT,
    "no_rule_match": FlagEvaluationReason.DEFAULT,
}


def _flag_reason(
    value: Any, reason_code: Optional[str], config_version: Optional[int]
) -> FlagEvaluationReason:
    """The OpenFeature reason of a flag record that has no error."""
    if reason_code == "flag_disabled":
        return FlagEvaluationReason.DISABLED
    if config_version == 2:
        return _V2_REASONS.get(reason_code or "", FlagEvaluationReason.UNKNOWN)
    # The contract maps no config version 1 reason code, so the reason follows the
    # value, as PostHog's OpenFeature providers report it.
    if value is True or isinstance(value, str):
        return FlagEvaluationReason.TARGETING_MATCH
    return FlagEvaluationReason.DEFAULT


def _json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    return "number"


def _with_article(type_name: str) -> str:
    return f"an {type_name}" if type_name[0] in "aeiou" else f"a {type_name}"


def _type_mismatch_message(
    key: str, value: Any, value_type: str, config_version: Optional[int]
) -> str:
    actual = _json_type_name(value)
    if actual == "number" and value_type == "integer":
        if isinstance(value, float) and not value.is_integer():
            return f"Flag '{key}' has the number {value!r}, which is not an integer."
        return (
            f"Flag '{key}' has the number {value!r}, which is outside the integer"
            f" range of -{_MAX_SAFE_INTEGER} to {_MAX_SAFE_INTEGER}."
        )
    message = (
        f"Flag '{key}' has {_with_article(actual)} value,"
        f" not {_with_article(value_type)} value."
    )
    if config_version != 2 and value_type != "boolean" and value_type != "string":
        message += (
            " The flag has config version 1, which has number and object values"
            " only as a payload. A server that does not send typed values reads"
            " every flag this way."
        )
    return message


def _resolve_flag_value(
    key: str,
    value: Any,
    default_value: _T,
    value_type: str,
    *,
    config_version: Optional[int],
    reason_code: Optional[str],
    variant: Optional[str],
    flag_metadata: Dict[str, FlagMetadataValue],
    error_code: Optional[FlagEvaluationErrorCode] = None,
    error_message: Optional[str] = None,
) -> FlagEvaluationDetails[_T]:
    """Resolve a flag value for a typed accessor without coercing it.

    The caller default is returned for a failed or unreadable record, a ``None``
    value, ``False`` read as another type, and a value of another type. Only the
    first and the last carry an error code.
    """

    def resolved(
        result: Any,
        code: Optional[FlagEvaluationErrorCode] = None,
        message: Optional[str] = None,
        returned_value: bool = False,
    ) -> FlagEvaluationDetails[Any]:
        return FlagEvaluationDetails(
            key=key,
            value=result,
            reason=FlagEvaluationReason.ERROR
            if code is not None
            else _flag_reason(value, reason_code, config_version),
            variant=variant if returned_value else None,
            error_code=code,
            error_message=message,
            flag_metadata=dict(flag_metadata),
        )

    if error_code is not None:
        return resolved(default_value, error_code, error_message)
    result = _FLAG_VALUE_READERS[value_type](value)
    if result is _UNREADABLE:
        return resolved(
            default_value,
            FlagEvaluationErrorCode.PARSE_ERROR,
            f"Flag '{key}' has an object value nested too deeply to read.",
        )
    if result is not _NOT_ACCEPTED:
        return resolved(result, returned_value=True)
    if value is None or value is False:
        return resolved(default_value)
    return resolved(
        default_value,
        FlagEvaluationErrorCode.TYPE_MISMATCH,
        _type_mismatch_message(key, value, value_type, config_version),
    )


def _resolve_flag_details(
    key: str, flag: Optional[FeatureFlag], default_value: _T, value_type: str
) -> FlagEvaluationDetails[_T]:
    """Resolve a ``/flags`` record for a typed accessor."""
    if flag is None:
        return FlagEvaluationDetails(
            key=key,
            value=default_value,
            reason=FlagEvaluationReason.ERROR,
            error_code=FlagEvaluationErrorCode.FLAG_NOT_FOUND,
            error_message=f"Flag '{key}' is not in the evaluation.",
        )
    metadata = flag.metadata if isinstance(flag.metadata, FlagMetadata) else None
    reason = flag.reason
    flag_metadata: Dict[str, Any] = {}
    if metadata is not None:
        flag_metadata.update(
            id=metadata.id,
            version=metadata.version,
            config_version=metadata.config_version,
        )
    if reason is not None:
        flag_metadata.update(
            reason_code=reason.code or None, condition_index=reason.condition_index
        )
    if metadata is not None:
        flag_metadata.update(
            rule_type=metadata.rule_type,
            rule_id=metadata.rule_id,
            variant_key=metadata.variant_key,
        )
    return _resolve_flag_value(
        key,
        flag.value,
        default_value,
        value_type,
        config_version=metadata.config_version if metadata else None,
        reason_code=reason.code if reason else None,
        variant=metadata.variant_key if metadata else None,
        flag_metadata={k: v for k, v in flag_metadata.items() if v is not None},
        error_code=flag.error_code,
        error_message=flag.error_message,
    )


def _parse_older_record(key: str, resp: Dict[str, Any]) -> FeatureFlag:
    """Parse a record without a typed ``value``, from a server before ``/flags?v=4``.

    Its value is ``variant``, or ``enabled`` when there is no variant, and its config
    version is ``1``. As for a ``/flags?v=4`` record, a known field with the wrong
    JSON type fails only this record.
    """
    try:
        _read_record_key(resp)
        _read_typed(resp.get("enabled"), "enabled", bool)
        _read_typed(resp.get("variant"), "variant", str)
        _read_typed(resp.get("failed"), "failed", bool)
        raw_reason = _read_typed(resp.get("reason"), "reason", dict)
        if raw_reason:
            _read_typed(raw_reason.get("code"), "reason.code", str)
            _read_int(raw_reason.get("condition_index"), "reason.condition_index")
            _read_typed(raw_reason.get("description"), "reason.description", str)
        raw_metadata = _read_typed(resp.get("metadata"), "metadata", dict)
        if raw_metadata:
            _read_int(raw_metadata.get("id"), "metadata.id")
            _read_int(raw_metadata.get("version"), "metadata.version")
            _read_typed(raw_metadata.get("description"), "metadata.description", str)
            _read_typed(
                raw_metadata.get("has_experiment"), "metadata.has_experiment", bool
            )
    except _MalformedRecord as error:
        return _malformed_record(key, str(error))

    reason = FlagReason.from_json(raw_reason)
    failed = resp.get("failed") is True
    error_code, error_message = _record_error(failed, reason, 1)
    return FeatureFlag(
        key=key,
        # An older server always sends ``enabled``; a record without it keeps
        # ``None``, as before.
        enabled=cast(bool, resp.get("enabled")),
        variant=resp.get("variant"),
        reason=reason,
        metadata=FlagMetadata.from_json(raw_metadata),
        failed=failed,
        error_code=error_code,
        error_message=error_message,
    )


def _read_record_key(resp: Dict[str, Any]) -> None:
    # The flag key is the record's map key. A ``key`` member of the wrong type fails
    # the record, like any known field; a string that differs is ignored.
    _read_typed(resp.get("key"), "key", str)


def _malformed_record(
    key: str, malformed_field: str, message: Optional[str] = None
) -> FeatureFlag:
    """A record with a known field of the wrong JSON type: failed, and disabled."""
    flag = FeatureFlag(
        key=key,
        enabled=False,
        variant=None,
        reason=None,
        metadata=LegacyFlagMetadata(payload=None),
        value=None,
        failed=True,
        error_code=FlagEvaluationErrorCode.PARSE_ERROR,
        error_message=message
        or f"The flag record's {malformed_field} field has the wrong JSON type.",
    )
    object.__setattr__(flag, "_malformed_field", malformed_field)
    return flag


def _parse_record(key: str, resp: Any) -> FeatureFlag:
    """Parse one record of a ``/flags`` response, stored under ``key``."""
    if isinstance(resp, dict) and "value" not in resp:
        return _parse_older_record(key, resp)
    return _parse_typed_record(key, resp)


def _has_untyped_flag_records(resp: Any) -> bool:
    """Whether a raw ``/flags`` response has a record without a typed ``value``.

    Such a record comes from a server that does not send ``/flags?v=4`` records.
    """
    if not isinstance(resp, dict):
        return False
    flags = resp.get("flags")
    if isinstance(flags, dict):
        return any(
            isinstance(record, dict) and "value" not in record
            for record in flags.values()
        )
    return bool(resp.get("featureFlags"))


_UNTYPED_FLAGS_RESPONSE_MESSAGE = (
    "[FEATURE FLAGS] The /flags response has no typed flag values, so the server"
    " may not support /flags?v=4. Typed accessors read every flag as config"
    " version 1: number and object values are only available as payloads, and"
    " get_number_value and get_object_value return the default."
)


class FlagsResponse(TypedDict, total=False):
    """Normalized response from the PostHog feature flags API."""

    flags: dict[str, FeatureFlag]
    errorsWhileComputingFlags: bool
    requestId: str
    quotaLimit: Optional[List[str]]
    evaluatedAt: Optional[int]
    minimalFlagCalledEvents: bool


class FlagsAndPayloads(TypedDict, total=True):
    """Feature flag values and payloads keyed by feature flag key."""

    featureFlags: Optional[dict[str, FlagValue]]
    featureFlagPayloads: Optional[dict[str, Any]]


@dataclass(frozen=True)
class FeatureFlagResult:
    """
    The result of calling a feature flag which includes the flag result, variant, and payload.

    Attributes:
        key (str): The unique identifier of the feature flag.
        enabled (bool): Whether the feature flag is enabled for the current context.
        variant (Optional[str]): The variant value if the flag is enabled and has variants, None otherwise.
        payload (Optional[Any]): Additional data associated with the feature flag, if any.
        reason (Optional[str]): A description of why the flag was enabled or disabled, if available.
    """

    key: str
    enabled: bool
    variant: Optional[str]
    payload: Optional[Any]
    reason: Optional[str]

    def get_value(self) -> FlagValue:
        """
        Returns the value of the flag. This is the variant if it exists, otherwise the enabled value.
        This is the value we report as `$feature_flag_response` in the `$feature_flag_called` event.

        Returns:
            FlagValue: Either a string variant or boolean value representing the flag's state.
        """
        return self.variant or self.enabled

    @classmethod
    def from_value_and_payload(
        cls, key: str, value: Union[FlagValue, None], payload: Any
    ) -> Union["FeatureFlagResult", None]:
        """
        Creates a FeatureFlagResult from a flag value and payload.

        Args:
            key (str): The unique identifier of the feature flag.
            value (Union[FlagValue, None]): The value of the flag (string variant or boolean).
            payload (Any): Additional data associated with the feature flag.

        Returns:
            Union[FeatureFlagResult, None]: A new FeatureFlagResult instance, or None if value is None.
        """
        if value is None:
            return None
        enabled, variant = (True, value) if isinstance(value, str) else (value, None)
        return cls(
            key=key,
            enabled=enabled,
            variant=variant,
            payload=_parse_flag_payload(payload),
            reason=None,
        )

    @classmethod
    def from_flag_details(
        cls,
        details: Union[FeatureFlag, None],
        override_match_value: Optional[FlagValue] = None,
    ) -> "FeatureFlagResult | None":
        """
        Create a FeatureFlagResult from a FeatureFlag object.

        Args:
            details (Union[FeatureFlag, None]): The FeatureFlag object to convert.
            override_match_value (Optional[FlagValue]): If provided, this value will be used to populate
                the enabled and variant fields instead of the values from the FeatureFlag.

        Returns:
            FeatureFlagResult | None: A new FeatureFlagResult instance, or None if details is None.
        """

        if details is None:
            return None

        if override_match_value is not None:
            enabled, variant = (
                (True, override_match_value)
                if isinstance(override_match_value, str)
                else (override_match_value, None)
            )
        else:
            enabled, variant = (details.enabled, details.variant)

        return cls(
            key=details.key,
            enabled=enabled,
            variant=variant,
            payload=_parse_flag_payload(details.metadata.payload),
            reason=details.reason.description if details.reason else None,
        )


def normalize_flags_response(resp: Any) -> FlagsResponse:
    """
    Normalize the response from the flags API endpoint into a FlagsResponse.

    Args:
        resp: A v1, v2 or v4 response from the flags API endpoint.

    Returns:
        A FlagsResponse containing feature flags and their details.
    """
    if "requestId" not in resp:
        resp["requestId"] = None
    if "flags" in resp:
        flags = resp["flags"]
        # For each flag, create a FeatureFlag object
        for key, value in flags.items():
            if isinstance(value, FeatureFlag):
                continue
            flags[key] = _parse_record(key, value)
    else:
        # Handle legacy format
        featureFlags = resp.get("featureFlags", {})
        featureFlagPayloads = resp.get("featureFlagPayloads", {})
        resp.pop("featureFlags", None)
        resp.pop("featureFlagPayloads", None)
        # look at each key in featureFlags and create a FeatureFlag object
        flags = {}
        for key, value in featureFlags.items():
            flags[key] = FeatureFlag.from_value_and_payload(
                key, value, featureFlagPayloads.get(key, None)
            )
        resp["flags"] = flags
    return cast(FlagsResponse, resp)


def to_flags_and_payloads(resp: FlagsResponse) -> FlagsAndPayloads:
    """
    Convert a FlagsResponse into a FlagsAndPayloads object which is a
    dict of feature flags and their payloads. This is needed by certain
    functions in the client.
    Args:
        resp: A FlagsResponse containing feature flags and their payloads.

    Returns:
        A tuple containing:
            - A dictionary mapping flag keys to their values (bool or str)
            - A dictionary mapping flag keys to their payloads
    """
    return {"featureFlags": to_values(resp), "featureFlagPayloads": to_payloads(resp)}


def to_values(response: FlagsResponse) -> Optional[dict[str, FlagValue]]:
    if "flags" not in response:
        return None

    flags = response.get("flags", {})
    return {
        key: value.get_value()
        for key, value in flags.items()
        if isinstance(value, FeatureFlag)
    }


def to_payloads(response: FlagsResponse) -> Optional[dict[str, str]]:
    if "flags" not in response:
        return None

    return {
        key: value.metadata.payload
        for key, value in response.get("flags", {}).items()
        if isinstance(value, FeatureFlag)
        and value.enabled
        and value.metadata.payload is not None
    }


class FeatureFlagError:
    """Error type constants for the $feature_flag_error property.

    These values are sent in analytics events to track flag evaluation failures.
    They should not be changed without considering impact on existing dashboards
    and queries that filter on these values.

    Error values:
        ERRORS_WHILE_COMPUTING: Server returned errorsWhileComputingFlags=true
        FLAG_MISSING: Requested flag not in API response
        LOCAL_EVALUATION_INCONCLUSIVE: Requested flag has a loaded definition but
            local evaluation was inconclusive and no remote value filled the gap
        QUOTA_LIMITED: Rate/quota limit exceeded
        TIMEOUT: Request timed out
        CONNECTION_ERROR: Network connectivity issue
        UNKNOWN_ERROR: Unexpected exceptions

    For API errors with status codes, use the api_error() method which returns
    a string like "api_error_500".
    """

    ERRORS_WHILE_COMPUTING = "errors_while_computing_flags"
    FLAG_MISSING = "flag_missing"
    LOCAL_EVALUATION_INCONCLUSIVE = "local_evaluation_inconclusive"
    QUOTA_LIMITED = "quota_limited"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN_ERROR = "unknown_error"

    @staticmethod
    def api_error(status: Union[int, str]) -> str:
        """Generate API error string with status code.

        Args:
            status: HTTP status code from the API error

        Returns:
            Error string like "api_error_500"
        """
        return f"api_error_{status}"
