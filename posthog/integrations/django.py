import json
import math
import re
import time
from typing import TYPE_CHECKING, Any, Optional, cast
from urllib.parse import unquote

from .. import contexts
from ..client import Client
from ..exception_utils import (
    _ExceptionCaptureMetadata,
    _capture_exception_with_metadata,
)

try:
    from asgiref.sync import iscoroutinefunction, markcoroutinefunction
except ImportError:
    # Fallback for older Django versions without asgiref
    import asyncio

    iscoroutinefunction = asyncio.iscoroutinefunction

    # No-op fallback for markcoroutinefunction
    # Older Django versions without asgiref typically don't support async middleware anyway
    def markcoroutinefunction(func):
        return func


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse  # noqa: F401
    from typing import Callable, Dict, Any, Union, Awaitable  # noqa: F401


_MAX_TRACING_HEADER_LENGTH = 1000
_TRACING_HEADER_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_tracing_header_value(value) -> Optional[str]:
    """Return a safe tracing header value, or None if the value is invalid.

    Tracing headers come from user-controlled HTTP requests and are copied into event properties.
    Match the PostHog app's header sanitization: accept strings only, remove C0/C1 control
    characters, trim surrounding whitespace, cap length, and drop empty results.
    """
    if not isinstance(value, str) or not value:
        return None

    return (
        _TRACING_HEADER_CONTROL_CHARS_RE.sub("", value).strip()[
            :_MAX_TRACING_HEADER_LENGTH
        ]
        or None
    )


def _get_sanitized_tracing_header(request, header_name) -> Optional[str]:
    try:
        return _sanitize_tracing_header_value(request.headers.get(header_name))
    except Exception:
        return None


_POSTHOG_COOKIE_NAME_RE = re.compile(r"^ph_.+_posthog$")
_POSTHOG_CONSENT_COOKIE_PREFIX = "__ph_opt_in_out_"
_POSTHOG_CONSENT_NO_VALUES = ("false", "0", "no")
# posthog-js defaults. The browser starts a new session after this much inactivity or session length.
_COOKIE_SESSION_IDLE_TIMEOUT_MS = 30 * 60 * 1000
_COOKIE_SESSION_MAX_LENGTH_MS = 24 * 60 * 60 * 1000


def _default_api_key() -> Optional[str]:
    # Read at call time, because the app sets the module-level keys after it imports this module.
    from .. import api_key, project_api_key

    return (project_api_key or "").strip() or (api_key or "").strip() or None


def _posthog_cookie_name(api_key: str) -> str:
    # posthog-js replaces these characters in the token when it names the cookie.
    sanitized = api_key.replace("+", "PL").replace("/", "SL").replace("=", "EQ")
    return f"ph_{sanitized}_posthog"


def _is_recent(timestamp_ms, now_ms: float, max_age_ms: int) -> bool:
    # abs, like posthog-js, so a browser clock that runs ahead cannot keep a session alive.
    return (
        isinstance(timestamp_ms, (int, float))
        and not isinstance(timestamp_ms, bool)
        and math.isfinite(timestamp_ms)
        and abs(now_ms - timestamp_ms) <= max_age_ms
    )


