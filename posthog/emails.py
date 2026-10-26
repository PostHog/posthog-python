"""Transactional email, sent from an email template in PostHog.

Setup:

1. In PostHog, open the email template under Workflows, make it transactional,
   and copy its ID. ``template_id`` is that UUID.
2. Create a project secret API key (``phs_...``) with Workflow write access
   (``hog_flow:write``) in your project settings. The project API key
   (``phc_...``) captures events but cannot send email.
3. Send from the client you already use, or from :class:`Emails` in a process
   that only sends email:

```python
from posthog import Posthog

posthog = Posthog("phc_...", secret_key="phs_...", host="https://eu.i.posthog.com")
result = posthog.emails.send(
    to="user@example.com",
    template_id="<template UUID>",
    template_data={"name": "Ada"},  # fills {{ variables.name }}
    distinct_id="user_123",
    idempotency_key="welcome:user_123",
)
print(result.id)  # the queued email
```

``posthog.emails`` above is the client's property. For async code, use
``AsyncPosthog`` or :class:`AsyncEmails` with ``posthog[async]`` installed.

Where sends go: a client on PostHog Cloud sends to its region's app host,
``us.posthog.com`` or ``eu.posthog.com``. For any other ``host``, such as a
reverse proxy, set ``app_host`` on the client, because the proxy must not
receive the secret key. :class:`Emails` takes the app host as ``host``.

What a result means: PostHog queued the email. Delivery happens later, and the
template's workflow in PostHog shows its sends, deliveries and failures.

Timeouts and retries: each request times out after 15 seconds. A send retries
network errors, rate limits and an unavailable service twice, waiting about 1
and then 2 seconds, or the server's ``Retry-After`` if that is longer. A
``Retry-After`` over 30 seconds raises at once. Every attempt sends the same
idempotency key, so a retry never sends twice. Set
``email_request_max_retries=0`` on the client, or ``max_retries=0`` on
:class:`Emails`, when a job runner already retries the send, and pass an
``idempotency_key`` that names the email, such as ``welcome:user_123``.

Errors: a send that is not queued raises :class:`EmailError`. Branch on
``code``. The ones a setup usually hits:

- ``missing_secret_key``: the client has no ``secret_key``.
- ``missing_app_host``: the client's ``host`` is not PostHog Cloud. Set ``app_host``.
- ``authentication_failed`` (401) or ``permission_denied`` (403): the key is
  wrong, or it lacks ``hog_flow:write``.
- ``not_found`` (404): the template ID is wrong, or transactional email is not
  enabled for the project.
- ``client_closed``: the client is shut down.

Cleanup: ``shutdown()`` on the client closes its email connections. Close a
standalone :class:`Emails` with ``close()`` or a ``with`` block.

Testing without network calls: pass your own transport, ``session`` to
:class:`Emails` or ``http_client`` to :class:`AsyncEmails`, with a fake adapter
or ``httpx.MockTransport``. Any library that mocks ``requests`` or ``httpx``
works too. A disabled client returns a result with ``id=None`` and sends nothing.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Callable, Mapping, Optional, TypeVar, Union
from uuid import UUID, uuid4

import requests

from .capture_v1 import _parse_retry_after
from .request import (
    EU_APP_ENDPOINT,
    US_APP_ENDPOINT,
    DatetimeSerializer,
    determine_app_host,
)
from .utils import clean
from .version import VERSION

__all__ = ["AsyncEmails", "EmailError", "EmailWarning", "Emails", "SendEmailResult"]

log = logging.getLogger("posthog")

_SEND_PATH = "/api/projects/@current/messaging_templates/{template_id}/send/"
_MAX_RETRY_DELAY = 30.0
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,255}$")
_IN_FLIGHT_CODES = frozenset({"idempotency_key_in_flight"})
_QUOTA_CODES = frozenset({"quota_limited"})


class EmailError(Exception):
    """PostHog did not queue the email, or the client could not tell whether it did.

    Branch on ``code``, PostHog's stable error code, or on ``name``, which uses
    Resend's error names. The message never contains the secret key or the
    request body.
    """

    def __init__(
        self,
        *,
        name: str,
        code: str,
        message: str,
        status_code: Optional[int] = None,
        why: Optional[str] = None,
        fix: Optional[str] = None,
        docs_url: Optional[str] = None,
        field: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.code = code
        self.message = message
        self.status_code = status_code
        self.why = why
        self.fix = fix
        self.docs_url = docs_url
        self.field = field
        self.retry_after = retry_after


@dataclass(frozen=True)
class EmailWarning:
    code: str
    message: str
    fix: Optional[str] = None


@dataclass(frozen=True)
class SendEmailResult:
    """The queued email. ``id`` is ``None`` when the client is disabled and sent nothing."""

    id: Optional[str]
    replayed: bool = False
    warnings: tuple[EmailWarning, ...] = ()


@dataclass(frozen=True)
class _PreparedSend:
    path: str
    body: bytes
    idempotency_key: str


def _prepare_send(
    to: str,
    template_id: Union[str, UUID],
    template_data: Optional[Mapping[str, Any]],
    distinct_id: Optional[str],
    idempotency_key: Optional[str],
) -> _PreparedSend:
    if not isinstance(to, str):
        raise TypeError("to must be one email address as a string.")
    if not to.strip():
        raise ValueError("to must not be empty.")
    try:
        template_uuid = (
            template_id if isinstance(template_id, UUID) else UUID(template_id)
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("template_id must be the template's UUID.") from None
    if template_data is not None and not isinstance(template_data, Mapping):
        raise TypeError("template_data must be a mapping.")
    if distinct_id is not None and (
        not isinstance(distinct_id, str) or not distinct_id
    ):
        raise ValueError("distinct_id must be a nonempty string.")
    if idempotency_key is None:
        idempotency_key = str(uuid4())
    elif not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.match(
        idempotency_key
    ):
        raise ValueError(
            "idempotency_key must be 1 to 255 visible ASCII characters without spaces."
        )

    payload: dict[str, Any] = {
        "to": to,
        "message_data": clean(dict(template_data or {})),
    }
    if distinct_id is not None:
        payload["identifiers"] = {"distinct_id": distinct_id}
    return _PreparedSend(
        path=_SEND_PATH.format(template_id=template_uuid),
        body=json.dumps(payload, cls=DatetimeSerializer).encode("utf-8"),
        idempotency_key=idempotency_key,
    )


def _validate_options(timeout: float, max_retries: int) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number of seconds.")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be a nonnegative integer.")


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _error_name(status: Optional[int], code: str) -> str:
    if code == "missing_secret_key" or status == 401:
        return "missing_api_key"
    if status is None:
        return "service_unavailable" if code == "network_error" else "application_error"
    if status == 400:
        return "validation_error"
    if status == 403:
        return "invalid_permission"
    if status == 404:
        return "not_found"
    if status == 409:
        return (
            "validation_error"
            if code == "template_outdated"
            else "concurrent_idempotent_requests"
        )
    if status == 422:
        # The template send route also answers 422 for send checks such as recipient_suppressed.
        return (
            "invalid_idempotent_request"
            if code == "idempotency_key_reused"
            else "validation_error"
        )
    if status == 429:
        return (
            "monthly_quota_exceeded" if code in _QUOTA_CODES else "rate_limit_exceeded"
        )
    if status == 503:
        return "service_unavailable"
    return "application_error"


def _sdk_error(code: str, message: str, status: Optional[int] = None) -> EmailError:
    return EmailError(
        name=_error_name(status, code), code=code, message=message, status_code=status
    )


def _retry_after(header: Optional[str]) -> Optional[float]:
    seconds = _parse_retry_after(header)
    if seconds is None or not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _parse_response(
    status: int, payload: Any, retry_after_header: Optional[str]
) -> SendEmailResult:
    fields = payload if isinstance(payload, dict) else {}
    if 200 <= status < 300:
        message_id = _text(fields.get("message_id"))
        if message_id is None:
            raise _sdk_error(
                "api_error", "PostHog answered without a message ID.", status
            )
        warnings = fields.get("warnings")
        return SendEmailResult(
            id=message_id,
            replayed=fields.get("replayed") is True,
            warnings=tuple(
                EmailWarning(
                    code=_text(warning.get("code")) or "warning",
                    message=_text(warning.get("detail"))
                    or _text(warning.get("message"))
                    or "",
                    fix=_text(warning.get("fix")),
                )
                for warning in (warnings if isinstance(warnings, list) else [])
                if isinstance(warning, dict)
            ),
        )
    code = _text(fields.get("code")) or "api_error"
    raise EmailError(
        name=_error_name(status, code),
        code=code,
        message=_text(fields.get("detail")) or f"PostHog answered HTTP {status}.",
        status_code=status,
        why=_text(fields.get("why")),
        fix=_text(fields.get("fix")),
        docs_url=_text(fields.get("docs_url")),
        field=_text(fields.get("attr")),
        retry_after=_retry_after(retry_after_header),
    )


def _is_retryable(error: EmailError) -> bool:
    # Every request carries an idempotency key, so a retry can't queue a second email.
    if error.status_code is None:
        return error.code == "network_error"
    if error.status_code == 409:
        return error.code in _IN_FLIGHT_CODES
    if error.status_code == 429:
        return error.code not in _QUOTA_CODES
    return error.status_code == 503


def _retry_delay(error: EmailError, attempt: int, max_retries: int) -> Optional[float]:
    if attempt >= max_retries or not _is_retryable(error):
        return None
    # Retrying before a longer Retry-After would break the server's rate limit.
    if error.retry_after is not None and error.retry_after > _MAX_RETRY_DELAY:
        return None
    backoff = min(_MAX_RETRY_DELAY, 2.0**attempt * random.uniform(0.8, 1.2))
    return max(backoff, error.retry_after or 0.0)


def _no_op() -> None:
    pass


def _log_warnings(result: SendEmailResult) -> None:
    for warning in result.warnings:
        log.warning("PostHog email warning %s: %s", warning.code, warning.message)


def _headers(secret_key: str, idempotency_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "User-Agent": f"posthog-python/{VERSION}",
    }


class _BearerAuth(requests.auth.AuthBase):
    def __init__(self, secret_key: str, idempotency_key: str) -> None:
        self._headers = _headers(secret_key, idempotency_key)

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        # Setting auth explicitly stops requests from substituting .netrc credentials.
        request.headers.update(self._headers)
        return request


class _EmailsBase:
    def __init__(
        self,
        secret_key: Optional[str],
        *,
        host: Optional[str],
        timeout: float,
        max_retries: int,
        disabled: bool,
    ) -> None:
        _validate_options(timeout, max_retries)
        self.secret_key = secret_key.strip() if isinstance(secret_key, str) else None
        self.host = determine_app_host(host)
        self.timeout = timeout
        self.max_retries = max_retries
        self.disabled = disabled
        self._parent_disabled: Callable[[], bool] = lambda: False
        self._admit: Callable[[], Optional[Callable[[], None]]] = lambda: _no_op
        self._host_error: Optional[str] = None

    def _is_disabled(self) -> bool:
        return self.disabled or self._parent_disabled()

    def _start_send(self) -> Callable[[], None]:
        finish = self._admit()
        if finish is None:
            raise _sdk_error(
                "client_closed",
                "The PostHog client is shut down, so no email was sent.",
            )
        return finish

    def _secret_key(self) -> str:
        if self._host_error:
            raise _sdk_error("missing_app_host", self._host_error)
        if not self.secret_key:
            raise _sdk_error(
                "missing_secret_key",
                "Sending email needs secret_key, a project secret API key with the hog_flow:write scope.",
            )
        return self.secret_key


class Emails(_EmailsBase):
    """Sends transactional email from PostHog email templates.

    ``host`` is your PostHog app host. PostHog Cloud ingestion hosts map to the
    app host, so ``us.i.posthog.com`` sends to ``us.posthog.com``. Any other
    host is used as given.

    Each send retries network errors, rate limits and an unavailable service up
    to ``max_retries`` times, with the same idempotency key. Set
    ``max_retries=0`` when a job runner already retries the send. ``timeout``
    is in seconds, per request. Pass ``session`` to send through your own
    ``requests.Session``, for example a test double.
    """

    def __init__(
        self,
        secret_key: Optional[str],
        *,
        host: Optional[str] = None,
        timeout: float = 15,
        max_retries: int = 2,
        disabled: bool = False,
        session: Optional[requests.Session] = None,
    ) -> None:
        super().__init__(
            secret_key,
            host=host,
            timeout=timeout,
            max_retries=max_retries,
            disabled=disabled,
        )
        self._session: Optional[requests.Session] = session
        self._owns_session = session is None
        self._session_lock = threading.Lock()

    def send(
        self,
        *,
        to: str,
        template_id: Union[str, UUID],
        template_data: Optional[Mapping[str, Any]] = None,
        distinct_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> SendEmailResult:
        """Queue one email from a transactional email template.

        Args:
            to: One recipient email address.
            template_id: The UUID of a template made transactional in PostHog.
            template_data: Values for the template's ``{{ variables.* }}``
                placeholders. Values are encoded the way capture encodes
                properties.
            distinct_id: The recipient's distinct ID in your product. The
                email's events go to this person. Without it, they go to a
                person whose distinct ID is the email address.
            idempotency_key: 1 to 255 visible ASCII characters. A send with a
                key that PostHog has already accepted queues nothing and returns
                the first send's result. Without it, the client makes a key
                that covers its own retries. Pass a key derived from what the
                email is for, such as ``welcome:user_123``, to cover retries
                outside this call, too.

        Returns:
            The queued email. A result means PostHog queued the email, not that
            it was delivered.

        Raises:
            EmailError: PostHog did not queue the email, or the client could
                not tell whether it did.
            TypeError, ValueError: An argument has the wrong type or format.

        Examples:
            ```python
            emails.send(
                to="user@example.com",
                template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
                template_data={"name": "Ada"},
                distinct_id="user_123",
                idempotency_key="welcome:user_123",
            )
            ```
        """
        prepared = _prepare_send(
            to, template_id, template_data, distinct_id, idempotency_key
        )
        if self._is_disabled():
            log.debug("PostHog client is disabled, so no email was sent.")
            return SendEmailResult(id=None)
        finish = self._start_send()
        try:
            return self._deliver(prepared)
        finally:
            finish()

    def _deliver(self, prepared: _PreparedSend) -> SendEmailResult:
        auth = _BearerAuth(self._secret_key(), prepared.idempotency_key)
        session = self._get_session()
        url = self.host + prepared.path
        for attempt in range(self.max_retries + 1):
            try:
                response = session.request(
                    "POST",
                    url,
                    data=prepared.body,
                    auth=auth,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                try:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    result = _parse_response(
                        response.status_code,
                        payload,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    response.close()
                _log_warnings(result)
                return result
            except requests.exceptions.RequestException as exc:
                error = _sdk_error("network_error", "The request to PostHog failed.")
                error.__cause__ = exc
            except EmailError as exc:
                error = exc
            delay = _retry_delay(error, attempt, self.max_retries)
            if delay is None:
                raise error
            time.sleep(delay)
        raise AssertionError("unreachable")

    def close(self) -> None:
        """Close the HTTP connections this client opened. A ``session`` you passed in stays open."""
        with self._session_lock:
            if self._session is not None and self._owns_session:
                self._session.close()
                self._session = None

    def __enter__(self) -> Emails:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()

    def _get_session(self) -> requests.Session:
        with self._session_lock:
            if self._session is None:
                session = requests.Session()
                session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
                self._session = session
            return self._session


class AsyncEmails(_EmailsBase):
    """The async counterpart of :class:`Emails`. Needs ``posthog[async]``.

    Pass ``http_client`` to send through your own ``httpx.AsyncClient``, for
    example one with an ``httpx.MockTransport`` in tests.
    """

    def __init__(
        self,
        secret_key: Optional[str],
        *,
        host: Optional[str] = None,
        timeout: float = 15,
        max_retries: int = 2,
        disabled: bool = False,
        http_client: Any = None,
    ) -> None:
        super().__init__(
            secret_key,
            host=host,
            timeout=timeout,
            max_retries=max_retries,
            disabled=disabled,
        )
        self._client: Any = http_client
        self._owns_client = http_client is None

    async def send(
        self,
        *,
        to: str,
        template_id: Union[str, UUID],
        template_data: Optional[Mapping[str, Any]] = None,
        distinct_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> SendEmailResult:
        """Queue one email from a transactional email template.

        Takes the same arguments and raises the same errors as
        :meth:`Emails.send`.

        Examples:
            ```python
            await emails.send(
                to="user@example.com",
                template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b",
                template_data={"name": "Ada"},
                distinct_id="user_123",
            )
            ```
        """
        prepared = _prepare_send(
            to, template_id, template_data, distinct_id, idempotency_key
        )
        if self._is_disabled():
            log.debug("PostHog client is disabled, so no email was sent.")
            return SendEmailResult(id=None)
        finish = self._start_send()
        try:
            return await self._deliver(prepared)
        finally:
            finish()

    async def _deliver(self, prepared: _PreparedSend) -> SendEmailResult:
        headers = _headers(self._secret_key(), prepared.idempotency_key)
        httpx, client = self._get_client()
        url = self.host + prepared.path
        for attempt in range(self.max_retries + 1):
            try:
                # An injected client may carry its own auth and timeout, so set both per request: auth=None keeps
                # the secret key's Bearer header, and the send timeout applies whatever the client's default.
                response = await client.post(
                    url,
                    content=prepared.body,
                    headers=headers,
                    auth=None,
                    timeout=self.timeout,
                )
                try:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    result = _parse_response(
                        response.status_code,
                        payload,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    await response.aclose()
                _log_warnings(result)
                return result
            except httpx.RequestError as exc:
                error = _sdk_error("network_error", "The request to PostHog failed.")
                error.__cause__ = exc
            except EmailError as exc:
                error = exc
            delay = _retry_delay(error, attempt, self.max_retries)
            if delay is None:
                raise error
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        """Close the HTTP connections this client opened. An ``http_client`` you passed in stays open."""
        if self._client is not None and self._owns_client:
            client, self._client = self._client, None
            await client.aclose()

    async def __aenter__(self) -> AsyncEmails:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    def _get_client(self) -> tuple[Any, Any]:
        try:
            httpx = importlib.import_module("httpx")
        except ImportError:
            raise RuntimeError(
                "Async email sending needs httpx. Install it with `posthog[async]`."
            ) from None
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            )
        return httpx, self._client


_EmailsT = TypeVar("_EmailsT", bound=_EmailsBase)


def _client_emails(
    factory: Callable[..., _EmailsT],
    secret_key: Optional[str],
    client_host: Optional[str],
    is_disabled: Callable[[], bool],
    admit: Callable[[], Optional[Callable[[], None]]],
    *,
    app_host: Optional[str],
    timeout: float,
    max_retries: int,
) -> _EmailsT:
    if app_host:
        emails = factory(
            secret_key, host=app_host, timeout=timeout, max_retries=max_retries
        )
        emails._parent_disabled = is_disabled
        emails._admit = admit
        return emails
    inferred_host = determine_app_host(client_host)
    is_cloud = inferred_host in (US_APP_ENDPOINT, EU_APP_ENDPOINT)
    emails = factory(
        secret_key,
        host=inferred_host if is_cloud else None,
        timeout=timeout,
        max_retries=max_retries,
    )
    if not is_cloud:
        # A custom host is often an ingestion proxy, which must not receive the secret key.
        emails._host_error = (
            "The client's host is not PostHog Cloud, so the client can't tell where to send email. "
            "Set app_host to your PostHog app host on the client."
        )
    emails._parent_disabled = is_disabled
    emails._admit = admit
    return emails
