import atexit
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Union
from typing_extensions import Unpack
from uuid import uuid4

from dateutil.tz import tzutc
from six import string_types

from posthog.args import OptionalCaptureArgs, OptionalSetArgs, ID_TYPES, ExceptionArg
from posthog.consumer import Consumer
from posthog.exception_capture import ExceptionCapture
from posthog.exception_utils import (
    exc_info_from_error,
    exceptions_from_error_tuple,
    handle_in_app,
    exception_is_already_captured,
    mark_exception_as_captured,
)
from posthog.feature_flags import InconclusiveMatchError, match_feature_flag_properties
from posthog.poller import Poller
from posthog.request import (
    DEFAULT_HOST,
    APIError,
    batch_post,
    determine_server_host,
    flags,
    get,
    remote_config,
)
from posthog.contexts import (
    _get_current_context,
    get_context_distinct_id,
    get_context_session_id,
    new_context,
)
from posthog.types import (
    FeatureFlag,
    FeatureFlagResult,
    FlagMetadata,
    FlagsAndPayloads,
    FlagsResponse,
    FlagValue,
    normalize_flags_response,
    to_flags_and_payloads,
    to_payloads,
    to_values,
)
from posthog.utils import (
    FlagCache,
    RedisFlagCache,
    SizeLimitedDict,
    clean,
    guess_timezone,
    remove_trailing_slash,
    system_context,
)
from posthog.version import VERSION

try:
    import queue
except ImportError:
    import Queue as queue


MAX_DICT_SIZE = 50_000


def get_identity_state(passed) -> tuple[str, bool]:
    """Returns the distinct id to use, and whether this is a personless event or not"""
    stringified = stringify_id(passed)
    if stringified and len(stringified):
        return (stringified, False)

    context_id = get_context_distinct_id()
    if context_id:
        return (context_id, False)

    return (str(uuid4()), True)


def add_context_tags(properties):
    properties = properties or {}
    current_context = _get_current_context()
    if current_context:
        context_tags = current_context.collect_tags()
        properties["$context_tags"] = set(context_tags.keys())
        # We want explicitly passed properties to override context tags
        context_tags.update(properties)
        properties = context_tags

    if "$session_id" not in properties and get_context_session_id():
        properties["$session_id"] = get_context_session_id()

    return properties