def _read_posthog_cookie(
    request,
    api_key,
    session_idle_timeout_ms: int = _COOKIE_SESSION_IDLE_TIMEOUT_MS,
    opt_out_by_default: bool = False,
) -> "tuple[Optional[str], Optional[str]]":
    """Return the identified distinct ID and the live session ID from the posthog-js cookie, if present.

    With its default persistence, posthog-js writes `ph_<project token>_posthog` as a first-party
    cookie, and the browser sends it on every same-site request. This links backend events to the
    browser session without `tracing_headers`. A session that is past the posthog-js idle timeout or
    length cap is not returned, because the browser starts a new session on its next activity.
    An anonymous distinct ID is not returned, so backend events for anonymous visitors stay
    personless. Nothing is returned when the visitor's posthog-js consent cookie opts out.
    """
    try:
        cookies = getattr(request, "COOKIES", None) or {}
        api_key = (api_key or "").strip()
        if api_key:
            raw = cookies.get(_posthog_cookie_name(api_key))
            consent_values = [
                value
                for value in [cookies.get(_POSTHOG_CONSENT_COOKIE_PREFIX + api_key)]
                if value is not None
            ]
        else:
            # The project is unknown: use the cookie only when it is the only PostHog cookie.
            matches = [
                value
                for name, value in cookies.items()
                if _POSTHOG_COOKIE_NAME_RE.match(name)
            ]
            raw = matches[0] if len(matches) == 1 else None
            consent_values = [
                value
                for name, value in cookies.items()
                if name.startswith(_POSTHOG_CONSENT_COOKIE_PREFIX)
            ]
        opted_out = (
            any(
                isinstance(value, str)
                and value.strip().lower() in _POSTHOG_CONSENT_NO_VALUES
                for value in consent_values
            )
            # Like posthog-js, a visitor with no consent cookie counts as opted out under this default.
            or (opt_out_by_default and not consent_values)
        )
        if not raw or opted_out:
            return None, None

        data = json.loads(unquote(raw))
        if not isinstance(data, dict):
            return None, None

        distinct_id = (
            _sanitize_tracing_header_value(data.get("distinct_id"))
            if data.get("$user_state") == "identified"
            else None
        )
        session_id = None
        session = data.get("$sesid")
        if isinstance(session, list) and len(session) in (2, 3):
            # Older posthog-js versions stored [last activity, session id] and start the session then.
            last_activity_ms, candidate = session[0], session[1]
            session_start_ms = session[2] if len(session) == 3 else last_activity_ms
            now_ms = time.time() * 1000
            if _is_recent(
                last_activity_ms, now_ms, session_idle_timeout_ms
            ) and _is_recent(session_start_ms, now_ms, _COOKIE_SESSION_MAX_LENGTH_MS):
                session_id = _sanitize_tracing_header_value(candidate)
        return distinct_id, session_id
    except Exception:
        return None, None


