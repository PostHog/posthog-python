import datetime  # noqa: F401
from typing import Callable, Dict, Optional, Any  # noqa: F401
from typing_extensions import Unpack

from posthog.args import OptionalCaptureArgs, OptionalSetArgs, ExceptionArg
from posthog.client import Client
from posthog.contexts import (
    new_context as inner_new_context,
    scoped as inner_scoped,
    tag as inner_tag,
    set_context_session as inner_set_context_session,
    identify_context as inner_identify_context,
)
from posthog.types import FeatureFlag, FlagsAndPayloads
from posthog.version import VERSION

__version__ = VERSION

"""Context management."""


def new_context(fresh=False, capture_exceptions=True):
    return inner_new_context(fresh=fresh, capture_exceptions=capture_exceptions)


def scoped(fresh=False, capture_exceptions=True):
    return inner_scoped(fresh=fresh, capture_exceptions=capture_exceptions)


def set_context_session(session_id: str):
    return inner_set_context_session(session_id)


def identify_context(distinct_id: str):
    return inner_identify_context(distinct_id)


def tag(name: str, value: Any):
    return inner_tag(name, value)


"""Settings."""
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
feature_flags_request_timeout_seconds = 3  # type: int
super_properties = None  # type: Optional[Dict]
# Currently alpha, use at your own risk
enable_exception_autocapture = False  # type: bool
log_captured_exceptions = False  # type: bool
# Used to determine in app paths for exception autocapture. Defaults to the current working directory
project_root = None  # type: Optional[str]
# Used for our AI observability feature to not capture any prompt or output just usage + metadata
privacy_mode = False  # type: bool

default_client = None  # type: Optional[Client]


# NOTE - this and following functions take unpacked kwargs because we needed to make
# it impossible to write `posthog.capture(distinct-id, event-name)` - basically, to enforce
# the breaking change made between 5.3.0 and 6.0.0. This decision can be unrolled in later
# versions, without a breaking change, to get back the type information in function signatures
def capture(event: str, **kwargs: Unpack[OptionalCaptureArgs]) -> Optional[str]:
    """
    Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up.

    A `capture` call requires
    - `event name` to specify the event
    - We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on.

    Capture takes a number of optional arguments, which are defined by the `OptionalCaptureArgs` type.

    For example:
    ```python
    # Enter a new context (e.g. a request/response cycle, an instance of a background job, etc)
    with posthog.new_context():
        # Associate this context with some user, by distinct_id
        posthog.identify_context('some user')

        # Capture an event, associated with the context-level distinct ID ('some user')
        posthog.capture('movie started')

        # Capture an event associated with some other user (overriding the context-level distinct ID)
        posthog.capture('movie joined', distinct_id='some-other-user')

        # Capture an event with some properties
        posthog.capture('movie played', properties={'movie_id': '123', 'category': 'romcom'})

        # Capture an event with some properties
        posthog.capture('purchase', properties={'product_id': '123', 'category': 'romcom'})
        # Capture an event with some associated group
        posthog.capture('purchase', groups={'company': 'id:5'})

        # Adding a tag to the current context will cause it to appear on all subsequent events
        posthog.tag_context('some-tag', 'some-value')

        posthog.capture('another-event') # Will be captured with `'some-tag': 'some-value'` in the properties dict
    ```
    """

    return _proxy("capture", event, **kwargs)


