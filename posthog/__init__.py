import datetime  # noqa: F401
from typing import Any, Callable, Dict, Optional  # noqa: F401

from typing_extensions import Unpack

from posthog.args import (
    ID_TYPES as ID_TYPES,
    ExceptionArg,
    OptionalCaptureArgs,
    OptionalSetArgs,
)
from posthog.client import Client
from posthog.exception_capture import ExceptionCapture
from posthog.contexts import (
    identify_context as inner_identify_context,
)
from posthog.contexts import (
    new_context as inner_new_context,
)
from posthog.contexts import (
    scoped as inner_scoped,
)
from posthog.contexts import (
    set_capture_exception_code_variables_context as inner_set_capture_exception_code_variables_context,
)
from posthog.contexts import (
    set_code_variables_ignore_patterns_context as inner_set_code_variables_ignore_patterns_context,
)
from posthog.contexts import (
    set_code_variables_mask_patterns_context as inner_set_code_variables_mask_patterns_context,
)
from posthog.contexts import (
    set_code_variables_mask_url_credentials_context as inner_set_code_variables_mask_url_credentials_context,
)
from posthog.contexts import (
    set_context_device_id as inner_set_context_device_id,
)
from posthog.contexts import (
    set_context_session as inner_set_context_session,
)
from posthog.contexts import (
    tag as inner_tag,
)
from posthog.contexts import (
    get_tags as inner_get_tags,
)
from posthog.exception_utils import (
    DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_URL_CREDENTIALS,
)
from posthog.feature_flag_evaluations import (
    FeatureFlagEvaluations as FeatureFlagEvaluations,
)
from posthog.feature_flags import (
    InconclusiveMatchError as InconclusiveMatchError,
)
from posthog.feature_flags import (
    RequiresServerEvaluation as RequiresServerEvaluation,
)
from posthog.flag_definition_cache import (
    FlagDefinitionCacheData as FlagDefinitionCacheData,
    FlagDefinitionCacheProvider as FlagDefinitionCacheProvider,
)
from posthog.request import (
    disable_connection_reuse as disable_connection_reuse,
    enable_keep_alive as enable_keep_alive,
    set_socket_options as set_socket_options,
    SocketOptions as SocketOptions,
)
from posthog.types import (
    BeforeSendCallback as BeforeSendCallback,
    FeatureFlag as FeatureFlag,
    FlagValue as FlagValue,
    FlagsAndPayloads as FlagsAndPayloads,
)
from posthog.types import (
    FeatureFlagResult as FeatureFlagResult,
)
from posthog.version import VERSION

__version__ = VERSION

"""Context management."""


def new_context(
    fresh: bool = False,
    capture_exceptions: Optional[bool] = None,
    client: Optional[Client] = None,
):
    """
    Create a new context scope that will be active for the duration of the with block.

    Args:
        fresh: Whether to start with a fresh context (default: False)
        capture_exceptions: Whether to capture exceptions raised within the context. If omitted, defaults to the relevant client's exception autocapture setting.
        client: Optional Posthog client instance to use for this context (default: None)

    Examples:
        ```python
        from posthog import new_context, tag, capture
        with new_context():
            tag("request_id", "123")
            capture("event_name", properties={"property": "value"})
        ```

    Category:
        Contexts
    """
    return inner_new_context(
        fresh=fresh, capture_exceptions=capture_exceptions, client=client
    )


def scoped(fresh=False, capture_exceptions: Optional[bool] = None):
    """
    Decorator that creates a new context for the function.

    Args:
        fresh: Whether to start with a fresh context (default: False)
        capture_exceptions: Whether to capture and track exceptions with posthog error tracking. If omitted, defaults to the global exception autocapture setting.

    Examples:
        ```python
        from posthog import scoped, tag, capture
        @scoped()
        def process_payment(payment_id):
            tag("payment_id", payment_id)
            capture("payment_started")
        ```

    Category:
        Contexts
    """
    return inner_scoped(fresh=fresh, capture_exceptions=capture_exceptions)


def set_context_session(session_id: str):
    """
    Set the session ID for the current context.

    Args:
        session_id: The session ID to associate with the current context and its children

    Examples:
        ```python
        from posthog import set_context_session
        set_context_session("session_123")
        ```

    Category:
        Contexts
    """
    return inner_set_context_session(session_id)