class PosthogContextMiddleware:
    """Middleware to automatically track Django requests.

    This middleware wraps all calls with a posthog context. It attempts to extract the following from the request:
    - Session ID, (extracted from `X-POSTHOG-SESSION-ID`, optionally falling back to the posthog-js cookie)
    - Distinct ID, (extracted from `X-POSTHOG-DISTINCT-ID`, falling back to the authenticated request user ID,
      then optionally to the posthog-js cookie)
    - Authenticated user email as `email`
    - Request URL as `$current_url`
    - Request method as `$request_method`
    - Request path as `$request_path`
    - Forwarded IP address as `$ip`
    - User agent as `$user_agent`

    The context will also auto-capture exceptions and send them to PostHog, unless you disable it by setting
    `POSTHOG_MW_CAPTURE_EXCEPTIONS` to `False` in your Django settings. The exceptions are captured using the
    global client, unless the setting `POSTHOG_MW_CLIENT` is set to a custom client instance

    Set `POSTHOG_MW_READ_POSTHOG_COOKIE` to `True` to read the session ID, and the distinct ID of an identified
    user, from the posthog-js cookie (`ph_<project token>_posthog`) when a request has neither tracing header.
    The browser sends that cookie on same-site requests without any frontend configuration. Turn it on only when
    posthog-js stores opt-out consent in a cookie, or when you do not use opt-out: the server cannot read an
    opt-out that posthog-js keeps in localStorage. It needs cookie-backed posthog-js persistence, the default
    cookie name (no `persistence_name`), and the default `__ph_opt_in_out_<token>` consent cookie name. Set
    `POSTHOG_MW_COOKIE_SESSION_IDLE_TIMEOUT_SECONDS` when posthog-js uses a custom `session_idle_timeout_seconds`,
    and set `POSTHOG_MW_COOKIE_OPT_OUT_BY_DEFAULT` to `True` when posthog-js uses `opt_out_capturing_by_default`.

    The middleware behaviour is customisable through 3 additional functions:
    - `POSTHOG_MW_EXTRA_TAGS`, which is a Callable[[HttpRequest], Dict[str, Any]] expected to return a dictionary of additional tags to be added to the context.
    - `POSTHOG_MW_REQUEST_FILTER`, which is a Callable[[HttpRequest], bool] expected to return `False` if the request should not be tracked.
    - `POSTHOG_MW_TAG_MAP`, which is a Callable[[Dict[str, Any]], Dict[str, Any]], which you can use to modify the tags before they're added to the context.

    You can use the `POSTHOG_MW_TAG_MAP` function to remove any default tags you don't want to capture, or override them with your own values.

    Context tags are automatically included as properties on all events captured within a context, including exceptions.
    See the context documentation for more information. The extracted distinct ID and session ID,
    if found, are used to associate all events captured in the middleware context with the same distinct ID
    and session as currently active on the frontend. See the documentation for `set_context_session`
    and `identify_context` for more details.

    This middleware is hybrid-capable: it supports both WSGI (sync) and ASGI (async) Django applications. The middleware
    detects at initialization whether the next middleware in the chain is async or sync, and adapts its behavior accordingly.
    This ensures compatibility with both pure sync and pure async middleware chains, as well as mixed chains in ASGI mode.
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        # type: (Union[Callable[[HttpRequest], HttpResponse], Callable[[HttpRequest], Awaitable[HttpResponse]]]) -> None
        """
        Initialize the middleware with Django's next handler.

        Args:
            get_response: The next middleware or view handler in Django's
                middleware chain. May be synchronous or asynchronous.
        """
        self.get_response = get_response
        self._is_coroutine = iscoroutinefunction(get_response)

        # Mark this instance as a coroutine function if get_response is async
        # This is required for Django to correctly detect async middleware
        if self._is_coroutine:
            markcoroutinefunction(self)

        from django.conf import settings

        if hasattr(settings, "POSTHOG_MW_EXTRA_TAGS") and callable(
            settings.POSTHOG_MW_EXTRA_TAGS
        ):
            self.extra_tags = cast(
                "Optional[Callable[[HttpRequest], Dict[str, Any]]]",
                settings.POSTHOG_MW_EXTRA_TAGS,
            )
        else:
            self.extra_tags = None

        if hasattr(settings, "POSTHOG_MW_REQUEST_FILTER") and callable(
            settings.POSTHOG_MW_REQUEST_FILTER
        ):
            self.request_filter = cast(
                "Optional[Callable[[HttpRequest], bool]]",
                settings.POSTHOG_MW_REQUEST_FILTER,
            )
        else:
            self.request_filter = None

        if hasattr(settings, "POSTHOG_MW_TAG_MAP") and callable(
            settings.POSTHOG_MW_TAG_MAP
        ):
            self.tag_map = cast(
                "Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]",
                settings.POSTHOG_MW_TAG_MAP,
            )
        else:
            self.tag_map = None

        if hasattr(settings, "POSTHOG_MW_CAPTURE_EXCEPTIONS") and isinstance(
            settings.POSTHOG_MW_CAPTURE_EXCEPTIONS, bool
        ):
            self.capture_exceptions = settings.POSTHOG_MW_CAPTURE_EXCEPTIONS
        else:
            self.capture_exceptions = True

        if hasattr(settings, "POSTHOG_MW_CLIENT") and isinstance(
            settings.POSTHOG_MW_CLIENT, Client
        ):
            self.client = cast("Optional[Client]", settings.POSTHOG_MW_CLIENT)
        else:
            self.client = None

        if hasattr(settings, "POSTHOG_MW_READ_POSTHOG_COOKIE") and isinstance(
            settings.POSTHOG_MW_READ_POSTHOG_COOKIE, bool
        ):
            self.read_posthog_cookie = settings.POSTHOG_MW_READ_POSTHOG_COOKIE
        else:
            self.read_posthog_cookie = False

        self.cookie_opt_out_by_default = (
            getattr(settings, "POSTHOG_MW_COOKIE_OPT_OUT_BY_DEFAULT", False) is True
        )

        idle_timeout_seconds = getattr(
            settings, "POSTHOG_MW_COOKIE_SESSION_IDLE_TIMEOUT_SECONDS", None
        )
        # The same bounds posthog-js applies to session_idle_timeout_seconds.
        self.cookie_session_idle_timeout_ms = (
            int(min(max(idle_timeout_seconds, 60), 10 * 60 * 60) * 1000)
            if isinstance(idle_timeout_seconds, (int, float))
            and not isinstance(idle_timeout_seconds, bool)
            and math.isfinite(idle_timeout_seconds)
            else _COOKIE_SESSION_IDLE_TIMEOUT_MS
        )

    def extract_tags(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        """Extract tags from request in sync context."""
        user_id, user_email = self.extract_request_user(request)
        return self._build_tags(request, user_id, user_email)

    def _build_tags(self, request, user_id, user_email):
        # type: (HttpRequest, Optional[str], Optional[str]) -> Dict[str, Any]
        """
        Build tags dict from request and user info.

        Centralized tag extraction logic used by both sync and async paths.
        """
        tags = {}

        header_session_id = _get_sanitized_tracing_header(
            request, "X-POSTHOG-SESSION-ID"
        )
        header_distinct_id = _get_sanitized_tracing_header(
            request, "X-POSTHOG-DISTINCT-ID"
        )
        # The cookie is read only without tracing headers, so one request never mixes two identities.
        cookie_distinct_id, cookie_session_id = (
            _read_posthog_cookie(
                request,
                self.client.api_key if self.client else _default_api_key(),
                self.cookie_session_idle_timeout_ms,
                self.cookie_opt_out_by_default,
            )
            if self.read_posthog_cookie
            and header_session_id is None
            and header_distinct_id is None
            else (None, None)
        )

        # Extract session ID from X-POSTHOG-SESSION-ID header or the posthog-js cookie
        session_id = header_session_id or cookie_session_id
        if session_id:
            contexts.set_context_session(session_id)

        # Extract distinct ID from X-POSTHOG-DISTINCT-ID header, request user id, or the posthog-js cookie
        distinct_id = header_distinct_id or user_id or cookie_distinct_id
        if distinct_id:
            contexts.identify_context(distinct_id)

        # Extract user email
        if user_email:
            tags["email"] = user_email

        # Extract current URL
        absolute_url = request.build_absolute_uri()
        if absolute_url:
            tags["$current_url"] = absolute_url

        # Extract request method
        if request.method:
            tags["$request_method"] = request.method

        # Extract request path
        if request.path:
            tags["$request_path"] = request.path

        # Extract IP address
        ip_address = request.headers.get("X-Forwarded-For")
        if ip_address:
            tags["$ip"] = ip_address

        # Extract user agent, mirrored into $raw_user_agent — the standardized
        # property PostHog's server-side classification (e.g. bot detection) reads
        user_agent = request.headers.get("User-Agent")
        if user_agent:
            tags["$user_agent"] = user_agent
            tags["$raw_user_agent"] = user_agent

        # Apply extra tags if configured
        if self.extra_tags:
            extra = self.extra_tags(request)
            if extra:
                tags.update(extra)

        # Apply tag mapping if configured
        if self.tag_map:
            tags = self.tag_map(tags)

        return tags

    def extract_request_user(self, request):
        # type: (HttpRequest) -> tuple[Optional[str], Optional[str]]
        """Extract user ID and email from request in sync context."""
        user = getattr(request, "user", None)
        return self._resolve_user_details(user)

    async def aextract_tags(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        """
        Async version of extract_tags for use in async request handling.

        Uses await request.auser() instead of request.user to avoid
        SynchronousOnlyOperation in async context.

        Follows Django's naming convention for async methods (auser, asave, etc.).
        """
        user_id, user_email = await self.aextract_request_user(request)
        return self._build_tags(request, user_id, user_email)

    async def aextract_request_user(self, request):
        # type: (HttpRequest) -> tuple[Optional[str], Optional[str]]
        """
        Async version of extract_request_user for use in async request handling.

        Uses await request.auser() instead of request.user to avoid
        SynchronousOnlyOperation in async context.

        Follows Django's naming convention for async methods (auser, asave, etc.).
        """
        auser = getattr(request, "auser", None)
        if callable(auser):
            try:
                user = await auser()
                return self._resolve_user_details(user)
            except Exception:
                # If auser() fails, return empty - don't break the request
                # Real errors (permissions, broken auth) will be logged by Django
                return None, None

        # Fallback for test requests without auser
        return None, None

    def _resolve_user_details(self, user):
        # type: (Any) -> tuple[Optional[str], Optional[str]]
        """
        Extract user ID and email from a user object.

        Handles both authenticated and unauthenticated users, as well as
        legacy Django where is_authenticated was a method.
        """
        user_id = None
        email = None

        if user is None:
            return user_id, email

        # Handle is_authenticated (property in modern Django, method in legacy)
        is_authenticated = getattr(user, "is_authenticated", False)
        if callable(is_authenticated):
            is_authenticated = is_authenticated()

        if not is_authenticated:
            return user_id, email

        # Extract user primary key
        user_pk = getattr(user, "pk", None)
        if user_pk is not None:
            user_id = str(user_pk)

        # Extract user email
        user_email = getattr(user, "email", None)
        if user_email:
            email = str(user_email)

        return user_id, email

    def __call__(self, request):
        # type: (HttpRequest) -> Union[HttpResponse, Awaitable[HttpResponse]]
        """
        Unified entry point for both sync and async request handling.

        When sync_capable and async_capable are both True, Django passes requests
        without conversion. This method detects the mode and routes accordingly.
        """
        if self._is_coroutine:
            return self.__acall__(request)
        else:
            # Synchronous path
            if self.request_filter and not self.request_filter(request):
                return self.get_response(request)

            with contexts.new_context(
                capture_exceptions=self.capture_exceptions, client=self.client
            ):
                for k, v in self.extract_tags(request).items():
                    contexts.tag(k, v)

                return self.get_response(request)

    async def __acall__(self, request):
        # type: (HttpRequest) -> Awaitable[HttpResponse]
        """
        Asynchronous entry point for async request handling.

        This method is called when the middleware chain is async.
        Uses aextract_tags() which calls request.auser() to avoid
        SynchronousOnlyOperation when accessing user in async context.
        """
        if self.request_filter and not self.request_filter(request):
            return await self.get_response(request)

        with contexts.new_context(
            capture_exceptions=self.capture_exceptions, client=self.client
        ):
            for k, v in (await self.aextract_tags(request)).items():
                contexts.tag(k, v)

            return await self.get_response(request)

    def process_exception(self, request, exception):
        # type: (HttpRequest, Exception) -> None
        """
        Process exceptions from views and downstream middleware.

        Django calls this WHILE still inside the context created by __call__,
        so request tags have already been extracted and set. This method just
        needs to capture the exception directly.

        Django converts view exceptions into responses before they propagate through
        the middleware stack, so the context manager in __call__/__acall__ never sees them.

        Note: Django's process_exception is always synchronous, even for async views.
        """
        if self.request_filter and not self.request_filter(request):
            return

        if not self.capture_exceptions:
            return

        # Context and tags already set by __call__ or __acall__
        # Just capture the exception
        capture_metadata: _ExceptionCaptureMetadata = {
            "level": "error",
            "source": "django.middleware",
            "mechanism": {"type": "middleware", "handled": False},
        }
        if self.client:
            _capture_exception_with_metadata(self.client, exception, capture_metadata)
        else:
            from posthog import capture_exception

            cast(Any, capture_exception)(exception, _capture_metadata=capture_metadata)