def set(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
    """
    Set properties on a user record.
    This will overwrite previous people property values. Generally operates similar to `capture`, with
    distinct_id being an optional argument, defaulting to the current context's distinct ID.

    If there is no context-level distinct ID, and no override distinct_id is passed, this function
    will do nothing.

    Context tags are folded into $set properties, so tagging the current context and then calling `set` will
    cause those tags to be set on the user (unlike capture, which causes them to just be set on the event).

     For example:
     ```python
     posthog.set(distinct_id='distinct id', properties={
         'current_browser': 'Chrome',
     })
     ```
    """

    return _proxy("set", **kwargs)


def set_once(**kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
    """
    Set properties on a user record, only if they do not yet exist.
    This will not overwrite previous people property values, unlike `set`.

    Otherwise, operates in an identical manner to `set`.
     ```
    """
    return _proxy("set_once", **kwargs)


def group_identify(
    group_type,  # type: str
    group_key,  # type: str
    properties=None,  # type: Optional[Dict]
    timestamp=None,  # type: Optional[datetime.datetime]
    uuid=None,  # type: Optional[str]
    disable_geoip=None,  # type: Optional[bool]
):
    # type: (...) -> Optional[str]
    """
    Set properties on a group

     A `group_identify` call requires
     - `group_type` type of your group
     - `group_key` unique identifier of the group

     For example:
     ```python
     posthog.group_identify('company', 5, {
         'employees': 11,
     })
     ```
    """

    return _proxy(
        "group_identify",
        group_type=group_type,
        group_key=group_key,
        properties=properties,
        timestamp=timestamp,
        uuid=uuid,
        disable_geoip=disable_geoip,
    )


def alias(
    previous_id,  # type: str
    distinct_id,  # type: str
    timestamp=None,  # type: Optional[datetime.datetime]
    uuid=None,  # type: Optional[str]
    disable_geoip=None,  # type: Optional[bool]
):
    # type: (...) -> Optional[str]
    """
    To marry up whatever a user does before they sign up or log in with what they do after you need to make an alias call.
    This will allow you to answer questions like "Which marketing channels leads to users churning after a month?" or
    "What do users do on our website before signing up?". Particularly useful for associating user behaviour before and after
    they e.g. register, login, or perform some other identifying action.

    An `alias` call requires
    - `previous distinct id` the unique ID of the user before
    - `distinct id` the current unique id

    For example:
    ```python
    posthog.alias('anonymous session id', 'distinct id')
    ```
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
):
    """
    capture_exception allows you to capture exceptions that happen in your code.

    Capture exception is idempotent - if it is called twice with the same exception instance, only a occurrence will be tracked in posthog.
    This is because, generally, contexts will cause exceptions to be captured automatically. However, to ensure you track an exception,
    if you catch and do not re-raise it, capturing it manually is recommended, unless you are certain it will have crossed a context
    boundary (e.g. by existing a `with posthog.new_context():` block already)

    A `capture_exception` call does not require any fields, but we recommend passing an exception of some kind:
    - `exception` to specify the exception to capture. If not provided, the current exception is captured via `sys.exc_info()`

    If the passed exception was raised and caught, the captured stack trace will consist of every frame between where the exception was raised
    and the point at which it is captured (the "traceback").

    If the passed exception was never raised, e.g. if you call `posthog.capture_exception(ValueError("Some Error"))`, the stack trace
    captured will be the full stack trace at the moment the exception was captured.

    Note that heavy use of contexts will lead to truncated stack traces, as the exception will be captured by the context entered most recently,
    which may not be the point you catch the exception for the final time in your code. It's recommended to use contexts sparingly, for this reason.

    `capture_exception` takes the same set of optional arguments as `capture`.
    """

    return _proxy("capture_exception", exception=exception, **kwargs)


def feature_enabled(
    key,  # type: str
    distinct_id,  # type: str
    groups={},  # type: dict
    person_properties={},  # type: dict
    group_properties={},  # type: dict
    only_evaluate_locally=False,  # type: bool
    send_feature_flag_events=True,  # type: bool
    disable_geoip=None,  # type: Optional[bool]
):
    # type: (...) -> bool
    """
    Use feature flags to enable or disable features for users.

    For example:
    ```python
    if posthog.feature_enabled('beta feature', 'distinct id'):
        # do something
    if posthog.feature_enabled('groups feature', 'distinct id', groups={"organization": "5"}):
        # do something
    ```

    You can call `posthog.load_feature_flags()` before to make sure you're not doing unexpected requests.
    """
    return _proxy(
        "feature_enabled",
        key=key,
        distinct_id=distinct_id,
        groups=groups,
        person_properties=person_properties,
        group_properties=group_properties,
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
    )


def get_feature_flag(
    key,  # type: str
    distinct_id,  # type: str
    groups={},  # type: dict
    person_properties={},  # type: dict
    group_properties={},  # type: dict
    only_evaluate_locally=False,  # type: bool
    send_feature_flag_events=True,  # type: bool
    disable_geoip=None,  # type: Optional[bool]
) -> Optional[FeatureFlag]:
    """
    Get feature flag variant for users. Used with experiments.
    Example:
    ```python
    if posthog.get_feature_flag('beta-feature', 'distinct_id') == 'test-variant':
        # do test variant code
    if posthog.get_feature_flag('beta-feature', 'distinct_id') == 'control':
        # do control code
    ```

    `groups` are a mapping from group type to group key. So, if you have a group type of "organization" and a group key of "5",
    you would pass groups={"organization": "5"}.

    `group_properties` take the format: { group_type_name: { group_properties } }

    So, for example, if you have the group type "organization" and the group key "5", with the properties name, and employee count,
    you'll send these as:

    ```python
        group_properties={"organization": {"name": "PostHog", "employees": 11}}
    ```
    """
    return _proxy(
        "get_feature_flag",
        key=key,
        distinct_id=distinct_id,
        groups=groups,
        person_properties=person_properties,
        group_properties=group_properties,
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
    )


def get_all_flags(
    distinct_id,  # type: str
    groups={},  # type: dict
    person_properties={},  # type: dict
    group_properties={},  # type: dict
    only_evaluate_locally=False,  # type: bool
    disable_geoip=None,  # type: Optional[bool]
) -> Optional[dict[str, FeatureFlag]]:
    """
    Get all flags for a given user.
    Example:
    ```python
    flags = posthog.get_all_flags('distinct_id')
    ```

    flags are key-value pairs where the key is the flag key and the value is the flag variant, or True, or False.
    """
    return _proxy(
        "get_all_flags",
        distinct_id=distinct_id,
        groups=groups,
        person_properties=person_properties,
        group_properties=group_properties,
        only_evaluate_locally=only_evaluate_locally,
        disable_geoip=disable_geoip,
    )


def get_feature_flag_payload(
    key,
    distinct_id,
    match_value=None,
    groups={},
    person_properties={},
    group_properties={},
    only_evaluate_locally=False,
    send_feature_flag_events=True,
    disable_geoip=None,  # type: Optional[bool]
) -> Optional[str]:
    return _proxy(
        "get_feature_flag_payload",
        key=key,
        distinct_id=distinct_id,
        match_value=match_value,
        groups=groups,
        person_properties=person_properties,
        group_properties=group_properties,
        only_evaluate_locally=only_evaluate_locally,
        send_feature_flag_events=send_feature_flag_events,
        disable_geoip=disable_geoip,
    )


def get_remote_config_payload(
    key,  # type: str
):
    """Get the payload for a remote config feature flag.

    Args:
        key: The key of the feature flag

    Returns:
        The payload associated with the feature flag. If payload is encrypted, the return value will decrypted

    Note:
        Requires personal_api_key to be set for authentication
    """
    return _proxy(
        "get_remote_config_payload",
        key=key,
    )


def get_all_flags_and_payloads(
    distinct_id,
    groups={},
    person_properties={},
    group_properties={},
    only_evaluate_locally=False,
    disable_geoip=None,  # type: Optional[bool]
) -> FlagsAndPayloads:
    return _proxy(
        "get_all_flags_and_payloads",
        distinct_id=distinct_id,
        groups=groups,
        person_properties=person_properties,
        group_properties=group_properties,
        only_evaluate_locally=only_evaluate_locally,
        disable_geoip=disable_geoip,
    )


def feature_flag_definitions():
    """Returns loaded feature flags, if any. Helpful for debugging what flag information you have loaded."""
    return _proxy("feature_flag_definitions")


def load_feature_flags():
    """Load feature flag definitions from PostHog."""
    return _proxy("load_feature_flags")


def flush():
    """Tell the client to flush."""
    _proxy("flush")


def join():
    """Block program until the client clears the queue"""
    _proxy("join")


def shutdown():
    """Flush all messages and cleanly shutdown the client"""
    _proxy("flush")
    _proxy("join")


def setup():
    global default_client
    if not default_client:
        if not api_key:
            raise ValueError("API key is required")
        default_client = Client(
            api_key,
            host=host,
            debug=debug,
            on_error=on_error,
            send=send,
            sync_mode=sync_mode,
            personal_api_key=personal_api_key,
            poll_interval=poll_interval,
            disabled=disabled,
            disable_geoip=disable_geoip,
            feature_flags_request_timeout_seconds=feature_flags_request_timeout_seconds,
            super_properties=super_properties,
            # TODO: Currently this monitoring begins only when the Client is initialised (which happens when you do something with the SDK)
            # This kind of initialisation is very annoying for exception capture. We need to figure out a way around this,
            # or deprecate this proxy option fully (it's already in the process of deprecation, no new clients should be using this method since like 5-6 months)
            enable_exception_autocapture=enable_exception_autocapture,
            log_captured_exceptions=log_captured_exceptions,
        )

    # always set incase user changes it
    default_client.disabled = disabled
    default_client.debug = debug


def _proxy(method, *args, **kwargs):
    """Create an analytics client if one doesn't exist and send to it."""
    setup()

    fn = getattr(default_client, method)
    return fn(*args, **kwargs)


class Posthog(Client):
    pass