def set_context_device_id(device_id: str):
    """
    Set the device ID for the current context, associating all feature flag requests
    in this or child contexts with the given device ID.

    Args:
        device_id: The device ID to associate with the current context and its children

    Examples:
        ```python
        from posthog import set_context_device_id
        set_context_device_id("device_123")
        ```

    Category:
        Contexts
    """
    return inner_set_context_device_id(device_id)


def identify_context(distinct_id: str):
    """
    Identify the current context with a distinct ID.

    Args:
        distinct_id: The distinct ID to associate with the current context and its children

    Examples:
        ```python
        from posthog import identify_context
        identify_context("user_123")
        ```

    Category:
        Identification
    """
    return inner_identify_context(distinct_id)


def set_capture_exception_code_variables_context(enabled: bool):
    """
    Override code-variable capture for exceptions in the current context.

    Args:
        enabled: Whether exceptions captured in this context should include local
            variable values from stack frames.

    Category:
        Contexts
    """
    return inner_set_capture_exception_code_variables_context(enabled)


def set_code_variables_mask_patterns_context(mask_patterns: list[str]):
    """
    Override code-variable mask patterns for exceptions in the current context.

    Args:
        mask_patterns: Variable-name patterns whose values should be replaced
            with ``***`` when code variables are captured.

    Category:
        Contexts
    """
    return inner_set_code_variables_mask_patterns_context(mask_patterns)


def set_code_variables_ignore_patterns_context(ignore_patterns: list[str]):
    """
    Override code-variable ignore patterns for exceptions in the current context.

    Args:
        ignore_patterns: Variable-name patterns that should be omitted entirely
            when code variables are captured.

    Category:
        Contexts
    """
    return inner_set_code_variables_ignore_patterns_context(ignore_patterns)


def set_code_variables_mask_url_credentials_context(enabled: bool):
    """
    Whether to scrub credentials embedded in URLs/DSNs (e.g. user:pass@host) from
    captured code variables for the current context.
    """
    return inner_set_code_variables_mask_url_credentials_context(enabled)


def tag(name: str, value: Any):
    """
    Add a tag to the current context.

    Args:
        name: The tag key
        value: The tag value

    Examples:
        ```python
        from posthog import tag
        tag("user_id", "123")
        ```

    Category:
        Contexts
    """
    return inner_tag(name, value)


def get_tags() -> Dict[str, Any]:
    """
    Get all tags from the current context.

    Returns:
        Dict of all tags in the current context

    Category:
        Contexts
    """
    return inner_get_tags()


"""Settings.

These module-level settings configure the legacy global PostHog client used by
functions such as ``posthog.capture()``. Set them before your first SDK call.
For new code, prefer creating an explicit ``Posthog``/``Client`` instance with
the corresponding constructor arguments.

Attributes:
    api_key: Project API key/token used by the global client. Missing or blank
        values create a disabled no-op global client.
    host: PostHog ingestion host. Defaults to the US ingestion endpoint when not
        set.
    on_error: Optional callback invoked by background consumers when event upload
        fails.
    debug: Enable verbose SDK logging and re-raise errors from public APIs.
    send: If False, queueing succeeds but events are not sent to PostHog.
    sync_mode: If True, send events synchronously instead of using background
        worker threads.
    disabled: If True, disable captures and API requests. Useful in tests.
    personal_api_key: Personal API key used for local feature flag evaluation
        and remote config payloads.
    poll_interval: Seconds between local feature flag definition refreshes.
    disable_geoip: Whether to disable server-side GeoIP enrichment. Defaults to
        True.
    is_server: Whether events are emitted from a server-side runtime. Defaults to
        True; set to False when using the SDK as a client/CLI so the device OS is
        attributed to the person normally.
    feature_flags_request_timeout_seconds: Timeout in seconds for feature flag
        and remote config requests.
    super_properties: Properties merged into every captured event.
    enable_exception_autocapture: Automatically capture uncaught exceptions.
    log_captured_exceptions: Also log exceptions captured by error tracking.
    before_send: Optional callback that can modify or drop events before upload.
        Return ``None`` to drop an event.
    enable_local_evaluation: Whether to poll feature flag definitions for local
        evaluation when a personal API key is configured.
    flag_definition_cache_provider: Optional external cache provider for sharing
        feature flag definitions across workers.
    capture_exception_code_variables: Capture local variable values on exception
        stack frames.
    code_variables_mask_patterns: Variable-name patterns to mask when capturing
        code variables.
    code_variables_ignore_patterns: Variable-name patterns to omit when capturing
        code variables.
    in_app_modules: Module/package prefixes treated as in-app frames in captured
        exceptions.
    enable_exception_autocapture_rate_limiting: Rate limit autocaptured
        exceptions client-side with a token bucket per exception type. Disabled
        by default.
    exception_autocapture_bucket_size: Maximum burst of autocaptured exceptions
        allowed per exception type (token bucket size, clamped to 0-100).
    exception_autocapture_refill_rate: Tokens restored per refill interval for
        each exception type's bucket.
    exception_autocapture_refill_interval_seconds: Seconds between token refills
        for autocaptured exception rate limiting.
"""
api_key = None  # type: Optional[str]
host = None  # type: Optional[str]
on_error = None  # type: Optional[Callable]
debug = False  # type: bool
send = True  # type: bool
sync_mode = False  # type: bool
disabled = False  # type: bool
personal_api_key = None  # type: Optional[str]
project_api_key = None  # type: Optional[str]
poll_interval = 30  # type: int
disable_geoip = True  # type: bool
is_server = True  # type: bool
feature_flags_request_timeout_seconds = 3  # type: int
super_properties = None  # type: Optional[Dict]
enable_exception_autocapture = False  # type: bool
log_captured_exceptions = False  # type: bool
# Used to determine in app paths for exception autocapture. Defaults to the current working directory
project_root = None  # type: Optional[str]
# Used for our AI observability feature to not capture any prompt or output just usage + metadata
privacy_mode = False  # type: bool
before_send = None  # type: Optional[BeforeSendCallback]
# Whether to enable feature flag polling for local evaluation by default. Defaults to True.
# We recommend setting this to False if you are only using the personalApiKey for evaluating remote config payloads via `get_remote_config_payload` and not using local evaluation.
enable_local_evaluation = True  # type: bool
flag_definition_cache_provider = None  # type: Optional[FlagDefinitionCacheProvider]