class Client(object):
    """
    This is the SDK reference for the PostHog Python SDK.
    You can learn more about example usage in the [Python SDK documentation](/docs/libraries/python).
    You can also follow [Flask](/docs/libraries/flask) and [Django](/docs/libraries/django)
    guides to integrate PostHog into your project.

    Examples:
        ```python
        from posthog import Posthog
        posthog = Posthog('<ph_project_api_key>', host='<ph_client_api_host>')
        posthog.debug = True
        if settings.TEST:
            posthog.disabled = True
        ```
    """

    log = logging.getLogger("posthog")

    def __init__(
        self,
        project_api_key: str,
        host=None,
        debug=False,
        max_queue_size=10000,
        send=True,
        on_error=None,
        flush_at=100,
        flush_interval=0.5,
        gzip=False,
        max_retries=3,
        sync_mode=False,
        timeout=15,
        thread=1,
        poll_interval=30,
        personal_api_key=None,
        disabled=False,
        disable_geoip=True,
        historical_migration=False,
        feature_flags_request_timeout_seconds=3,
        super_properties=None,
        enable_exception_autocapture=False,
        log_captured_exceptions=False,
        project_root=None,
        privacy_mode=False,
        before_send=None,
        flag_fallback_cache_url=None,
        enable_local_evaluation=True,
    ):
        """
        Initialize a new PostHog client instance.

        Args:
            project_api_key: The project API key.
            host: The host to use for the client.
            debug: Whether to enable debug mode.

        Examples:
            ```python
            from posthog import Posthog

            posthog = Posthog('<ph_project_api_key>', host='<ph_app_host>')
            ```

        Category:
            Initialization
        """
        self.queue = queue.Queue(max_queue_size)

        # api_key: This should be the Team API Key (token), public
        self.api_key = project_api_key

        self.on_error = on_error
        self.debug = debug
        self.send = send
        self.sync_mode = sync_mode
        # Used for session replay URL generation - we don't want the server host here.
        self.raw_host = host or DEFAULT_HOST
        self.host = determine_server_host(host)
        self.gzip = gzip
        self.timeout = timeout
        self._feature_flags = None  # private variable to store flags
        self.feature_flags_by_key = None
        self.group_type_mapping = None
        self.cohorts = None
        self.poll_interval = poll_interval
        self.feature_flags_request_timeout_seconds = (
            feature_flags_request_timeout_seconds
        )
        self.poller = None
        self.distinct_ids_feature_flags_reported = SizeLimitedDict(MAX_DICT_SIZE, set)
        self.flag_cache = self._initialize_flag_cache(flag_fallback_cache_url)
        self.flag_definition_version = 0
        self.disabled = disabled
        self.disable_geoip = disable_geoip
        self.historical_migration = historical_migration
        self.super_properties = super_properties
        self.enable_exception_autocapture = enable_exception_autocapture
        self.log_captured_exceptions = log_captured_exceptions
        self.exception_capture = None
        self.privacy_mode = privacy_mode
        self.enable_local_evaluation = enable_local_evaluation

        if project_root is None:
            try:
                project_root = os.getcwd()
            except Exception:
                project_root = None

        self.project_root = project_root

        # personal_api_key: This should be a generated Personal API Key, private
        self.personal_api_key = personal_api_key
        if debug:
            # Ensures that debug level messages are logged when debug mode is on.
            # Otherwise, defaults to WARNING level. See https://docs.python.org/3/howto/logging.html#what-happens-if-no-configuration-is-provided
            logging.basicConfig()
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.WARNING)

        if before_send is not None:
            if callable(before_send):
                self.before_send = before_send
            else:
                self.log.warning("before_send is not callable, it will be ignored")
                self.before_send = None
        else:
            self.before_send = None

        if self.enable_exception_autocapture:
            self.exception_capture = ExceptionCapture(self)

        if sync_mode:
            self.consumers = None
        else:
            # On program exit, allow the consumer thread to exit cleanly.
            # This prevents exceptions and a messy shutdown when the
            # interpreter is destroyed before the daemon thread finishes
            # execution. However, it is *not* the same as flushing the queue!
            # To guarantee all messages have been delivered, you'll still need
            # to call flush().
            if send:
                atexit.register(self.join)
            for n in range(thread):
                self.consumers = []
                consumer = Consumer(
                    self.queue,
                    self.api_key,
                    host=self.host,
                    on_error=on_error,
                    flush_at=flush_at,
                    flush_interval=flush_interval,
                    gzip=gzip,
                    retries=max_retries,
                    timeout=timeout,
                    historical_migration=historical_migration,
                )
                self.consumers.append(consumer)

                # if we've disabled sending, just don't start the consumer
                if send:
                    consumer.start()

    def new_context(self, fresh=False, capture_exceptions=True):
        """
        Create a new context for managing shared state. Learn more about [contexts](/docs/libraries/python#contexts).

        Args:
            fresh: Whether to create a fresh context that doesn't inherit from parent.
            capture_exceptions: Whether to automatically capture exceptions in this context.

        Examples:
            ```python
            with posthog.new_context():
                identify_context('<distinct_id>')
                posthog.capture('event_name')
            ```

        Category:
            Contexts
        """
        return new_context(
            fresh=fresh, capture_exceptions=capture_exceptions, client=self
        )

    @property
    def feature_flags(self):
        """
        Get the local evaluation feature flags.
        """
        return self._feature_flags

    @feature_flags.setter
    def feature_flags(self, flags):
        """
        Set the local evaluation feature flags.
        """
        self._feature_flags = flags or []
        self.feature_flags_by_key = {
            flag["key"]: flag
            for flag in self._feature_flags
            if flag.get("key") is not None
        }
        assert self.feature_flags_by_key is not None, (
            "feature_flags_by_key should be initialized when feature_flags is set"
        )

    def get_feature_variants(
        self,
        distinct_id,
        groups=None,
        person_properties=None,
        group_properties=None,
        disable_geoip=None,
    ) -> dict[str, Union[bool, str]]:
        """
        Get feature flag variants for a user by calling decide.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.

        Category:
            Feature Flags
        """
        resp_data = self.get_flags_decision(
            distinct_id, groups, person_properties, group_properties, disable_geoip
        )
        return to_values(resp_data) or {}

    def get_feature_payloads(
        self,
        distinct_id,
        groups=None,
        person_properties=None,
        group_properties=None,
        disable_geoip=None,
    ) -> dict[str, str]:
        """
        Get feature flag payloads for a user by calling decide.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            payloads = posthog.get_feature_payloads('<distinct_id>')
            ```

        Category:
            Feature Flags
        """
        resp_data = self.get_flags_decision(
            distinct_id, groups, person_properties, group_properties, disable_geoip
        )
        return to_payloads(resp_data) or {}

    def get_feature_flags_and_payloads(
        self,
        distinct_id,
        groups=None,
        person_properties=None,
        group_properties=None,
        disable_geoip=None,
    ) -> FlagsAndPayloads:
        """
        Get feature flags and payloads for a user by calling decide.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            result = posthog.get_feature_flags_and_payloads('<distinct_id>')
            ```

        Category:
            Feature Flags
        """
        resp = self.get_flags_decision(
            distinct_id, groups, person_properties, group_properties, disable_geoip
        )
        return to_flags_and_payloads(resp)

    def get_flags_decision(
        self,
        distinct_id: Optional[ID_TYPES] = None,
        groups: Optional[dict] = None,
        person_properties=None,
        group_properties=None,
        disable_geoip=None,
    ) -> FlagsResponse:
        """
        Get feature flags decision.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            decision = posthog.get_flags_decision('user123')
            ```

        Category:
            Feature Flags
        """
        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        if distinct_id is None:
            distinct_id = get_context_distinct_id()

        if disable_geoip is None:
            disable_geoip = self.disable_geoip

        if not groups:
            groups = {}

        request_data = {
            "distinct_id": distinct_id,
            "groups": groups,
            "person_properties": person_properties,
            "group_properties": group_properties,
            "geoip_disable": disable_geoip,
        }

        resp_data = flags(
            self.api_key,
            self.host,
            timeout=self.feature_flags_request_timeout_seconds,
            **request_data,
        )

        return normalize_flags_response(resp_data)

    def capture(
        self, event: str, **kwargs: Unpack[OptionalCaptureArgs]
    ) -> Optional[str]:
        """
        Captures an event manually. [Learn about capture best practices](https://posthog.com/docs/product-analytics/capture-events)

        Args:
            event: The event name to capture.
            distinct_id: The distinct ID of the user.
            properties: A dictionary of properties to include with the event.
            timestamp: The timestamp of the event.
            uuid: A unique identifier for the event.
            groups: A dictionary of group information.
            send_feature_flags: Whether to send feature flags with the event.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            # Anonymous event
            posthog.capture('some-anon-event')
            ```
            ```python
            # Context usage
            from posthog import identify_context, new_context
            with new_context():
                identify_context('distinct_id_of_the_user')
                posthog.capture('user_signed_up')
                posthog.capture('user_logged_in')
                posthog.capture('some-custom-action', distinct_id='distinct_id_of_the_user')
            ```
            ```python
            # Set event properties
            posthog.capture(
                "user_signed_up",
                distinct_id="distinct_id_of_the_user",
                properties={
                    "login_type": "email",
                    "is_free_trial": "true"
                }
            )
            ```
            ```python
            # Page view event
            posthog.capture('$pageview', distinct_id="distinct_id_of_the_user", properties={'$current_url': 'https://example.com'})
            ```

        Category:
            Capture
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        timestamp = kwargs.get("timestamp", None)
        uuid = kwargs.get("uuid", None)
        groups = kwargs.get("groups", None)
        send_feature_flags = kwargs.get("send_feature_flags", False)
        disable_geoip = kwargs.get("disable_geoip", None)

        properties = {**(properties or {}), **system_context()}

        properties = add_context_tags(properties)
        assert properties is not None  # Type hint for mypy

        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless and "$process_person_profile" not in properties:
            properties["$process_person_profile"] = False

        msg = {
            "properties": properties,
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "event": event,
            "uuid": uuid,
        }

        if groups:
            properties["$groups"] = groups

        extra_properties: dict[str, Any] = {}
        feature_variants: Optional[dict[str, Union[bool, str]]] = {}

        # Parse and normalize send_feature_flags parameter
        flag_options = self._parse_send_feature_flags(send_feature_flags)

        if flag_options["should_send"]:
            try:
                if flag_options["only_evaluate_locally"] is True:
                    # Only use local evaluation
                    feature_variants = self.get_all_flags(
                        distinct_id,
                        groups=(groups or {}),
                        person_properties=flag_options["person_properties"],
                        group_properties=flag_options["group_properties"],
                        disable_geoip=disable_geoip,
                        only_evaluate_locally=True,
                    )
                else:
                    # Default behavior - use remote evaluation
                    feature_variants = self.get_feature_variants(
                        distinct_id,
                        groups,
                        person_properties=flag_options["person_properties"],
                        group_properties=flag_options["group_properties"],
                        disable_geoip=disable_geoip,
                    )
            except Exception as e:
                self.log.exception(
                    f"[FEATURE FLAGS] Unable to get feature variants: {e}"
                )

        elif self.feature_flags and event != "$feature_flag_called":
            # Local evaluation is enabled, flags are loaded, so try and get all flags we can without going to the server
            feature_variants = self.get_all_flags(
                distinct_id,
                groups=(groups or {}),
                disable_geoip=disable_geoip,
                only_evaluate_locally=True,
            )

        for feature, variant in (feature_variants or {}).items():
            extra_properties[f"$feature/{feature}"] = variant

        active_feature_flags = [
            key
            for (key, value) in (feature_variants or {}).items()
            if value is not False
        ]
        if active_feature_flags:
            extra_properties["$active_feature_flags"] = active_feature_flags

        if extra_properties:
            properties = {**extra_properties, **properties}
            msg["properties"] = properties

        return self._enqueue(msg, disable_geoip)

    def _parse_send_feature_flags(self, send_feature_flags) -> dict:
        """
        Parse and normalize send_feature_flags parameter into a standard format.

        Args:
            send_feature_flags: Either bool or SendFeatureFlagsOptions dict

        Returns:
            dict: Normalized options with keys: should_send, only_evaluate_locally,
                  person_properties, group_properties

        Raises:
            TypeError: If send_feature_flags is not bool or dict
        """
        if isinstance(send_feature_flags, dict):
            return {
                "should_send": True,
                "only_evaluate_locally": send_feature_flags.get(
                    "only_evaluate_locally"
                ),
                "person_properties": send_feature_flags.get("person_properties"),
                "group_properties": send_feature_flags.get("group_properties"),
            }
        elif isinstance(send_feature_flags, bool):
            return {
                "should_send": send_feature_flags,
                "only_evaluate_locally": None,
                "person_properties": None,
                "group_properties": None,
            }
        else:
            raise TypeError(
                f"Invalid type for send_feature_flags: {type(send_feature_flags)}. "
                f"Expected bool or dict."
            )

    def set(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        """
        Set properties on a person profile.

        Args:
            distinct_id: The distinct ID of the user.
            properties: A dictionary of properties to set.
            timestamp: The timestamp of the event.
            uuid: A unique identifier for the event.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            # Set with distinct id
            posthog.capture(
                'event_name',
                distinct_id='user-distinct-id',
                properties={
                    '$set': {'name': 'Max Hedgehog'},
                    '$set_once': {'initial_url': '/blog'}
                }
            )
            ```
            ```python
            # Set using context
            from posthog import new_context, identify_context
            with new_context():
                identify_context('user-distinct-id')
                posthog.capture('event_name')
            ```

        Category:
            Identification
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        timestamp = kwargs.get("timestamp", None)
        uuid = kwargs.get("uuid", None)
        disable_geoip = kwargs.get("disable_geoip", None)

        properties = properties or {}

        properties = add_context_tags(properties)

        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless or not properties:
            return None  # Personless set() does nothing

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set": properties,
            "event": "$set",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def set_once(self, **kwargs: Unpack[OptionalSetArgs]) -> Optional[str]:
        """
        Set properties on a person profile only if they haven't been set before.

        Args:
            distinct_id: The distinct ID of the user.
            properties: A dictionary of properties to set once.
            timestamp: The timestamp of the event.
            uuid: A unique identifier for the event.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            posthog.set_once(distinct_id='user123', properties={'initial_signup_date': '2024-01-01'})
            ```

        Category:
            Identification
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        timestamp = kwargs.get("timestamp", None)
        uuid = kwargs.get("uuid", None)
        disable_geoip = kwargs.get("disable_geoip", None)
        properties = properties or {}

        properties = add_context_tags(properties)

        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless or not properties:
            return None  # Personless set_once() does nothing

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set_once": properties,
            "event": "$set_once",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def group_identify(
        self,
        group_type: str,
        group_key: str,
        properties: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Union[datetime, str]] = None,
        uuid: Optional[str] = None,
        disable_geoip: Optional[bool] = None,
        distinct_id: Optional[ID_TYPES] = None,
    ) -> Optional[str]:
        """
        Identify a group and set its properties.

        Args:
            group_type: The type of group (e.g., 'company', 'team').
            group_key: The unique identifier for the group.
            properties: A dictionary of properties to set on the group.
            timestamp: The timestamp of the event.
            uuid: A unique identifier for the event.
            disable_geoip: Whether to disable GeoIP for this event.
            distinct_id: The distinct ID of the user performing the action.

        Examples:
            ```python
            posthog.group_identify('company', 'company_id_in_your_db', {
                'name': 'Awesome Inc.',
                'employees': 11
            })
            ```

        Category:
            Identification
        """
        properties = properties or {}

        # group_identify is purposefully always personful
        distinct_id = get_identity_state(distinct_id)[0]

        msg: Dict[str, Any] = {
            "event": "$groupidentify",
            "properties": {
                "$group_type": group_type,
                "$group_key": group_key,
                "$group_set": properties,
            },
            "distinct_id": distinct_id,
            "timestamp": timestamp,
            "uuid": uuid,
        }

        # NOTE - group_identify doesn't generally use context properties - should it?
        if get_context_session_id():
            msg["properties"]["$session_id"] = str(get_context_session_id())

        return self._enqueue(msg, disable_geoip)

    def alias(
        self,
        previous_id: str,
        distinct_id: Optional[str],
        timestamp=None,
        uuid=None,
        disable_geoip=None,
    ):
        """
        Create an alias between two distinct IDs.

        Args:
            previous_id: The previous distinct ID.
            distinct_id: The new distinct ID to alias to.
            timestamp: The timestamp of the event.
            uuid: A unique identifier for the event.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            posthog.alias(previous_id='distinct_id', distinct_id='alias_id')
            ```

        Category:
            Identification
        """
        (distinct_id, personless) = get_identity_state(distinct_id)

        if personless:
            return None  # Personless alias() does nothing - should this throw?

        msg = {
            "properties": {
                "distinct_id": previous_id,
                "alias": distinct_id,
            },
            "timestamp": timestamp,
            "event": "$create_alias",
            "distinct_id": previous_id,
            "uuid": uuid,
        }

        if get_context_session_id():
            msg["properties"]["$session_id"] = str(get_context_session_id())

        return self._enqueue(msg, disable_geoip)

    def capture_exception(
        self,
        exception: Optional[ExceptionArg],
        **kwargs: Unpack[OptionalCaptureArgs],
    ):
        """
        Capture an exception for error tracking.

        Args:
            exception: The exception to capture.
            distinct_id: The distinct ID of the user.
            properties: A dictionary of additional properties.
            send_feature_flags: Whether to send feature flags with the exception.
            disable_geoip: Whether to disable GeoIP for this event.

        Examples:
            ```python
            try:
                # Some code that might fail
                pass
            except Exception as e:
                posthog.capture_exception(e, 'user_distinct_id', properties=additional_properties)
            ```

        Category:
            Error Tracking
        """
        distinct_id = kwargs.get("distinct_id", None)
        properties = kwargs.get("properties", None)
        send_feature_flags = kwargs.get("send_feature_flags", False)
        disable_geoip = kwargs.get("disable_geoip", None)
        # this function shouldn't ever throw an error, so it logs exceptions instead of raising them.
        # this is important to ensure we don't unexpectedly re-raise exceptions in the user's code.
        try:
            properties = properties or {}

            # Check if this exception has already been captured
            if exception is not None and exception_is_already_captured(exception):
                self.log.debug("Exception already captured, skipping")
                return None

            if exception is not None:
                exc_info = exc_info_from_error(exception)
            else:
                exc_info = sys.exc_info()

            if exc_info is None or exc_info == (None, None, None):
                self.log.warning("No exception information available")
                return None

            # Format stack trace for cymbal
            all_exceptions_with_trace = exceptions_from_error_tuple(exc_info)

            # Add in-app property to frames in the exceptions
            event = handle_in_app(
                {
                    "exception": {
                        "values": all_exceptions_with_trace,
                    },
                },
                project_root=self.project_root,
            )
            all_exceptions_with_trace_and_in_app = event["exception"]["values"]

            properties = {
                "$exception_type": all_exceptions_with_trace_and_in_app[0].get("type"),
                "$exception_message": all_exceptions_with_trace_and_in_app[0].get(
                    "value"
                ),
                "$exception_list": all_exceptions_with_trace_and_in_app,
                "$exception_personURL": f"{remove_trailing_slash(self.raw_host)}/project/{self.api_key}/person/{distinct_id}",
                **properties,
            }

            if self.log_captured_exceptions:
                self.log.exception(exception, extra=kwargs)

            timestamp = kwargs.get("timestamp", None)
            uuid = kwargs.get("uuid", None)
            groups = kwargs.get("groups", None)
            res = self.capture(
                "$exception",
                distinct_id=distinct_id,
                properties=properties,
                timestamp=timestamp,
                uuid=uuid,
                groups=groups,
                send_feature_flags=send_feature_flags,
                disable_geoip=disable_geoip,
            )

            # Mark the exception as captured to prevent duplicate captures
            if exception is not None and res is not None:
                mark_exception_as_captured(exception, res)

            return res
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")

    def _enqueue(self, msg, disable_geoip):
        # type: (...) -> Optional[str]
        """Push a new `msg` onto the queue, return `(success, msg)`"""

        if self.disabled:
            return None

        timestamp = msg["timestamp"]
        if timestamp is None:
            timestamp = datetime.now(tz=tzutc())

        # add common
        timestamp = guess_timezone(timestamp)
        msg["timestamp"] = timestamp.isoformat()

        if "uuid" in msg:
            uuid = msg.pop("uuid")
            if uuid:
                msg["uuid"] = stringify_id(uuid)

        if "uuid" not in msg:
            # Always send a uuid, so we can always return one
            msg["uuid"] = stringify_id(uuid4())

        sent_uuid = msg["uuid"]

        if not msg.get("properties"):
            msg["properties"] = {}
        msg["properties"]["$lib"] = "posthog-python"
        msg["properties"]["$lib_version"] = VERSION

        if disable_geoip is None:
            disable_geoip = self.disable_geoip

        if disable_geoip:
            msg["properties"]["$geoip_disable"] = True

        if self.super_properties:
            msg["properties"] = {**msg["properties"], **self.super_properties}

        msg["distinct_id"] = stringify_id(msg.get("distinct_id", None))

        msg = clean(msg)

        if self.before_send:
            try:
                modified_msg = self.before_send(msg)
                if modified_msg is None:
                    self.log.debug("Event dropped by before_send callback")
                    return None
                msg = modified_msg
            except Exception as e:
                self.log.exception(f"Error in before_send callback: {e}")
                # Continue with the original message if callback fails

        self.log.debug("queueing: %s", msg)

        # if send is False, return msg as if it was successfully queued
        if not self.send:
            return sent_uuid

        if self.sync_mode:
            self.log.debug("enqueued with blocking %s.", msg["event"])
            batch_post(
                self.api_key,
                self.host,
                gzip=self.gzip,
                timeout=self.timeout,
                batch=[msg],
                historical_migration=self.historical_migration,
            )

            return sent_uuid

        try:
            self.queue.put(msg, block=False)
            self.log.debug("enqueued %s.", msg["event"])
            return sent_uuid
        except queue.Full:
            self.log.warning("analytics-python queue is full")
            return None

    def flush(self):
        """
        Force a flush from the internal queue to the server. Do not use directly, call `shutdown()` instead.

        Examples:
            ```python
            posthog.capture('event_name')
            posthog.flush()  # Ensures the event is sent immediately
            ```
        """
        queue = self.queue
        size = queue.qsize()
        queue.join()
        # Note that this message may not be precise, because of threading.
        self.log.debug("successfully flushed about %s items.", size)

    def join(self):
        """
        End the consumer thread once the queue is empty. Do not use directly, call `shutdown()` instead.

        Examples:
            ```python
            posthog.join()
            ```
        """
        for consumer in self.consumers:
            consumer.pause()
            try:
                consumer.join()
            except RuntimeError:
                # consumer thread has not started
                pass

        if self.poller:
            self.poller.stop()

    def shutdown(self):
        """
        Flush all messages and cleanly shutdown the client. Call this before the process ends in serverless environments to avoid data loss.

        Examples:
            ```python
            posthog.shutdown()
            ```
        """
        self.flush()
        self.join()

        if self.exception_capture:
            self.exception_capture.close()

    def _load_feature_flags(self):
        try:
            # Store old flags to detect changes
            old_flags_by_key: dict[str, dict] = self.feature_flags_by_key or {}

            response = get(
                self.personal_api_key,
                f"/api/feature_flag/local_evaluation/?token={self.api_key}&send_cohorts",
                self.host,
                timeout=10,
            )

            self.feature_flags = response["flags"] or []
            self.group_type_mapping = response["group_type_mapping"] or {}
            self.cohorts = response["cohorts"] or {}

            # Check if flag definitions changed and update version
            if self.flag_cache and old_flags_by_key != (
                self.feature_flags_by_key or {}
            ):
                old_version = self.flag_definition_version
                self.flag_definition_version += 1
                self.flag_cache.invalidate_version(old_version)

        except APIError as e:
            if e.status == 401:
                self.log.error(
                    "[FEATURE FLAGS] Error loading feature flags: To use feature flags, please set a valid personal_api_key. More information: https://posthog.com/docs/api/overview"
                )
                if self.debug:
                    raise APIError(
                        status=401,
                        message="You are using a write-only key with feature flags. "
                        "To use feature flags, please set a personal_api_key "
                        "More information: https://posthog.com/docs/api/overview",
                    )
            elif e.status == 402:
                self.log.warning(
                    "[FEATURE FLAGS] PostHog feature flags quota limited, resetting feature flag data.  Learn more about billing limits at https://posthog.com/docs/billing/limits-alerts"
                )
                # Reset all feature flag data when quota limited
                self.feature_flags = []
                self.group_type_mapping = {}
                self.cohorts = {}

                # Clear flag cache when quota limited
                if self.flag_cache:
                    self.flag_cache.clear()

                if self.debug:
                    raise APIError(
                        status=402,
                        message="PostHog feature flags quota limited",
                    )
            else:
                self.log.error(f"[FEATURE FLAGS] Error loading feature flags: {e}")
        except Exception as e:
            self.log.warning(
                "[FEATURE FLAGS] Fetching feature flags failed with following error. We will retry in %s seconds."
                % self.poll_interval
            )
            self.log.warning(e)

        self._last_feature_flag_poll = datetime.now(tz=tzutc())

    def load_feature_flags(self):
        """
        Load feature flags for local evaluation.

        Examples:
            ```python
            posthog.load_feature_flags()
            ```

        Category:
            Feature Flags
        """
        if not self.personal_api_key:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a personal_api_key to use feature flags."
            )
            self.feature_flags = []
            return

        self._load_feature_flags()

        # Only start the poller if local evaluation is enabled
        if self.enable_local_evaluation and not (
            self.poller and self.poller.is_alive()
        ):
            self.poller = Poller(
                interval=timedelta(seconds=self.poll_interval),
                execute=self._load_feature_flags,
            )
            self.poller.start()

    def _compute_flag_locally(
        self,
        feature_flag,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        warn_on_unknown_groups=True,
    ) -> FlagValue:
        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        if feature_flag.get("ensure_experience_continuity", False):
            raise InconclusiveMatchError("Flag has experience continuity enabled")

        if not feature_flag.get("active"):
            return False

        flag_filters = feature_flag.get("filters") or {}
        aggregation_group_type_index = flag_filters.get("aggregation_group_type_index")
        if aggregation_group_type_index is not None:
            group_name = self.group_type_mapping.get(str(aggregation_group_type_index))

            if not group_name:
                self.log.warning(
                    f"[FEATURE FLAGS] Unknown group type index {aggregation_group_type_index} for feature flag {feature_flag['key']}"
                )
                # failover to `/decide/`
                raise InconclusiveMatchError("Flag has unknown group type index")

            if group_name not in groups:
                # Group flags are never enabled in `groups` aren't passed in
                # don't failover to `/decide/`, since response will be the same
                if warn_on_unknown_groups:
                    self.log.warning(
                        f"[FEATURE FLAGS] Can't compute group feature flag: {feature_flag['key']} without group names passed in"
                    )
                else:
                    self.log.debug(
                        f"[FEATURE FLAGS] Can't compute group feature flag: {feature_flag['key']} without group names passed in"
                    )
                return False

            focused_group_properties = group_properties[group_name]
            return match_feature_flag_properties(
                feature_flag, groups[group_name], focused_group_properties
            )
        else:
            return match_feature_flag_properties(
                feature_flag, distinct_id, person_properties, self.cohorts
            )

    def feature_enabled(
        self,
        key,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ):
        """
        Check if a feature flag is enabled for a user.

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')
            if is_my_flag_enabled:
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
            ```

        Category:
            Feature Flags
        """
        response = self.get_feature_flag(
            key,
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
        )

        if response is None:
            return None
        return bool(response)

    def _get_feature_flag_result(
        self,
        key: str,
        distinct_id: ID_TYPES,
        *,
        override_match_value: Optional[FlagValue] = None,
        groups: Optional[Dict[str, str]] = None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ) -> Optional[FeatureFlagResult]:
        if self.disabled:
            return None

        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                distinct_id,
                groups or {},
                person_properties or {},
                group_properties or {},
            )
        )
        # Ensure non-None values for type checking
        groups = groups or {}
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        flag_result = None
        flag_details = None
        request_id = None

        flag_value = self._locally_evaluate_flag(
            key, distinct_id, groups, person_properties, group_properties
        )
        flag_was_locally_evaluated = flag_value is not None

        if flag_was_locally_evaluated:
            lookup_match_value = override_match_value or flag_value
            payload = (
                self._compute_payload_locally(key, lookup_match_value)
                if lookup_match_value is not None
                else None
            )
            flag_result = FeatureFlagResult.from_value_and_payload(
                key, lookup_match_value, payload
            )

            # Cache successful local evaluation
            if self.flag_cache and flag_result:
                self.flag_cache.set_cached_flag(
                    distinct_id, key, flag_result, self.flag_definition_version
                )
        elif not only_evaluate_locally:
            try:
                flag_details, request_id = self._get_feature_flag_details_from_decide(
                    key,
                    distinct_id,
                    groups,
                    person_properties,
                    group_properties,
                    disable_geoip,
                )
                flag_result = FeatureFlagResult.from_flag_details(
                    flag_details, override_match_value
                )

                # Cache successful remote evaluation
                if self.flag_cache and flag_result:
                    self.flag_cache.set_cached_flag(
                        distinct_id, key, flag_result, self.flag_definition_version
                    )

                self.log.debug(
                    f"Successfully computed flag remotely: #{key} -> #{flag_result}"
                )
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get flag remotely: {e}")

                # Fallback to cached value if remote evaluation fails
                if self.flag_cache:
                    stale_result = self.flag_cache.get_stale_cached_flag(
                        distinct_id, key
                    )
                    if stale_result:
                        self.log.info(
                            f"[FEATURE FLAGS] Using stale cached value for flag {key}"
                        )
                        flag_result = stale_result

        if send_feature_flag_events:
            self._capture_feature_flag_called(
                distinct_id,
                key,
                flag_result.get_value() if flag_result else None,
                flag_result.payload if flag_result else None,
                flag_was_locally_evaluated,
                groups,
                disable_geoip,
                request_id,
                flag_details,
            )

        return flag_result

    def get_feature_flag_result(
        self,
        key,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ) -> Optional[FeatureFlagResult]:
        """
        Get a FeatureFlagResult object which contains the flag result and payload for a key by evaluating locally or remotely
        depending on whether local evaluation is enabled and the flag can be locally evaluated.
        This also captures the `$feature_flag_called` event unless `send_feature_flag_events` is `False`.

        Examples:
            ```python
            flag_result = posthog.get_feature_flag_result('flag-key', 'distinct_id_of_your_user')
            if flag_result and flag_result.get_value() == 'variant-key':
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = flag_result.payload
            ```

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.

        Returns:
            Optional[FeatureFlagResult]: The feature flag result or None if disabled/not found.
        """
        return self._get_feature_flag_result(
            key,
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
        )

    def get_feature_flag(
        self,
        key,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ) -> Optional[FlagValue]:
        """
        Get multivariate feature flag value for a user.

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            enabled_variant = posthog.get_feature_flag('flag-key', 'distinct_id_of_your_user')
            if enabled_variant == 'variant-key': # replace 'variant-key' with the key of your variant
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
            ```

        Category:
            Feature Flags
        """
        feature_flag_result = self.get_feature_flag_result(
            key,
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
        )
        return feature_flag_result.get_value() if feature_flag_result else None

    def _locally_evaluate_flag(
        self,
        key: str,
        distinct_id: ID_TYPES,
        groups: dict[str, str],
        person_properties: dict[str, str],
        group_properties: dict[str, str],
    ) -> Optional[FlagValue]:
        if self.feature_flags is None and self.personal_api_key:
            self.load_feature_flags()
        response = None

        if self.feature_flags:
            assert self.feature_flags_by_key is not None, (
                "feature_flags_by_key should be initialized when feature_flags is set"
            )
            # Local evaluation
            flag = self.feature_flags_by_key.get(key)
            if flag:
                try:
                    response = self._compute_flag_locally(
                        flag,
                        distinct_id,
                        groups=groups,
                        person_properties=person_properties,
                        group_properties=group_properties,
                    )
                    self.log.debug(
                        f"Successfully computed flag locally: {key} -> {response}"
                    )
                except InconclusiveMatchError as e:
                    self.log.debug(f"Failed to compute flag {key} locally: {e}")
                except Exception as e:
                    self.log.exception(
                        f"[FEATURE FLAGS] Error while computing variant locally: {e}"
                    )
        return response

    def get_feature_flag_payload(
        self,
        key,
        distinct_id,
        *,
        match_value: Optional[FlagValue] = None,
        groups=None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ):
        """
        Get the payload for a feature flag.

        Args:
            key: The feature flag key.
            distinct_id: The distinct ID of the user.
            match_value: The specific flag value to get payload for.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            send_feature_flag_events: Whether to send feature flag events.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            is_my_flag_enabled = posthog.feature_enabled('flag-key', 'distinct_id_of_your_user')

            if is_my_flag_enabled:
                # Do something differently for this user
                # Optional: fetch the payload
                matched_flag_payload = posthog.get_feature_flag_payload('flag-key', 'distinct_id_of_your_user')
            ```

        Category:
            Feature Flags
        """
        feature_flag_result = self._get_feature_flag_result(
            key,
            distinct_id,
            override_match_value=match_value,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
        )
        return feature_flag_result.payload if feature_flag_result else None

    def _get_feature_flag_details_from_decide(
        self,
        key: str,
        distinct_id: ID_TYPES,
        groups: dict[str, str],
        person_properties: dict[str, str],
        group_properties: dict[str, str],
        disable_geoip: Optional[bool],
    ) -> tuple[Optional[FeatureFlag], Optional[str]]:
        """
        Calls /decide and returns the flag details and request id
        """
        resp_data = self.get_flags_decision(
            distinct_id, groups, person_properties, group_properties, disable_geoip
        )
        request_id = resp_data.get("requestId")
        flags = resp_data.get("flags")
        flag_details = flags.get(key) if flags else None
        return flag_details, request_id

    def _capture_feature_flag_called(
        self,
        distinct_id: ID_TYPES,
        key: str,
        response: Optional[FlagValue],
        payload: Optional[str],
        flag_was_locally_evaluated: bool,
        groups: Dict[str, str],
        disable_geoip: Optional[bool],
        request_id: Optional[str],
        flag_details: Optional[FeatureFlag],
    ):
        feature_flag_reported_key = (
            f"{key}_{'::null::' if response is None else str(response)}"
        )

        if (
            feature_flag_reported_key
            not in self.distinct_ids_feature_flags_reported[distinct_id]
        ):
            properties: dict[str, Any] = {
                "$feature_flag": key,
                "$feature_flag_response": response,
                "locally_evaluated": flag_was_locally_evaluated,
                f"$feature/{key}": response,
            }

            if payload is not None:
                # if payload is not a string, json serialize it to a string
                properties["$feature_flag_payload"] = payload

            if request_id:
                properties["$feature_flag_request_id"] = request_id
            if isinstance(flag_details, FeatureFlag):
                if flag_details.reason and flag_details.reason.description:
                    properties["$feature_flag_reason"] = flag_details.reason.description
                if isinstance(flag_details.metadata, FlagMetadata):
                    if flag_details.metadata.version:
                        properties["$feature_flag_version"] = (
                            flag_details.metadata.version
                        )
                    if flag_details.metadata.id:
                        properties["$feature_flag_id"] = flag_details.metadata.id

            self.capture(
                "$feature_flag_called",
                distinct_id=distinct_id,
                properties=properties,
                groups=groups,
                disable_geoip=disable_geoip,
            )
            self.distinct_ids_feature_flags_reported[distinct_id].add(
                feature_flag_reported_key
            )

    def get_remote_config_payload(self, key: str):
        if self.disabled:
            return None

        if self.personal_api_key is None:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a personal_api_key to fetch decrypted feature flag payloads."
            )
            return None

        try:
            return remote_config(
                self.personal_api_key,
                self.api_key,
                self.host,
                key,
                timeout=self.feature_flags_request_timeout_seconds,
            )
        except Exception as e:
            self.log.exception(
                f"[FEATURE FLAGS] Unable to get decrypted feature flag payload: {e}"
            )

    def _compute_payload_locally(
        self, key: str, match_value: FlagValue
    ) -> Optional[str]:
        payload = None

        if self.feature_flags_by_key is None:
            return payload

        flag_definition = self.feature_flags_by_key.get(key)
        if flag_definition:
            flag_filters = flag_definition.get("filters") or {}
            flag_payloads = flag_filters.get("payloads") or {}
            # For boolean flags, convert True to "true"
            # For multivariate flags, use the variant string as-is
            lookup_value = (
                "true"
                if isinstance(match_value, bool) and match_value
                else str(match_value)
            )
            payload = flag_payloads.get(lookup_value, None)
        return payload

    def get_all_flags(
        self,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        disable_geoip=None,
    ) -> Optional[dict[str, Union[bool, str]]]:
        """
        Get all feature flags for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            posthog.get_all_flags('distinct_id_of_your_user')
            ```

        Category:
            Feature Flags
        """
        response = self.get_all_flags_and_payloads(
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            disable_geoip=disable_geoip,
        )

        return response["featureFlags"]

    def get_all_flags_and_payloads(
        self,
        distinct_id,
        *,
        groups=None,
        person_properties=None,
        group_properties=None,
        only_evaluate_locally=False,
        disable_geoip=None,
    ) -> FlagsAndPayloads:
        """
        Get all feature flags and their payloads for a user.

        Args:
            distinct_id: The distinct ID of the user.
            groups: A dictionary of group information.
            person_properties: A dictionary of person properties.
            group_properties: A dictionary of group properties.
            only_evaluate_locally: Whether to only evaluate locally.
            disable_geoip: Whether to disable GeoIP for this request.

        Examples:
            ```python
            posthog.get_all_flags_and_payloads('distinct_id_of_your_user')
            ```

        Category:
            Feature Flags
        """
        if self.disabled:
            return {"featureFlags": None, "featureFlagPayloads": None}

        person_properties, group_properties = (
            self._add_local_person_and_group_properties(
                distinct_id, groups, person_properties, group_properties
            )
        )

        response, fallback_to_decide = self._get_all_flags_and_payloads_locally(
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
        )

        if fallback_to_decide and not only_evaluate_locally:
            try:
                decide_response = self.get_flags_decision(
                    distinct_id,
                    groups=groups,
                    person_properties=person_properties,
                    group_properties=group_properties,
                    disable_geoip=disable_geoip,
                )
                return to_flags_and_payloads(decide_response)
            except Exception as e:
                self.log.exception(
                    f"[FEATURE FLAGS] Unable to get feature flags and payloads: {e}"
                )

        return response

    def _get_all_flags_and_payloads_locally(
        self,
        distinct_id: ID_TYPES,
        *,
        groups: Dict[str, Union[str, int]],
        person_properties=None,
        group_properties=None,
        warn_on_unknown_groups=False,
    ) -> tuple[FlagsAndPayloads, bool]:
        person_properties = person_properties or {}
        group_properties = group_properties or {}

        if self.feature_flags is None and self.personal_api_key:
            self.load_feature_flags()

        flags: dict[str, FlagValue] = {}
        payloads: dict[str, str] = {}
        fallback_to_decide = False
        # If loading in previous line failed
        if self.feature_flags:
            for flag in self.feature_flags:
                try:
                    flags[flag["key"]] = self._compute_flag_locally(
                        flag,
                        distinct_id,
                        groups=groups,
                        person_properties=person_properties,
                        group_properties=group_properties,
                        warn_on_unknown_groups=warn_on_unknown_groups,
                    )
                    matched_payload = self._compute_payload_locally(
                        flag["key"], flags[flag["key"]]
                    )
                    if matched_payload is not None:
                        payloads[flag["key"]] = matched_payload
                except InconclusiveMatchError:
                    # No need to log this, since it's just telling us to fall back to `/decide`
                    fallback_to_decide = True
                except Exception as e:
                    self.log.exception(
                        f"[FEATURE FLAGS] Error while computing variant and payload: {e}"
                    )
                    fallback_to_decide = True
        else:
            fallback_to_decide = True

        return {
            "featureFlags": flags,
            "featureFlagPayloads": payloads,
        }, fallback_to_decide

    def _initialize_flag_cache(self, cache_url):
        """Initialize feature flag cache for graceful degradation during service outages.

        When enabled, the cache stores flag evaluation results and serves them as fallback
        when the PostHog API is unavailable. This ensures your application continues to
        receive flag values even during outages.

        Args:
            cache_url: Cache configuration URL. Examples:
                - None: Disable caching
                - "memory://local/?ttl=300&size=10000": Memory cache with TTL and size
                - "redis://localhost:6379/0/?ttl=300": Redis cache with TTL
                - "redis://username:password@host:port/?ttl=300": Redis with auth

        Example usage:
            # Memory cache
            client = Client(
                "your-api-key",
                flag_fallback_cache_url="memory://local/?ttl=300&size=10000"
            )

            # Redis cache
            client = Client(
                "your-api-key",
                flag_fallback_cache_url="redis://localhost:6379/0/?ttl=300"
            )

            # Normal evaluation - cache is populated
            flag_value = client.get_feature_flag("my-flag", "user123")

            # During API outage - returns cached value instead of None
            flag_value = client.get_feature_flag("my-flag", "user123")  # Uses cache
        """
        if not cache_url:
            return None

        try:
            from urllib.parse import urlparse, parse_qs
        except ImportError:
            from urlparse import urlparse, parse_qs

        try:
            parsed = urlparse(cache_url)
            scheme = parsed.scheme.lower()
            query_params = parse_qs(parsed.query)
            ttl = int(query_params.get("ttl", [300])[0])

            if scheme == "memory":
                size = int(query_params.get("size", [10000])[0])
                return FlagCache(size, ttl)

            elif scheme == "redis":
                try:
                    # Not worth importing redis if we're not using it
                    import redis

                    redis_url = f"{parsed.scheme}://"
                    if parsed.username or parsed.password:
                        redis_url += f"{parsed.username or ''}:{parsed.password or ''}@"
                    redis_url += (
                        f"{parsed.hostname or 'localhost'}:{parsed.port or 6379}"
                    )
                    if parsed.path:
                        redis_url += parsed.path

                    client = redis.from_url(redis_url)

                    # Test connection before using it
                    client.ping()

                    return RedisFlagCache(client, default_ttl=ttl)

                except ImportError:
                    self.log.warning(
                        "[FEATURE FLAGS] Redis not available, flag caching disabled"
                    )
                    return None
                except Exception as e:
                    self.log.warning(
                        f"[FEATURE FLAGS] Redis connection failed: {e}, flag caching disabled"
                    )
                    return None
            else:
                raise ValueError(
                    f"Unknown cache URL scheme: {scheme}. Supported schemes: memory, redis"
                )

        except Exception as e:
            self.log.warning(
                f"[FEATURE FLAGS] Failed to parse cache URL '{cache_url}': {e}"
            )
            return None

    def feature_flag_definitions(self):
        return self.feature_flags

    def _add_local_person_and_group_properties(
        self, distinct_id, groups, person_properties, group_properties
    ):
        all_person_properties = {
            "distinct_id": distinct_id,
            **(person_properties or {}),
        }

        all_group_properties = {}
        if groups:
            for group_name in groups:
                all_group_properties[group_name] = {
                    "$group_key": groups[group_name],
                    **(group_properties.get(group_name) or {}),
                }

        return all_person_properties, all_group_properties


def stringify_id(val):
    if val is None:
        return None
    if isinstance(val, string_types):
        return val
    return str(val)
