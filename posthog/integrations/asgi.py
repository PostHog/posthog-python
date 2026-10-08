"""Framework-independent ASGI request context and exception capture.

The middleware speaks the ASGI protocol directly and has no dependency on an ASGI
framework. It can therefore be used with FastAPI, Starlette, Litestar, or a raw
ASGI application::

    from fastapi import FastAPI
    from posthog.integrations.asgi import PosthogASGIMiddleware

    app = FastAPI()
    app.add_middleware(PosthogASGIMiddleware)

It can also wrap an application directly::

    app = PosthogASGIMiddleware(app)

Only HTTP and WebSocket connections are instrumented. Lifespan and custom ASGI
scope types pass through unchanged.
"""

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Optional, Union, cast

from .. import contexts
from ..client import Client
from ..exception_utils import _capture_exception_with_metadata

_ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]
_RequestFilterResult = Union[bool, Awaitable[bool]]
_RequestFilter = Callable[[dict[str, Any]], _RequestFilterResult]
_ExtraTagsResult = Union[
    Optional[Mapping[str, Any]], Awaitable[Optional[Mapping[str, Any]]]
]
_ExtraTags = Callable[[dict[str, Any]], _ExtraTagsResult]

_MAX_HEADER_LENGTH = 1000
_MAX_PATH_LENGTH = 2048
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CAPTURE_METADATA = {
    "level": "error",
    "source": "asgi.middleware",
    "mechanism": {"type": "middleware", "handled": False},
}


def _sanitize_text(
    value: object, max_length: int = _MAX_HEADER_LENGTH
) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return _CONTROL_CHARS_RE.sub("", value).strip()[:max_length] or None


def _decode_header(value: object) -> Optional[str]:
    if not isinstance(value, bytes):
        return None
    return _sanitize_text(value.decode("latin-1"))


def _headers_from_scope(scope: Mapping[str, Any]) -> dict[bytes, bytes]:
    result: dict[bytes, bytes] = {}
    headers = scope.get("headers", ())
    if not isinstance(headers, (list, tuple)):
        return result

    for item in headers:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            # Keep the first value. Tracing headers are singular, and joining arbitrary
            # duplicate user input can create misleading identity values.
            result.setdefault(item[0].lower(), item[1])
    return result


def _server_host(scope: Mapping[str, Any]) -> Optional[str]:
    server = scope.get("server")
    if not isinstance(server, (list, tuple)) or len(server) != 2:
        return None

    hostname, port = server
    if not isinstance(hostname, str) or not isinstance(port, int):
        return None

    hostname = _sanitize_text(hostname)
    if not hostname:
        return None
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    scheme = scope.get("scheme")
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    return hostname if default_port else f"{hostname}:{port}"


def _extract_tags(
    scope: Mapping[str, Any], headers: Mapping[bytes, bytes]
) -> dict[str, Any]:
    tags: dict[str, Any] = {}

    method = _sanitize_text(scope.get("method"), max_length=32)
    if method:
        tags["$request_method"] = method

    path = _sanitize_text(scope.get("path"), max_length=_MAX_PATH_LENGTH)
    if path:
        tags["$request_path"] = path

    user_agent = _decode_header(headers.get(b"user-agent"))
    if user_agent:
        tags["$user_agent"] = user_agent
        tags["$raw_user_agent"] = user_agent

    forwarded_for = _decode_header(headers.get(b"x-forwarded-for"))
    if forwarded_for:
        ip_address = _sanitize_text(forwarded_for.split(",", 1)[0])
    else:
        client = scope.get("client")
        ip_address = (
            _sanitize_text(client[0])
            if isinstance(client, (list, tuple))
            and client
            and isinstance(client[0], str)
            else None
        )
    if ip_address:
        tags["$ip"] = ip_address

    scheme = _sanitize_text(scope.get("scheme"), max_length=16)
    host = _decode_header(headers.get(b"host")) or _server_host(scope)
    if scheme and host and path:
        # Deliberately omit query strings: they commonly contain secrets and
        # high-cardinality values. The path remains available separately.
        tags["$current_url"] = f"{scheme}://{host}{path}"

    return tags


async def _resolve_callback_result(value):
    if inspect.isawaitable(value):
        return await value
    return value


class PosthogASGIMiddleware:
    """Add PostHog context and automatic exception capture to an ASGI app.

    Args:
        app: The downstream ASGI application.
        client: Optional PostHog client. The global client is used by default.
        capture_exceptions: Capture exceptions escaping the downstream app.
        request_filter: Optional sync or async callback receiving the ASGI scope.
            Returning ``False`` bypasses all instrumentation for that scope.
        extra_tags: Optional sync or async callback receiving the ASGI scope and
            returning additional context tags.
    """

    def __init__(
        self,
        app: _ASGIApp,
        client: Optional[Client] = None,
        capture_exceptions: bool = True,
        request_filter: Optional[_RequestFilter] = None,
        extra_tags: Optional[_ExtraTags] = None,
    ) -> None:
        self.app = app
        self.client = client
        self.capture_exceptions = capture_exceptions
        self.request_filter = request_filter
        self.extra_tags = extra_tags

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        if self.request_filter and not await _resolve_callback_result(
            self.request_filter(scope)
        ):
            await self.app(scope, receive, send)
            return

        # Exception capture is explicit below so integration-specific mechanism
        # metadata is preserved. The context itself must not capture a second time.
        with contexts.new_context(capture_exceptions=False, client=self.client):
            headers = _headers_from_scope(scope)
            session_id = _decode_header(headers.get(b"x-posthog-session-id"))
            if session_id:
                contexts.set_context_session(session_id)

            distinct_id = _decode_header(headers.get(b"x-posthog-distinct-id"))
            if distinct_id:
                contexts.identify_context(distinct_id)

            tags = _extract_tags(scope, headers)
            if self.extra_tags:
                extra_tags = await _resolve_callback_result(self.extra_tags(scope))
                if extra_tags:
                    tags.update(extra_tags)
            for key, value in tags.items():
                contexts.tag(key, value)

            try:
                await self.app(scope, receive, send)
            except Exception as exception:
                if self.capture_exceptions:
                    if self.client:
                        _capture_exception_with_metadata(
                            self.client, exception, _CAPTURE_METADATA
                        )
                    else:
                        from .. import capture_exception

                        cast(Any, capture_exception)(
                            exception, _capture_metadata=_CAPTURE_METADATA
                        )
                raise