default_client = None  # type: Optional[Client]

capture_exception_code_variables = False
code_variables_mask_patterns = DEFAULT_CODE_VARIABLES_MASK_PATTERNS
code_variables_ignore_patterns = DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS
code_variables_mask_url_credentials = DEFAULT_CODE_VARIABLES_MASK_URL_CREDENTIALS
in_app_modules = None  # type: Optional[list[str]]
enable_exception_autocapture_rate_limiting = False  # type: bool
exception_autocapture_bucket_size = ExceptionCapture.DEFAULT_BUCKET_SIZE  # type: int
exception_autocapture_refill_rate = ExceptionCapture.DEFAULT_REFILL_RATE  # type: int
exception_autocapture_refill_interval_seconds = (
    ExceptionCapture.DEFAULT_REFILL_INTERVAL_SECONDS
)  # type: float


# NOTE - this and following functions take unpacked kwargs because we needed to make
# it impossible to write `posthog.capture(distinct-id, event-name)` - basically, to enforce
# the breaking change made between 5.3.0 and 6.0.0. This decision can be unrolled in later
# versions, without a breaking change, to get back the type information in function signatures
def capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]:
    """
    Capture anything a user does within your system.

    Args:
        event: The event name to specify the event
        **kwargs: Optional arguments including:
            distinct_id: Unique identifier for the user
            properties: Dict of event properties
            timestamp: When the event occurred
            uuid: Unique identifier for this event. If omitted, one is generated
                and returned. If provided, it must be a valid UUID string or
                uuid.UUID instance; invalid values are ignored and replaced with
                a newly generated UUID.
            groups: Dict of group types and IDs
            flags: A FeatureFlagEvaluations snapshot from evaluate_flags(). The
                exact values from the snapshot are attached with no extra /flags
                request.
            send_feature_flags: Deprecated. Prefer flags=... from
                evaluate_flags(). When truthy, evaluates flags during capture and
                attaches them to the event.
            disable_geoip: Whether to disable GeoIP lookup

    Details:
        Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up. A capture call requires an event name to specify the event. We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on. Capture takes a number of optional arguments, which are defined by the `OptionalCaptureArgs` type.

    Examples:
        ```python
        # Context and capture usage
        from posthog import new_context, identify_context, tag_context, capture
        # Enter a new context (e.g. a request/response cycle, an instance of a background job, etc)
        with new_context():
            # Associate this context with some user, by distinct_id
            identify_context('some user')

            # Capture an event, associated with the context-level distinct ID ('some user')
            capture('movie started')

            # Capture an event associated with some other user (overriding the context-level distinct ID)
            capture('movie joined', distinct_id='some-other-user')

            # Capture an event with some properties
            capture('movie played', properties={'movie_id': '123', 'category': 'romcom'})

            # Capture an event with some properties
            capture('purchase', properties={'product_id': '123', 'category': 'romcom'})
            # Capture an event with some associated group
            capture('purchase', groups={'company': 'id:5'})

            # Adding a tag to the current context will cause it to appear on all subsequent events
            tag_context('some-tag', 'some-value')

            capture('another-event') # Will be captured with `'some-tag': 'some-value'` in the properties dict
        ```
        ```python
        # Set event properties
        from posthog import capture
        capture(
            "user_signed_up",
            distinct_id="distinct_id_of_the_user",
            properties={
                "login_type": "email",
                "is_free_trial": "true"
            }
        )
        ```
    Category:
        Events
    """

    return _proxy("capture", event, **kwargs)


