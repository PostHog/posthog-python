import datetime  # noqa: F401
from typing import Callable, Dict, Optional  # noqa: F401

from posthog.client import Client
from posthog.version import VERSION
import logging

__version__ = VERSION

default_client = None


class Posthog(object):
    log = logging.getLogger("posthog")

    """Settings."""
    disabled = False  # type: bool
    _client: Client = None  # type: Client

    def __init__(
        self,
        api_key,
        host="https://app.posthog.com",
        on_error=None,
        debug=False,
        send=True,
        sync_mode=False,
        personal_api_key=None,
        project_api_key=None,
        poll_interval=30,
        disabled=False,
    ):
        self.disabled = disabled
        self._client = Client(
            api_key=api_key,
            host=host,
            on_error=on_error,
            debug=debug,
            send=send,
            sync_mode=sync_mode,
            personal_api_key=personal_api_key,
            project_api_key=project_api_key,
            poll_interval=poll_interval,
        )

    def capture(
        self,
        distinct_id,  # type: str
        event,  # type: str
        properties=None,  # type: Optional[Dict]
        context=None,  # type: Optional[Dict]
        timestamp=None,  # type: Optional[datetime.datetime]
        uuid=None,  # type: Optional[str]
        groups=None,  # type: Optional[Dict]
        send_feature_flags=False,
    ):
        # type: (...) -> None
        """
        Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up.

        A `capture` call requires
        - `distinct id` which uniquely identifies your user
        - `event name` to specify the event
        - We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on.

        Optionally you can submit
        - `properties`, which can be a dict with any information you'd like to add
        - `groups`, which is a dict of group type -> group key mappings

        For example:
        ```python
        posthog.capture('distinct id', 'opened app')
        posthog.capture('distinct id', 'movie played', {'movie_id': '123', 'category': 'romcom'})

        posthog.capture('distinct id', 'purchase', groups={'company': 'id:5'})
        ```
        """
        self._proxy(
            "capture",
            distinct_id=distinct_id,
            event=event,
            properties=properties,
            context=context,
            timestamp=timestamp,
            uuid=uuid,
            groups=groups,
            send_feature_flags=send_feature_flags,
        )

    def identify(
        self,
        distinct_id,  # type: str
        properties=None,  # type: Optional[Dict]
        context=None,  # type: Optional[Dict]
        timestamp=None,  # type: Optional[datetime.datetime]
        uuid=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        """
        Identify lets you add metadata on your users so you can more easily identify who they are in PostHog, and even do things like segment users by these properties.

        An `identify` call requires
        - `distinct id` which uniquely identifies your user
        - `properties` with a dict with any key: value pairs

        For example:
        ```python
        posthog.identify('distinct id', {
            'email': 'dwayne@gmail.com',
            'name': 'Dwayne Johnson'
        })
        ```
        """
        self._proxy(
            "identify",
            distinct_id=distinct_id,
            properties=properties,
            context=context,
            timestamp=timestamp,
            uuid=uuid,
        )

    def set(
        self,
        distinct_id,  # type: str
        properties=None,  # type: Optional[Dict]
        context=None,  # type: Optional[Dict]
        timestamp=None,  # type: Optional[datetime.datetime]
        uuid=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        """
        Set properties on a user record.
        This will overwrite previous people property values, just like `identify`.

        A `set` call requires
        - `distinct id` which uniquely identifies your user
        - `properties` with a dict with any key: value pairs

        For example:
        ```python
        posthog.set('distinct id', {
            'current_browser': 'Chrome',
        })
        ```
        """
        self._proxy(
            "set",
            distinct_id=distinct_id,
            properties=properties,
            context=context,
            timestamp=timestamp,
            uuid=uuid,
        )

    def set_once(
        self,
        distinct_id,  # type: str
        properties=None,  # type: Optional[Dict]
        context=None,  # type: Optional[Dict]
        timestamp=None,  # type: Optional[datetime.datetime]
        uuid=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        """
        Set properties on a user record, only if they do not yet exist.
        This will not overwrite previous people property values, unlike `identify`.

        A `set_once` call requires
        - `distinct id` which uniquely identifies your user
        - `properties` with a dict with any key: value pairs

        For example:
        ```python
        posthog.set_once('distinct id', {
            'referred_by': 'friend',
        })
        ```
        """
        self._proxy(
            "set_once",
            distinct_id=distinct_id,
            properties=properties,
            context=context,
            timestamp=timestamp,
            uuid=uuid,
        )

    def group_identify(
        self,
        group_type,  # type: str
        group_key,  # type: str
        properties=None,  # type: Optional[Dict]
        context=None,  # type: Optional[Dict]
        timestamp=None,  # type: Optional[datetime.datetime]
        uuid=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        """
        Set properties on a group

        A `group_identify` call requires
        - `group_type` type of your group
        - `group_key` unique identifier of the group
        - `properties` with a dict with any key: value pairs

        For example:
        ```python
        posthog.group_identify('company', 5, {
            'employees': 11,
        })
        ```
        """
        self._proxy(
            "group_identify",
            group_type=group_type,
            group_key=group_key,
            properties=properties,
            context=context,
            timestamp=timestamp,
            uuid=uuid,
        )

    def alias(
        self,
        previous_id,  # type: str
        distinct_id,  # type: str
        context=None,  # type: Optional[Dict]
        timestamp=None,  # type: Optional[datetime.datetime]
        uuid=None,  # type: Optional[str]
    ):
        # type: (...) -> None
        """
        To marry up whatever a user does before they sign up or log in with what they do after you need to make an alias call. This will allow you to answer questions like "Which marketing channels leads to users churning after a month?" or "What do users do on our website before signing up?"

        In a purely back-end implementation, this means whenever an anonymous user does something, you'll want to send a session ID ([Django](https://stackoverflow.com/questions/526179/in-django-how-can-i-find-out-the-request-session-sessionid-and-use-it-as-a-vari), [Flask](https://stackoverflow.com/questions/15156132/flask-login-how-to-get-session-id)) with the capture call. Then, when that users signs up, you want to do an alias call with the session ID and the newly created user ID.

        The same concept applies for when a user logs in.

        An `alias` call requires
        - `previous distinct id` the unique ID of the user before
        - `distinct id` the current unique id

        For example:
        ```python
        posthog.alias('anonymous session id', 'distinct id')
        ```
        """
        self._proxy(
            "alias",
            previous_id=previous_id,
            distinct_id=distinct_id,
            context=context,
            timestamp=timestamp,
            uuid=uuid,
        )

    def feature_enabled(
        self,
        key,  # type: str
        distinct_id,  # type: str
        groups={},  # type: dict
        person_properties={},  # type: dict
        group_properties={},  # type: dict
        only_evaluate_locally=False,  # type: bool
        send_feature_flag_events=True,  # type: bool
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
        return self._proxy(
            "feature_enabled",
            key=key,
            distinct_id=distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
        )

    def get_feature_flag(
        self,
        key,  # type: str
        distinct_id,  # type: str
        groups={},  # type: dict
        person_properties={},  # type: dict
        group_properties={},  # type: dict
        only_evaluate_locally=False,  # type: bool
        send_feature_flag_events=True,  # type: bool
    ):
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
        return self._proxy(
            "get_feature_flag",
            key=key,
            distinct_id=distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
        )

    def get_all_flags(
        self,
        distinct_id,  # type: str
        groups={},  # type: dict
        person_properties={},  # type: dict
        group_properties={},  # type: dict
        only_evaluate_locally=False,  # type: bool
    ):
        """
        Get all flags for a given user.
        Example:
        ```python
        flags = posthog.get_all_flags('distinct_id')
        ```

        flags are key-value pairs where the key is the flag key and the value is the flag variant, or True, or False.
        """
        return self._proxy(
            "get_all_flags",
            distinct_id=distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
        )

    def get_feature_flag_payload(
        self,
        key,
        distinct_id,
        match_value=None,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
        send_feature_flag_events=True,
    ):
        return self._proxy(
            "get_feature_flag_payload",
            key=key,
            distinct_id=distinct_id,
            match_value=match_value,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
        )

    def get_all_flags_and_payloads(
        self,
        distinct_id,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
    ):
        return self._proxy(
            "get_all_flags_and_payloads",
            distinct_id=distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
        )

    def page(self, *args, **kwargs):
        """Send a page call."""
        self._proxy("page", *args, **kwargs)

    def screen(self, *args, **kwargs):
        """Send a screen call."""
        self._proxy("screen", *args, **kwargs)

    def flush(self):
        """Tell the client to flush."""
        self._proxy("flush")

    def join(self):
        """Block program until the client clears the queue"""
        self._proxy("join")

    def shutdown(self):
        """Flush all messages and cleanly shutdown the client"""
        self._proxy("flush")
        self._proxy("join")

    def _proxy(self, method, *args, **kwargs):
        """Create an analytics client if one doesn't exist and send to it."""
        if self.disabled:
            return None

        fn = getattr(self._client, method)
        return fn(*args, **kwargs)
