"""Transactional push notifications, sent from a push template in PostHog.

Setup:

1. In PostHog, open the push notification under Workflows, make it sendable
   from the API, and copy its ID. ``template_id`` is that UUID.
2. Create a project secret API key (``phs_...``) with the
   ``transactional_push:write`` scope in your project settings.
3. Send from the client you already use, or from :class:`Push` in a process
   that only sends notifications:

```python
from posthog import Posthog

posthog = Posthog("phc_...", secret_key="phs_...", host="https://eu.i.posthog.com")
result = posthog.push.send(
    template_id="<template UUID>",
    distinct_id="user_123",
    template_data={"order_id": "1042"},  # fills {{ variables.order_id }}
    idempotency_key="order-shipped:1042",
)
print(result.id)  # the queued send
```

The notification goes to every device the person registered for the
template's push channels. A person with no registered device gets nothing,
and the send shows as skipped in the template's sends. The template can't
read the person's properties, so pass everything it needs in
``template_data``.

Sends go where ``posthog.emails`` sends go, and use the client's
``email_request_timeout_seconds`` and ``email_request_max_retries``. For
async code, use ``AsyncPosthog`` or :class:`AsyncPush` with
``posthog[async]`` installed.

Errors: a send that is not queued raises :class:`PushError`. Branch on
``code``, such as ``transactional_push_paused`` when the template's workflow
is turned off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Mapping, Optional, Union
from uuid import UUID, uuid4

import requests

from .emails import (
    AsyncEmails,
    EmailError,
    Emails,
    _IDEMPOTENCY_KEY,
    _PreparedSend,
    _retry_limit,
)
from .request import DatetimeSerializer
from .utils import clean

__all__ = ["AsyncPush", "Push", "PushError", "SendPushResult"]

_SEND_PATH = "/api/projects/@current/messaging/send/push/"
_MAX_DISTINCT_ID_LENGTH = 400
_NOT_DONE = "no push notification was sent"
_KEY_NEEDED = (
    "Sending push notifications needs secret_key, a project secret API key with the "
    "transactional_push:write scope."
)


class PushError(Exception):
    """PostHog did not queue the push notification, or the client could not tell whether it did.

    Branch on ``code``, PostHog's stable error code, or on ``name``, a coarse
    category of the failure. The message never contains the secret key or the
    request body.

    Attributes:
        name: A coarse category of the failure.
        code: PostHog's stable error code.
        message: What went wrong.
        status_code: The HTTP status of the response, or ``None`` when none arrived.
        why: PostHog's explanation of the failure, when it gave one.
        fix: What to change to fix it, when PostHog said.
        docs_url: A docs page about the error, when PostHog gave one.
        field: The request field the error is about, when there is one.
        retry_after: Seconds to wait before retrying, when PostHog said.
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

    @classmethod
    def _from_email_error(cls, error: EmailError) -> PushError:
        return cls(
            name=error.name,
            code=error.code,
            message=error.message,
            status_code=error.status_code,
            why=error.why,
            fix=error.fix,
            docs_url=error.docs_url,
            field=error.field,
            retry_after=error.retry_after,
        )


@dataclass(frozen=True)
class SendPushResult:
    """The queued push notification. ``id`` is its message ID.

    A result means PostHog queued the notification, not that a device received it.

    Attributes:
        id: The send's message ID.
        replayed: ``True`` when PostHog had already accepted this
            ``idempotency_key`` and returned the first send's result.
    """

    id: str
    replayed: bool = False