def set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
    """
    Set properties on a user record.

    Args:
        **kwargs: Optional arguments including:
            distinct_id: Unique identifier for the user. Falls back to the
                context distinct ID; if none exists, this call does nothing.
            properties: Dict of person properties to set.
            timestamp: When the properties were set.
            uuid: Unique identifier for this operation. If omitted, one is
                generated and returned. If provided, it must be a valid UUID
                string or uuid.UUID instance; invalid values are ignored and
                replaced with a newly generated UUID.
            disable_geoip: Whether to disable GeoIP lookup.

    Details:
        This will overwrite previous people property values. Generally operates similar to `capture`, with distinct_id being an optional argument, defaulting to the current context's distinct ID. If there is no context-level distinct ID, and no override distinct_id is passed, this function will do nothing. Context tags are folded into $set properties, so tagging the current context and then calling `set` will cause those tags to be set on the user (unlike capture, which causes them to just be set on the event).

    Examples:
        ```python
        # Set person properties
        from posthog import set
        set(distinct_id='distinct_id', properties={'name': 'Max Hedgehog'})
        ```
    Category:
        Identification
    """

    return _proxy("set", **kwargs)


def set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
    """
    Set properties on a user record, only if they do not yet exist.

    Args:
        **kwargs: Optional arguments including:
            distinct_id: Unique identifier for the user. Falls back to the
                context distinct ID; if none exists, this call does nothing.
            properties: Dict of person properties to set only once.
            timestamp: When the properties were set.
            uuid: Unique identifier for this operation. If omitted, one is
                generated and returned. If provided, it must be a valid UUID
                string or uuid.UUID instance; invalid values are ignored and
                replaced with a newly generated UUID.
            disable_geoip: Whether to disable GeoIP lookup.

    Details:
        This will not overwrite previous people property values, unlike `set`. Otherwise, operates in an identical manner to `set`.

    Examples:
        ```python
        # Set property once
        from posthog import set_once
        set_once(distinct_id='distinct_id', properties={'initial_url': '/blog'})

        ```
    Category:
        Identification
    """
    return _proxy("set_once", **kwargs)


def group_identify(
    group_type: str,
    group_key: str,
    properties: Optional[Dict[str, Any]] = None,
    timestamp: Optional[datetime.datetime] = None,
    uuid: Optional[str] = None,
    disable_geoip: Optional[bool] = None,
    distinct_id: Optional[ID_TYPES] = None,
) -> Optional[str]:
    """
    Set properties on a group.

    Args:
        group_type: Type of your group
        group_key: Unique identifier of the group
        properties: Properties to set on the group
        timestamp: Optional timestamp for the event
        uuid: Optional UUID for the event
        disable_geoip: Whether to disable GeoIP lookup
        distinct_id: Optional distinct ID of the user performing the action

    Examples:
        ```python
        # Group identify
        from posthog import group_identify
        group_identify('company', 'company_id_in_your_db', {
            'name': 'Awesome Inc.',
            'employees': 11
        })
        ```
    Category:
        Identification
    """

    return _proxy(
        "group_identify",
        group_type=group_type,
        group_key=group_key,
        properties=properties,
        timestamp=timestamp,
        uuid=uuid,
        disable_geoip=disable_geoip,
        distinct_id=distinct_id,
    )


