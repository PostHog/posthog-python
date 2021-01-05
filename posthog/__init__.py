from typing import Callable, Dict, Optional

from posthog.client import Client
from posthog.version import VERSION

__version__ = VERSION

"""Settings."""
api_key = None  # type: str
host = None  # type: str
on_error = None  # type: Callable
debug = False  # type: bool
send = True  # type: bool
sync_mode = False  # type: bool
disabled = False  # type: bool
personal_api_key = None  # type: str

default_client = None


def capture(
    distinct_id,  # type: str,
    event,  # type: str,
    properties=None,  # type: Optional[Dict]
    context=None,  # type: Optional[Dict]
    timestamp=None,  # type: Optional[datetime.datetime]
    message_id=None,  # type: Optional[str]
):
    # type: (...) -> None
    """
    Capture allows you to capture anything a user does within your system, which you can later use in PostHog to find patterns in usage, work out which features to improve or where people are giving up.

    A `capture` call requires
    - `distinct id` which uniquely identifies your user
    - `event name` to make sure
    - We recommend using [verb] [noun], like `movie played` or `movie updated` to easily identify what your events mean later on.

    Optionally you can submit
    - `properties`, which can be a dict with any information you'd like to add

    For example:
    ```python
    posthog.capture('distinct id', 'movie played', {'movie_id': '123', 'category': 'romcom'})
    ```
    """
    _proxy(
        "capture",
        distinct_id=distinct_id,
        event=event,
        properties=properties,
        context=context,
        timestamp=timestamp,
        message_id=message_id,
    )


def identify(
    distinct_id,  # type: str,
    properties=None,  # type: Optional[Dict]
    context=None,  # type: Optional[Dict]
    timestamp=None,  # type: Optional[datetime.datetime]
    message_id=None,  # type: Optional[str]
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
    _proxy(
        "identify",
        distinct_id=distinct_id,
        properties=properties,
        context=context,
        timestamp=timestamp,
        message_id=message_id,
    )


def group(*args, **kwargs):
    """Send a group call."""
    _proxy("group", *args, **kwargs)


def alias(
    previous_id,  # type: str,
    distinct_id,  # type: str,
    context=None,  # type: Optional[Dict]
    timestamp=None,  # type: Optional[datetime.datetime]
    message_id=None,  # type: Optional[str]
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
    _proxy(
        "alias",
        previous_id=previous_id,
        distinct_id=distinct_id,
        context=context,
        timestamp=timestamp,
        message_id=message_id,
    )


def feature_enabled(
    key,  # type: str,
    distinct_id,  # type: str,
    default=False,  # type: bool
):
    # type: (...) -> bool
    """
    Use feature flags to enable or disable features for users.

    For example:
    ```python
    if posthog.feature_enabled('beta feature', 'distinct id'):
        # do something
    ```

    You can call `posthog.load_feature_flags()` before to make sure you're not doing unexpected requests.
    """
    return _proxy("feature_enabled", key=key, distinct_id=distinct_id, default=default)


def page(*args, **kwargs):
    """Send a page call."""
    _proxy("page", *args, **kwargs)


def screen(*args, **kwargs):
    """Send a screen call."""
    _proxy("screen", *args, **kwargs)


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


def _proxy(method, *args, **kwargs):
    """Create an analytics client if one doesn't exist and send to it."""
    global default_client
    if disabled:
        return None
    if not default_client:
        default_client = Client(
            api_key,
            host=host,
            debug=debug,
            on_error=on_error,
            send=send,
            sync_mode=sync_mode,
            personal_api_key=personal_api_key,
        )

    fn = getattr(default_client, method)
    return fn(*args, **kwargs)