def _prepare(
    template_id: Union[str, UUID],
    distinct_id: str,
    template_data: Optional[Mapping[str, Any]],
    idempotency_key: Optional[str],
    max_retries: Optional[int],
) -> _PreparedSend:
    if (
        not isinstance(distinct_id, str)
        or not distinct_id
        or len(distinct_id) > _MAX_DISTINCT_ID_LENGTH
    ):
        raise ValueError(
            f"distinct_id must be the person's distinct ID, a nonempty string of at most "
            f"{_MAX_DISTINCT_ID_LENGTH} characters."
        )
    if idempotency_key is None:
        idempotency_key = str(uuid4())
    elif not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.match(
        idempotency_key
    ):
        raise ValueError(
            "idempotency_key must be 1 to 255 visible ASCII characters without spaces."
        )
    max_retries = _retry_limit(max_retries)
    try:
        template_uuid = (
            template_id if isinstance(template_id, UUID) else UUID(template_id)
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("template_id must be the push template's UUID.") from None
    if template_data is not None and not isinstance(template_data, Mapping):
        raise TypeError("template_data must be a mapping.")
    payload = {
        "template_id": str(template_uuid),
        "distinct_id": distinct_id,
        "message_data": clean(dict(template_data or {})),
    }
    return _PreparedSend(
        path=_SEND_PATH,
        body=json.dumps(payload, cls=DatetimeSerializer).encode("utf-8"),
        idempotency_key=idempotency_key,
        max_retries=max_retries,
    )


def _result(result: Any) -> SendPushResult:
    return SendPushResult(id=result.id, replayed=result.replayed)


class Push:
    """Sends transactional push notifications from PostHog push templates.

    ``host`` is your PostHog app host. PostHog Cloud ingestion hosts map to the
    app host, so ``us.i.posthog.com`` sends to ``us.posthog.com``. Any other
    host is used as given.

    Each send retries network errors, rate limits and an unavailable service up
    to ``max_retries`` times, with the same idempotency key. ``timeout`` is in
    seconds, per request. Pass ``session`` to send through your own
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
        self._emails = Emails(
            secret_key,
            host=host,
            timeout=timeout,
            max_retries=max_retries,
            disabled=disabled,
            session=session,
        )

    @classmethod
    def _over(cls, emails: Emails) -> Push:
        push = cls.__new__(cls)
        push._emails = emails
        return push

    def send(
        self,
        *,
        template_id: Union[str, UUID],
        distinct_id: str,
        template_data: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> SendPushResult:
        """Queue one push notification to every device of one person.

        Args:
            template_id: The UUID of a push template made transactional in PostHog.
            distinct_id: The distinct ID of the person to notify.
            template_data: Values for the template's ``{{ variables.* }}``
                placeholders. Values are encoded the way capture encodes
                properties.
            idempotency_key: 1 to 255 visible ASCII characters. A send with a
                key that PostHog has already accepted queues nothing and returns
                the first send's result. Without it, the client makes a key
                that covers its own retries. Pass a key derived from what the
                notification is for, such as ``order-shipped:1042``, to cover
                retries outside this call, too.
            max_retries: Overrides the client's maximum number of retries for
                this send. ``0`` turns retries off.

        Returns:
            The queued notification. A result means PostHog queued it, not
            that a device received it.

        Raises:
            PushError: PostHog did not queue the notification, or the client
                could not tell whether it did.
            TypeError, ValueError: An argument has the wrong type or format.
        """
        prepared = _prepare(
            template_id, distinct_id, template_data, idempotency_key, max_retries
        )
        try:
            return _result(self._emails._queue(prepared, _NOT_DONE, _KEY_NEEDED))
        except EmailError as error:
            raise PushError._from_email_error(error) from error.__cause__

    def close(self) -> None:
        """Close the HTTP connections this client opened. A ``session`` you passed in stays open."""
        self._emails.close()

    def __enter__(self) -> Push:
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()


class AsyncPush:
    """The async counterpart of :class:`Push`. Needs ``posthog[async]``.

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
        self._emails = AsyncEmails(
            secret_key,
            host=host,
            timeout=timeout,
            max_retries=max_retries,
            disabled=disabled,
            http_client=http_client,
        )

    @classmethod
    def _over(cls, emails: AsyncEmails) -> AsyncPush:
        push = cls.__new__(cls)
        push._emails = emails
        return push

    async def send(
        self,
        *,
        template_id: Union[str, UUID],
        distinct_id: str,
        template_data: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> SendPushResult:
        """Queue one push notification to every device of one person.

        Takes the same arguments and raises the same errors as :meth:`Push.send`.
        """
        prepared = _prepare(
            template_id, distinct_id, template_data, idempotency_key, max_retries
        )
        try:
            return _result(await self._emails._queue(prepared, _NOT_DONE, _KEY_NEEDED))
        except EmailError as error:
            raise PushError._from_email_error(error) from error.__cause__

    async def aclose(self) -> None:
        """Close the HTTP connections this client opened. An ``http_client`` you passed in stays open."""
        await self._emails.aclose()

    async def __aenter__(self) -> AsyncPush:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.aclose()