def alias(
    previous_id: str,
    distinct_id: str,
    timestamp: Optional[datetime.datetime] = None,
    uuid: Optional[str] = None,
    disable_geoip: Optional[bool] = None,
) -> Optional[str]:
    """
    Associate user behaviour before and after they e.g. register, login, or perform some other identifying action.

    Args:
        previous_id: The unique ID of the user before
        distinct_id: The current unique id
        timestamp: Optional timestamp for the event
        uuid: Optional UUID for the event
        disable_geoip: Whether to disable GeoIP lookup

    Details:
        To marry up whatever a user does before they sign up or log in with what they do after you need to make an alias call. This will allow you to answer questions like "Which marketing channels leads to users churning after a month?" or "What do users do on our website before signing up?". Particularly useful for associating user behaviour before and after they e.g. register, login, or perform some other identifying action.

    Examples:
        ```python
        # Alias user
        from posthog import alias
        alias(previous_id='distinct_id', distinct_id='alias_id')
        ```
    Category:
        Identification
    """

    return _proxy(
        "alias",
        previous_id=previous_id,
        distinct_id=distinct_id,
        timestamp=timestamp,
        uuid=uuid,
        disable_geoip=disable_geoip,
    )


def capture_exception(
    exception: Optional[ExceptionArg] = None,
    **kwargs: Unpack[OptionalCaptureArgs],
) -> Optional[str]:
    """
    Capture exceptions that happen in your code.

    Args:
        exception: The exception to capture. If not provided, the current exception is captured via `sys.exc_info()`
        **kwargs: Optional capture arguments including distinct_id, properties,
            timestamp, uuid, groups, flags, send_feature_flags, and disable_geoip.

    Details:
        Capture exception is idempotent - if it is called twice with the same exception instance, only a occurrence will be tracked in posthog. This is because, generally, contexts will cause exceptions to be captured automatically. However, to ensure you track an exception, if you catch and do not re-raise it, capturing it manually is recommended, unless you are certain it will have crossed a context boundary (e.g. by existing a `with posthog.new_context():` block already). If the passed exception was raised and caught, the captured stack trace will consist of every frame between where the exception was raised and the point at which it is captured (the "traceback"). If the passed exception was never raised, e.g. if you call `posthog.capture_exception(ValueError("Some Error"))`, the stack trace captured will be the full stack trace at the moment the exception was captured. Note that heavy use of contexts will lead to truncated stack traces, as the exception will be captured by the context entered most recently, which may not be the point you catch the exception for the final time in your code. It's recommended to use contexts sparingly, for this reason. `capture_exception` takes the same set of optional arguments as `capture`.

    Examples:
        ```python
        # Capture exception
        from posthog import capture_exception
        try:
            risky_operation()
        except Exception as e:
            capture_exception(e)
        ```
    Category:
        Events
    """

    return _proxy("capture_exception", exception=exception, **kwargs)


