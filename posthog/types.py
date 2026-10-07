import json
import logging
import math
from dataclasses import dataclass
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
        id: Numeric feature flag ID.
        payload: Payload configured for the matched flag value, if any. For a
            number or object value it is the value encoded as JSON, the way
            servers before ``/flags?v=3`` sent it.
        version: Feature flag version.
        description: Feature flag description.
        has_experiment: Whether the flag has a linked experiment. ``None`` when
            the server does not report the field (older deployments).
        config_version: Configuration format of the flag. Responses without a
            typed ``value`` always read as ``1``.
        rule_type: Type of the rule that produced the result, when one did.
        rule_id: ID of the rule that produced the result, when one did.
        experiment_id: ID of the experiment behind the result, when there is one.
        variant_key: Key of the assigned variant, when one was assigned.
        holdout_id: ID of the holdout that excluded the subject, when one did.
        forced_variant: Whether a condition-level variant override produced the
            result.
    """

    id: int
    payload: Optional[str]
    version: int
    description: str
    has_experiment: Optional[bool] = None
    config_version: Optional[int] = None
    rule_type: Optional[str] = None
    rule_id: Optional[str] = None
    experiment_id: Optional[int] = None
    variant_key: Optional[str] = None
    holdout_id: Optional[int] = None
    forced_variant: Optional[bool] = None

    @classmethod
    def from_json(cls, resp: Any) -> Union["FlagMetadata", LegacyFlagMetadata]:
        """Parse the metadata of a record without a typed ``value``.

        Such a record comes from a server before ``/flags?v=3``, so only the
        fields that response carries are read, and ``config_version`` is ``1``.
        """
        if not resp:
            return LegacyFlagMetadata(payload=None)
        raw_has_experiment = resp.get("has_experiment")
        return cls(
            id=resp.get("id", 0),
            payload=resp.get("payload"),
            version=resp.get("version", 0),
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
    responses before ``/flags?v=3`` carried it, which the legacy getters return.

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

        A record with a ``value`` member is a ``/flags?v=3`` record. A record
        without one comes from an older server: its value is ``variant``, or
        ``enabled`` when there is no variant, and its config version is ``1``.
        """
        if "value" in resp:
            return _parse_typed_record(resp.get("key"), resp)

        reason = None
        if resp.get("reason"):
            reason = FlagReason.from_json(resp.get("reason"))

        metadata = None
        if resp.get("metadata"):
            metadata = FlagMetadata.from_json(resp.get("metadata"))
        else:
            metadata = LegacyFlagMetadata(payload=None)

        failed = resp.get("failed") is True
        error_code, error_message = _record_error(failed, reason, 1)
        return cls(
            key=resp.get("key"),
            enabled=resp.get("enabled"),
            variant=resp.get("variant"),
            reason=reason,
            metadata=metadata,
            failed=failed,
            error_code=error_code,
            error_message=error_message,
        )

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
    with the value as a JSON-encoded payload, as servers before ``/flags?v=3`` sent it.
    """
    if value is None or isinstance(value, bool):
        return bool(value), None, None
    if isinstance(value, str):
        return True, value, None
    return True, None, json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _parse_typed_record(key: str, resp: Any) -> FeatureFlag:
    """Parse a ``/flags?v=3`` record, which carries a typed ``value``.

    Unknown fields are ignored. A known field with the wrong JSON type fails only
    this record, with ``PARSE_ERROR``. An absent or null field reads as absent,
    except ``value``, where null means the flag has no value.
    """
    try:
        if not isinstance(resp, dict):
            raise _MalformedRecord("record")
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
            payload = raw_metadata.get("payload")
            metadata = FlagMetadata(
                id=_read_int(raw_metadata.get("id"), "metadata.id") or 0,
                payload=payload if payload is not None else encoded_payload,
                version=_read_int(raw_metadata.get("version"), "metadata.version") or 0,
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
                experiment_id=_read_int(
                    raw_metadata.get("experiment_id"), "metadata.experiment_id"
                ),
                variant_key=_read_typed(
                    raw_metadata.get("variant_key"), "metadata.variant_key", str
                ),
                holdout_id=_read_int(
                    raw_metadata.get("holdout_id"), "metadata.holdout_id"
                ),
                forced_variant=_read_typed(
                    raw_metadata.get("forced_variant"), "metadata.forced_variant", bool
                ),
            )
    except _MalformedRecord as error:
        return FeatureFlag(
            key=key,
            enabled=False,
            variant=None,
            reason=None,
            metadata=LegacyFlagMetadata(payload=None),
            value=None,
            failed=True,
            error_code=FlagEvaluationErrorCode.PARSE_ERROR,
            error_message=f"The flag record's {error} field has the wrong JSON type.",
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


@dataclass(frozen=True)
class FlagEvaluationDetails(Generic[_T]):
    """The result of a typed flag accessor such as ``get_boolean_details``.

    Attributes:
        key: Feature flag key.
        value: The flag value, or the caller default when the flag has no value
            of the requested type.
        variant: Key of the assigned variant, when the server reported one.
        reason: The reason the server gave for the result, when there is one.
        metadata: The flag metadata the server returned, when there is any.
        error_code: Why the caller default was returned, when it was returned
            because of an error. A flag without a value, or with the value
            ``False`` read as another type, returns the default without an error.
        error_message: Human-readable detail for ``error_code``.
    """

    key: str
    value: _T
    variant: Optional[str] = None
    reason: Optional[FlagReason] = None
    metadata: Optional[FlagMetadata] = None
    error_code: Optional[FlagEvaluationErrorCode] = None
    error_message: Optional[str] = None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_FLAG_VALUE_TYPES: Dict[str, Callable[[Any], bool]] = {
    "boolean": lambda value: isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
    "number": _is_number,
    "object": lambda value: isinstance(value, dict),
}


def _resolve_flag_details(
    key: str, flag: Optional[FeatureFlag], default_value: _T, value_type: str
) -> FlagEvaluationDetails[_T]:
    """Resolve a flag record for a typed accessor without coercing its value.

    The caller default is returned for an absent flag, a failed or unreadable record,
    a ``None`` value, and a value of another type. ``False`` read as another type
    returns the default without an error.
    """
    if flag is None:
        return FlagEvaluationDetails(
            key=key,
            value=default_value,
            error_code=FlagEvaluationErrorCode.FLAG_NOT_FOUND,
            error_message=f"Flag '{key}' is not in the evaluation.",
        )
    metadata = flag.metadata if isinstance(flag.metadata, FlagMetadata) else None
    variant = metadata.variant_key if metadata else None

    def resolved(
        value: Any,
        error_code: Optional[FlagEvaluationErrorCode] = None,
        error_message: Optional[str] = None,
    ) -> FlagEvaluationDetails[Any]:
        return FlagEvaluationDetails(
            key=key,
            value=value,
            variant=variant,
            reason=flag.reason,
            metadata=metadata,
            error_code=error_code,
            error_message=error_message,
        )

    if flag.error_code is not None:
        return resolved(default_value, flag.error_code, flag.error_message)
    accepts = _FLAG_VALUE_TYPES[value_type]
    if accepts(flag.value):
        return resolved(flag.value)
    if flag.value is None or flag.value is False:
        return resolved(default_value)
    return resolved(
        default_value,
        FlagEvaluationErrorCode.TYPE_MISMATCH,
        f"Flag '{key}' does not have a {value_type} value.",
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
        resp: A v1, v2 or v3 response from the flags API endpoint.

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
            if not isinstance(value, dict):
                flags[key] = _parse_typed_record(key, value)
                continue
            value["key"] = key
            flags[key] = FeatureFlag.from_json(value)
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
        QUOTA_LIMITED: Rate/quota limit exceeded
        TIMEOUT: Request timed out
        CONNECTION_ERROR: Network connectivity issue
        UNKNOWN_ERROR: Unexpected exceptions

    For API errors with status codes, use the api_error() method which returns
    a string like "api_error_500".
    """

    ERRORS_WHILE_COMPUTING = "errors_while_computing_flags"
    FLAG_MISSING = "flag_missing"
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
