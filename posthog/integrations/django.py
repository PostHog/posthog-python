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
# posthog-js grants consent only for these exact values. Anything else is pending.
_POSTHOG_CONSENT_YES_VALUES = ("true", "1", "yes")
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
                (name, value)
                for name, value in cookies.items()
                if _POSTHOG_COOKIE_NAME_RE.match(name)
            ]
            cookie_name, raw = matches[0] if len(matches) == 1 else (None, None)
            # Check consent only for the selected cookie's project.
            consent_values = [
                value
                for name, value in cookies.items()
                if cookie_name is not None
                and name.startswith(_POSTHOG_CONSENT_COOKIE_PREFIX)
                and _posthog_cookie_name(name[len(_POSTHOG_CONSENT_COOKIE_PREFIX) :])
                == cookie_name
            ]
        opted_out = (
            any(
                isinstance(value, str)
                and value.strip().lower() in _POSTHOG_CONSENT_NO_VALUES
                for value in consent_values
            )
            # Like posthog-js, a visitor without a granted consent value counts as opted out under this default.
            or (
                opt_out_by_default
                and not any(
                    value in _POSTHOG_CONSENT_YES_VALUES for value in consent_values
                )
            )
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

    Configure the integration with these Django settings:
    - `POSTHOG_MW_CLIENT`: client used for automatic exception capture, or the global client.
      This does not redirect events explicitly captured through other clients.
    - `POSTHOG_MW_CAPTURE_EXCEPTIONS`: `False` enables context enrichment only;
      `True` enables automatic exception capture; `None` inherits the effective client's
      `enable_exception_autocapture`. When omitted, the legacy default is `True`.
    - `POSTHOG_MW_REQUEST_FILTER`: a callback receiving the request. Returning `False`
      disables both enrichment and automatic exception capture for that request.
    - `POSTHOG_MW_EXTRA_PROPERTIES`: a callback returning additional event properties.
    - `POSTHOG_MW_TRUST_TRACING_HEADERS`: accept the client-controlled PostHog identity
      and session headers only when `True` (default: `False`). These values are analytics
      context, never authentication or authorization. Authenticated Django user identity
      is still available when headers are disabled.
    - `POSTHOG_MW_PROPERTIES_MAP`: an optional callback transforming request properties.

    The old `POSTHOG_MW_EXTRA_TAGS` and `POSTHOG_MW_TAG_MAP` settings remain aliases;
    the property-named settings take precedence when both are configured.

    Set `POSTHOG_MW_READ_POSTHOG_COOKIE` to `True` to read the session ID, and the distinct ID of an identified
    user, from the posthog-js cookie (`ph_<project token>_posthog`) when a request has no trusted tracing header.
    The browser sends that cookie on same-site requests without any frontend configuration. Turn it on only when
    posthog-js stores opt-out consent in a cookie, or when you do not use opt-out: the server cannot read an
    opt-out that posthog-js keeps in localStorage. It needs cookie-backed posthog-js persistence, the default
    cookie name (no `persistence_name`), and the default `__ph_opt_in_out_<token>` consent cookie name. Set
    `POSTHOG_MW_COOKIE_SESSION_IDLE_TIMEOUT_SECONDS` when posthog-js uses a custom `session_idle_timeout_seconds`,
    and set `POSTHOG_MW_COOKIE_OPT_OUT_BY_DEFAULT` to `True` when posthog-js uses `opt_out_capturing_by_default`.

    Request properties are included on all events captured within the active context,
    including when automatic exception capture is disabled.
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

        extra_properties = (
            settings.POSTHOG_MW_EXTRA_PROPERTIES
            if hasattr(settings, "POSTHOG_MW_EXTRA_PROPERTIES")
            else getattr(settings, "POSTHOG_MW_EXTRA_TAGS", None)
        )
        self.extra_properties = (
            cast("Callable[[HttpRequest], Dict[str, Any]]", extra_properties)
            if callable(extra_properties)
            else None
        )

        if hasattr(settings, "POSTHOG_MW_REQUEST_FILTER") and callable(
            settings.POSTHOG_MW_REQUEST_FILTER
        ):
            self.request_filter = cast(
                "Optional[Callable[[HttpRequest], bool]]",
                settings.POSTHOG_MW_REQUEST_FILTER,
            )
        else:
            self.request_filter = None

        properties_map = (
            settings.POSTHOG_MW_PROPERTIES_MAP
            if hasattr(settings, "POSTHOG_MW_PROPERTIES_MAP")
            else getattr(settings, "POSTHOG_MW_TAG_MAP", None)
        )
        self.properties_map = (
            cast("Callable[[Dict[str, Any]], Dict[str, Any]]", properties_map)
            if callable(properties_map)
            else None
        )

        capture_exceptions = getattr(settings, "POSTHOG_MW_CAPTURE_EXCEPTIONS", True)
        self.capture_exceptions: Optional[bool] = (
            capture_exceptions
            if capture_exceptions is None or isinstance(capture_exceptions, bool)
            else True
        )
        trust_tracing_headers = getattr(
            settings, "POSTHOG_MW_TRUST_TRACING_HEADERS", False
        )
        self.trust_tracing_headers = (
            trust_tracing_headers if isinstance(trust_tracing_headers, bool) else False
        )

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
            # Zero falls back to the default, like posthog-js.
            and idle_timeout_seconds != 0
            else _COOKIE_SESSION_IDLE_TIMEOUT_MS
        )

    @property
    def extra_tags(self):
        """Compatibility alias for ``extra_properties``."""
        return self.extra_properties

    @extra_tags.setter
    def extra_tags(self, callback):
        self.extra_properties = callback

    @property
    def tag_map(self):
        """Compatibility alias for ``properties_map``."""
        return self.properties_map

    @tag_map.setter
    def tag_map(self, callback):
        self.properties_map = callback

    def extract_tags(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        """Compatibility alias for ``extract_properties``."""
        return self.extract_properties(request)

    def extract_properties(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        """Extract event properties and identity from a synchronous request."""
        user_id, user_email = self.extract_request_user(request)
        return self._build_tags(request, user_id, user_email)

    def _build_tags(self, request, user_id, user_email):
        # type: (HttpRequest, Optional[str], Optional[str]) -> Dict[str, Any]
        """
        Build tags dict from request and user info.

        Centralized tag extraction logic used by both sync and async paths.
        """
        tags = {}

        # Client-controlled headers are analytics context, not authenticated identity.
        header_session_id = None
        header_distinct_id = None
        if self.trust_tracing_headers:
            header_session_id = _get_sanitized_tracing_header(
                request, "X-POSTHOG-SESSION-ID"
            )
            header_distinct_id = _get_sanitized_tracing_header(
                request, "X-POSTHOG-DISTINCT-ID"
            )
        # The cookie is read only without trusted tracing headers, so one request never mixes two identities.
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

        # Session ID from a trusted X-POSTHOG-SESSION-ID header or the posthog-js cookie
        session_id = header_session_id or cookie_session_id
        if session_id:
            contexts.set_context_session(session_id)

        # Distinct ID from a trusted X-POSTHOG-DISTINCT-ID header, request user id, or the posthog-js cookie
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

        # Apply application-specific properties, then the optional transform.
        if self.extra_properties:
            extra = self.extra_properties(request)
            if extra:
                tags.update(extra)

        if self.properties_map:
            tags = self.properties_map(tags)

        return tags

    def extract_request_user(self, request):
        # type: (HttpRequest) -> tuple[Optional[str], Optional[str]]
        """Extract user ID and email from request in sync context."""
        user = getattr(request, "user", None)
        return self._resolve_user_details(user)

    async def aextract_tags(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        """Compatibility alias for ``aextract_properties``."""
        return await self.aextract_properties(request)

    async def aextract_properties(self, request):
        # type: (HttpRequest) -> Dict[str, Any]
        """
        Async version of extract_properties for use in async request handling.

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

        capture_exceptions = (
            self.capture_exceptions
            if self.capture_exceptions is not None
            else contexts._default_capture_exceptions(self.client)
        )
        if not capture_exceptions:
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
            from .. import capture_exception

            cast(Any, capture_exception)(exception, _capture_metadata=capture_metadata)