def feature_enabled(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[bool]:
    """
    Use feature flags to enable or disable features for users.

    Args:
        key: The feature flag key
        distinct_id: The user's distinct ID
        groups: Groups mapping
        person_properties: Person properties
        group_properties: Group properties
        only_evaluate_locally: Whether to evaluate only locally
        send_feature_flag_events: Whether to send feature flag events
        disable_geoip: Whether to disable GeoIP lookup
        device_id: Optional device ID override for experience-continuity flags

    Details:
        You can call `posthog.load_feature_flags()` before to make sure you're not doing unexpected requests.

    Examples:
        ```python
        # Boolean feature flag
        from posthog import feature_enabled, get_feature_flag_payload
        is_my_flag_enabled = feature_enabled('flag-key', 'distinct_id_of_your_user')
        if is_my_flag_enabled:
            matched_flag_payload = get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
        ```
    Category:
        Feature flags
    """
    return _proxy(
        "feature_enabled",
        key=key,
        distinct_id=distinct_id,
        groups=groups or {},
        person_properties=person_properties or {},
        group_properties=group_properties or {},
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
        device_id=device_id,
    )


def get_feature_flag(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[FlagValue]:
    """
    Get feature flag variant for users. Used with experiments.

    Args:
        key: The feature flag key
        distinct_id: The user's distinct ID
        groups: Groups mapping from group type to group key
        person_properties: Person properties
        group_properties: Group properties in format { group_type_name: { group_properties } }
        only_evaluate_locally: Whether to evaluate only locally
        send_feature_flag_events: Whether to send feature flag events
        disable_geoip: Whether to disable GeoIP lookup
        device_id: Optional device ID override for experience-continuity flags

    Details:
        `groups` are a mapping from group type to group key. So, if you have a group type of "organization" and a group key of "5", you would pass groups={"organization": "5"}. `group_properties` take the format: { group_type_name: { group_properties } }. So, for example, if you have the group type "organization" and the group key "5", with the properties name, and employee count, you'll send these as: group_properties={"organization": {"name": "PostHog", "employees": 11}}.

    Examples:
        ```python
        # Multivariate feature flag
        from posthog import get_feature_flag, get_feature_flag_payload
        enabled_variant = get_feature_flag('flag-key', 'distinct_id_of_your_user')
        if enabled_variant == 'variant-key':
            matched_flag_payload = get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
        ```
    Category:
        Feature flags
    """
    return _proxy(
        "get_feature_flag",
        key=key,
        distinct_id=distinct_id,
        groups=groups or {},
        person_properties=person_properties or {},
        group_properties=group_properties or {},
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
        device_id=device_id,
    )


def get_all_flags(
    distinct_id: ID_TYPES,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
) -> Optional[dict[str, FlagValue]]:
    """
    Get all flags for a given user.

    Args:
        distinct_id: The user's distinct ID
        groups: Groups mapping
        person_properties: Person properties
        group_properties: Group properties
        only_evaluate_locally: Whether to evaluate only locally
        disable_geoip: Whether to disable GeoIP lookup
        device_id: Optional device ID override for experience-continuity flags
        flag_keys_to_evaluate: Optional list of flag keys to evaluate (evaluates all if None)

    Details:
        Flags are key-value pairs where the key is the flag key and the value is the flag variant, or True, or False.

    Examples:
        ```python
        # All flags for user
        from posthog import get_all_flags
        get_all_flags('distinct_id_of_your_user')
        ```
    Category:
        Feature flags
    """
    return _proxy(
        "get_all_flags",
        distinct_id=distinct_id,
        groups=groups or {},
        person_properties=person_properties or {},
        group_properties=group_properties or {},
        only_evaluate_locally=only_evaluate_locally,
        disable_geoip=disable_geoip,
        device_id=device_id,
        flag_keys_to_evaluate=flag_keys_to_evaluate,
    )


def get_feature_flag_result(
    key: str,
    distinct_id: ID_TYPES,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[FeatureFlagResult]:
    """
    Get a FeatureFlagResult object which contains the flag result and payload.

    This method evaluates a feature flag and returns a FeatureFlagResult object containing:
    - enabled: Whether the flag is enabled
    - variant: The variant value if the flag has variants
    - payload: The payload associated with the flag (automatically deserialized from JSON)
    - key: The flag key
    - reason: Why the flag was enabled/disabled

    Args:
        key: The feature flag key.
        distinct_id: The user's distinct ID.
        groups: Mapping of group type to group key.
        person_properties: Person properties to use for evaluation.
        group_properties: Group properties keyed by group type.
        only_evaluate_locally: Whether to evaluate only locally.
        send_feature_flag_events: Whether to send a $feature_flag_called event.
        disable_geoip: Whether to disable GeoIP lookup.
        device_id: Optional device ID override for experience-continuity flags.

    Example:
    ```python
    result = posthog.get_feature_flag_result('beta-feature', 'distinct_id')
    if result and result.enabled:
        # Use the variant and payload
        print(f"Variant: {result.variant}")
        print(f"Payload: {result.payload}")
    ```
    """
    return _proxy(
        "get_feature_flag_result",
        key=key,
        distinct_id=distinct_id,
        groups=groups or {},
        person_properties=person_properties or {},
        group_properties=group_properties or {},
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
        device_id=device_id,
    )


def get_feature_flag_payload(
    key: str,
    distinct_id: ID_TYPES,
    match_value: Optional[FlagValue] = None,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    send_feature_flag_events: bool = True,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
) -> Optional[object]:
    """
    Get the payload associated with a feature flag value.

    Deprecated for new code. Prefer ``evaluate_flags()`` and
    ``flags.get_flag_payload(key)`` so flag evaluation happens once per request.

    Args:
        key: The feature flag key.
        distinct_id: The user's distinct ID.
        match_value: Optional flag value to use when selecting a payload.
        groups: Mapping of group type to group key.
        person_properties: Person properties to use for evaluation.
        group_properties: Group properties keyed by group type.
        only_evaluate_locally: Whether to evaluate only locally.
        send_feature_flag_events: Whether to send a $feature_flag_called event.
        disable_geoip: Whether to disable GeoIP lookup.
        device_id: Optional device ID override for experience-continuity flags.

    Category:
        Feature flags
    """
    return _proxy(
        "get_feature_flag_payload",
        key=key,
        distinct_id=distinct_id,
        match_value=match_value,
        groups=groups or {},
        person_properties=person_properties or {},
        group_properties=group_properties or {},
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
        device_id=device_id,
    )


def get_remote_config_payload(
    key: str,
):
    """Get the payload for a remote config feature flag.

    Args:
        key: The key of the feature flag

    Returns:
        The payload associated with the feature flag. If payload is encrypted, the return value will be decrypted

    Note:
        Requires personal_api_key to be set for authentication
    """
    return _proxy(
        "get_remote_config_payload",
        key=key,
    )


def get_all_flags_and_payloads(
    distinct_id: ID_TYPES,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    device_id: Optional[str] = None,
    flag_keys_to_evaluate: Optional[list[str]] = None,
) -> FlagsAndPayloads:
    """
    Get all feature flag values and payloads for a user.

    Args:
        distinct_id: The user's distinct ID.
        groups: Mapping of group type to group key.
        person_properties: Person properties to use for evaluation.
        group_properties: Group properties keyed by group type.
        only_evaluate_locally: Whether to evaluate only locally.
        disable_geoip: Whether to disable GeoIP lookup.
        device_id: Optional device ID override for experience-continuity flags.
        flag_keys_to_evaluate: Optional list of flag keys to evaluate. Evaluates
            all flags when omitted.

    Returns:
        A dict with ``featureFlags`` and ``featureFlagPayloads`` entries.

    Category:
        Feature flags
    """
    return _proxy(
        "get_all_flags_and_payloads",
        distinct_id=distinct_id,
        groups=groups or {},
        person_properties=person_properties or {},
        group_properties=group_properties or {},
        only_evaluate_locally=only_evaluate_locally,
        disable_geoip=disable_geoip,
        device_id=device_id,
        flag_keys_to_evaluate=flag_keys_to_evaluate,
    )


def evaluate_flags(
    distinct_id: Optional[str] = None,
    groups: Optional[Dict[str, str]] = None,
    person_properties: Optional[Dict[str, Any]] = None,
    group_properties: Optional[Dict[str, Dict[str, Any]]] = None,
    only_evaluate_locally: bool = False,
    disable_geoip: Optional[bool] = None,
    flag_keys: Optional[list[str]] = None,
    device_id: Optional[str] = None,
) -> FeatureFlagEvaluations:
    """Evaluate all feature flags for a user in a single call and return a
    :class:`FeatureFlagEvaluations` snapshot. Branch on ``.is_enabled()`` /
    ``.get_flag()`` and pass the same snapshot to ``capture()`` via the
    ``flags`` option so events carry the exact flag values the code branched on.

    Prefer this over repeated ``get_feature_flag()`` calls and over
    ``capture(send_feature_flags=True)`` — it consolidates flag evaluation into
    a single ``/flags`` request per incoming request.

    Args:
        distinct_id: The user's distinct ID. If ``None``, falls back to the context
            distinct_id. If still unresolvable, returns an empty snapshot.
        groups: Mapping of group type to group key.
        person_properties: Person properties to use for evaluation.
        group_properties: Group properties keyed by group type.
        only_evaluate_locally: If ``True``, never fall back to remote evaluation.
        disable_geoip: Whether to disable GeoIP lookup.
        flag_keys: Optional list of flag keys. When provided, only these flags are
            evaluated — the underlying ``/flags`` request asks the server for just
            this subset, which makes the response smaller and the request cheaper.
            Use this when you only need a handful of flags out of many.
        device_id: Optional device ID override. If not provided, falls back to the
            context device_id (which may be set via tracing headers). Used by
            experience-continuity flags to match users across distinct_id changes.

    Examples:
        ```python
        from posthog import evaluate_flags, capture
        flags = evaluate_flags("user_123", person_properties={"plan": "enterprise"})
        if flags.is_enabled("new-dashboard"):
            render_new_dashboard()
        capture("page_viewed", distinct_id="user_123", flags=flags)
        ```

    Category:
        Feature flags
    """
    return _proxy(
        "evaluate_flags",
        distinct_id=distinct_id,
        groups=groups,
        person_properties=person_properties,
        group_properties=group_properties,
        only_evaluate_locally=only_evaluate_locally,
        disable_geoip=disable_geoip,
        flag_keys=flag_keys,
        device_id=device_id,
    )


def feature_flag_definitions():
    """
    Returns loaded feature flags.

    Details:
        Returns loaded feature flags, if any. Helpful for debugging what flag information you have loaded.

    Examples:
        ```python
        from posthog import feature_flag_definitions
        definitions = feature_flag_definitions()
        ```

    Category:
        Feature flags
    """
    return _proxy("feature_flag_definitions")


def load_feature_flags():
    """
    Load feature flag definitions from PostHog.

    Examples:
        ```python
        from posthog import load_feature_flags
        load_feature_flags()
        ```

    Category:
        Feature flags
    """
    return _proxy("load_feature_flags")


def flush(timeout_seconds: Optional[float] = 10) -> None:
    """
    Tell the client to flush all queued events.

    Args:
        timeout_seconds: Maximum seconds to wait for the queue to flush.
            Defaults to 10 seconds. Pass ``None`` to wait indefinitely.

    Examples:
        ```python
        from posthog import flush
        flush()
        ```

    Category:
        Client management
    """
    _proxy("flush", timeout_seconds=timeout_seconds)


def join() -> None:
    """
    Block program until the client clears the queue. Used during program shutdown. You should use `shutdown()` directly in most cases.

    Examples:
        ```python
        from posthog import join
        join()
        ```

    Category:
        Client management
    """
    _proxy("join")


def shutdown() -> None:
    """
    Flush all messages and cleanly shutdown the client.

    Examples:
        ```python
        from posthog import shutdown
        shutdown()
        ```

    Category:
        Client management
    """
    _proxy("flush")
    _proxy("join")


def setup() -> Client:
    """
    Create or return the global PostHog client configured by module settings.

    Most applications should either instantiate ``Posthog`` directly or set
    ``posthog.api_key``/other module settings before calling top-level helpers.
    ``setup()`` is called automatically by global APIs such as ``capture()``.

    Returns:
        The global ``Client`` instance. If ``api_key`` is missing or blank,
        the client is disabled and module-level calls become no-ops.

    Category:
        Initialization
    """
    global default_client
    if not default_client:
        configured_api_key = api_key.strip() if api_key else ""
        default_client = Client(
            configured_api_key,
            host=host,
            debug=debug,
            on_error=on_error,
            send=send,
            sync_mode=sync_mode,
            personal_api_key=personal_api_key,
            poll_interval=poll_interval,
            disabled=disabled,
            disable_geoip=disable_geoip,
            is_server=is_server,
            feature_flags_request_timeout_seconds=feature_flags_request_timeout_seconds,
            super_properties=super_properties,
            # TODO: Currently this monitoring begins only when the Client is initialised (which happens when you do something with the SDK)
            # This kind of initialisation is very annoying for exception capture. We need to figure out a way around this,
            # or deprecate this proxy option fully (it's already in the process of deprecation, no new clients should be using this method since like 5-6 months)
            enable_exception_autocapture=enable_exception_autocapture,
            log_captured_exceptions=log_captured_exceptions,
            before_send=before_send,
            enable_local_evaluation=enable_local_evaluation,
            flag_definition_cache_provider=flag_definition_cache_provider,
            capture_exception_code_variables=capture_exception_code_variables,
            code_variables_mask_patterns=code_variables_mask_patterns,
            code_variables_ignore_patterns=code_variables_ignore_patterns,
            code_variables_mask_url_credentials=code_variables_mask_url_credentials,
            in_app_modules=in_app_modules,
            enable_exception_autocapture_rate_limiting=enable_exception_autocapture_rate_limiting,
            exception_autocapture_bucket_size=exception_autocapture_bucket_size,
            exception_autocapture_refill_rate=exception_autocapture_refill_rate,
            exception_autocapture_refill_interval_seconds=exception_autocapture_refill_interval_seconds,
        )

    # Always set in case user changes it. Preserve Client's auto-disabled state
    # for API keys that become empty after trimming.
    default_client.disabled = disabled or not default_client.api_key
    default_client.debug = debug
    default_client._set_before_send(before_send)

    return default_client


def _proxy(method, *args, **kwargs):
    """Create an analytics client if one doesn't exist and send to it."""
    setup()

    fn = getattr(default_client, method)
    return fn(*args, **kwargs)


class Posthog(Client):
    """
    Public PostHog SDK client.

    ``Posthog`` is the customer-facing alias for ``Client`` and accepts the same
    constructor arguments. Use it to create an explicit SDK instance instead of
    relying on module-level global configuration.
    """

    pass
