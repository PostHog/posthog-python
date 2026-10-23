"""Transactional email, sent from an email template in PostHog or from content in your code.

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

# Content you rendered for this recipient, named by a key
posthog.emails.send(
    to="user@example.com",
    template_key="password-reset",
    subject="Reset your password",
    html=rendered_html,
    distinct_id="user_123",
)

# A template in your code. PostHog fills in the variables, and the client
# uploads the template whenever its content changes.
posthog.emails.send(
    to="user@example.com",
    template_key="welcome",
    subject="Welcome, {{ variables.name }}",
    html=welcome_html,
    template_data={"name": "Ada"},
    distinct_id="user_123",
)
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
- ``app_host_required``: the client's ``host`` is not PostHog Cloud. Set ``app_host``.
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
import base64
import hashlib
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
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Sequence,
    TypedDict,
    TypeVar,
    Union,
    overload,
)
from uuid import UUID, uuid4

import requests
from typing_extensions import NotRequired, Unpack

from .capture_v1 import _parse_retry_after
from .request import (
    EU_APP_ENDPOINT,
    US_APP_ENDPOINT,
    DatetimeSerializer,
    determine_app_host,
)
from .utils import clean
from .version import VERSION

__all__ = [
    "AsyncEmails",
    "CodeEmailArgs",
    "EmailAttachment",
    "EmailError",
    "EmailTag",
    "EmailWarning",
    "Emails",
    "SendEmailResult",
    "TemplateEmailArgs",
]

log = logging.getLogger("posthog")

_SEND_PATH = "/api/projects/@current/messaging_templates/{template_id}/send/"
_CODE_SEND_PATH = "/api/projects/@current/messaging/send/email/"
_UPLOAD_PATH = "/api/projects/@current/messaging_templates/by_key/{template_key}/"
_TEMPLATE_KEY = re.compile(r"^[a-zA-Z0-9_-]{1,400}$")
_CONTENT_HASH_HEADER = b"posthog-email-template-v1\n"
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


class EmailTag(TypedDict):
    """A name and value that PostHog adds to the email's engagement events as ``$email_tags``."""

    name: str
    value: str


class EmailAttachment(TypedDict):
    """A file sent with the email.

    Args:
        filename: The file name the recipient sees, such as ``invite.ics``.
        content: The file as bytes, or already Base64 encoded as a string.
        content_type: The MIME type. Without it, the type follows the file name.
        content_id: Shows the file inline: reference it in ``html`` as
            ``<img src="cid:logo">`` with ``content_id`` ``logo``.
    """

    filename: str
    content: Union[bytes, str]
    content_type: NotRequired[str]
    content_id: NotRequired[str]


class _RecipientArgs(TypedDict):
    to: str
    distinct_id: NotRequired[Optional[str]]
    idempotency_key: NotRequired[Optional[str]]


class TemplateEmailArgs(_RecipientArgs):
    """A send of an email template made in PostHog, named by its ID.

    Args:
        to: One recipient email address.
        template_id: The UUID of a template made transactional in PostHog.
        template_data: Values for the template's ``{{ variables.* }}``
            placeholders.
        distinct_id: The recipient's distinct ID in your product.
        idempotency_key: 1 to 255 visible ASCII characters.
    """

    template_id: Union[str, UUID]
    template_data: NotRequired[Optional[Mapping[str, Any]]]


class CodeEmailArgs(_RecipientArgs):
    """A send of email content that lives in your code, named by a key.

    Without ``template_data``, the content is rendered for this one recipient
    and sent as is. With it, the content is a template: PostHog fills in its
    ``{{ variables.* }}`` placeholders, and the client uploads the template
    whenever PostHog doesn't hold its content yet.

    A PostHog version that doesn't take a field yet, such as ``cc``, refuses
    the send with ``invalid_request`` and names the field in
    ``EmailError.field``. It never drops the field and sends the rest.

    Args:
        to: One recipient email address.
        template_key: Names the email, so its metrics, logs and status group
            under it. Letters, digits, ``-`` and ``_``, at most 400.
        subject: The subject line.
        html: The HTML body. Give ``html``, ``text`` or both.
        text: The plain-text body. Without it, PostHog derives it from
            ``html``. HTML with no text in it, such as a single image, goes out
            without a plain-text part.
        from_: A verified sender, such as ``"Acme <hello@example.com>"``.
        reply_to: One address or a list of addresses.
        cc: One address or a list of addresses that get a visible copy.
        bcc: One address or a list of addresses that get a hidden copy.
        headers: Extra email headers by name, such as ``X-Entity-Ref-ID``.
        tags: Names and values that PostHog adds to the email's engagement
            events.
        attachments: Files sent with the email.
        capture_engagement: Capture opens and clicks. Without it, PostHog's
            default applies, which is on.
        template_data: Values for the ``{{ variables.* }}`` placeholders.
        distinct_id: The recipient's distinct ID in your product.
        idempotency_key: 1 to 255 visible ASCII characters. Template sends
            and code sends share one set of keys: a key already used for
            another email, or for a ``template_id`` send, raises
            ``idempotency_key_reused``. A retry with the same key returns the
            first send's result, also when its workflow was paused since.
    """

    template_key: str
    subject: str
    html: NotRequired[str]
    text: NotRequired[str]
    from_: NotRequired[str]
    reply_to: NotRequired[Union[str, Sequence[str]]]
    cc: NotRequired[Union[str, Sequence[str]]]
    bcc: NotRequired[Union[str, Sequence[str]]]
    headers: NotRequired[Mapping[str, str]]
    tags: NotRequired[Sequence[EmailTag]]
    attachments: NotRequired[Sequence[EmailAttachment]]
    capture_engagement: NotRequired[bool]
    template_data: NotRequired[Mapping[str, Any]]


@dataclass(frozen=True)
class _PreparedUpload:
    path: str
    body: bytes


@dataclass(frozen=True)
class _PreparedSend:
    path: str
    body: bytes
    idempotency_key: str
    upload: Optional[_PreparedUpload] = None


def _prepare(args: Mapping[str, Any]) -> _PreparedSend:
    is_template_form = "template_id" in args
    if is_template_form == ("template_key" in args):
        raise TypeError("Pass either template_id or template_key.")
    form = TemplateEmailArgs if is_template_form else CodeEmailArgs
    unexpected = sorted(set(args) - set(form.__annotations__))
    if unexpected:
        raise TypeError(
            f"send() got arguments that the {'template' if is_template_form else 'code'} "
            f"form doesn't take: {', '.join(unexpected)}."
        )
    if "to" not in args:
        raise TypeError("send() is missing the argument to.")
    if is_template_form:
        return _prepare_send(
            args["to"],
            args["template_id"],
            args.get("template_data"),
            args.get("distinct_id"),
            args.get("idempotency_key"),
        )
    return _prepare_code_send(args)


def _prepare_code_send(args: Mapping[str, Any]) -> _PreparedSend:
    if "subject" not in args:
        raise TypeError("send() is missing the argument subject.")
    to, distinct_id, idempotency_key = _recipient(
        args["to"], args.get("distinct_id"), args.get("idempotency_key")
    )
    template_key = args["template_key"]
    if not isinstance(template_key, str) or not _TEMPLATE_KEY.match(template_key):
        raise ValueError(
            "template_key can only have letters, digits, hyphens (-) and underscores (_), at most 400."
        )
    content = {
        field: _nonempty_text(args, field)
        for field in ("subject", "html", "text")
        if field in args
    }
    if "html" not in content and "text" not in content:
        raise TypeError("send() needs html, text or both.")

    payload: dict[str, Any] = {"to": to}
    upload = None
    if "template_data" in args:
        template_data = args["template_data"]
        if not isinstance(template_data, Mapping):
            raise TypeError("template_data must be a mapping.")
        payload["template"] = {"key": template_key, "hash": _content_hash(content)}
        payload["message_data"] = clean(dict(template_data))
        upload = _PreparedUpload(
            path=_UPLOAD_PATH.format(template_key=template_key),
            body=_encode(
                {
                    "name": template_key,
                    **content,
                    "if_not_superseded": True,
                    "if_code_managed": True,
                }
            ),
        )
    else:
        payload.update({"template_key": template_key, **content})
    payload.update(_sender(args))
    if distinct_id is not None:
        payload["identifiers"] = {"distinct_id": distinct_id}
    return _PreparedSend(
        path=_CODE_SEND_PATH,
        body=_encode(payload),
        idempotency_key=idempotency_key,
        upload=upload,
    )


def _content_hash(content: Mapping[str, str]) -> str:
    """PostHog's content hash v1 of a template, so a send can say which content it holds."""
    encoded = bytearray(_CONTENT_HASH_HEADER)
    for field in ("subject", "preheader", "html", "text"):
        value = content.get(field, "").replace("\r\n", "\n").replace("\r", "\n")
        data = value.encode("utf-8")
        encoded += f"{len(data)}:".encode() + data + b"\n"
    return "v1:" + hashlib.sha256(encoded).hexdigest()


def _sender(args: Mapping[str, Any]) -> dict[str, Any]:
    sender: dict[str, Any] = {}
    if "from_" in args:
        sender["from"] = _nonempty_text(args, "from_")
    for field in ("reply_to", "cc", "bcc"):
        if field in args:
            sender[field] = _addresses(field, args[field])
    if "headers" in args:
        sender["headers"] = _headers_field(args["headers"])
    if "tags" in args:
        sender["tags"] = _tags(args["tags"])
    if "attachments" in args:
        sender["attachments"] = _attachments(args["attachments"])
    if "capture_engagement" in args:
        if not isinstance(args["capture_engagement"], bool):
            raise TypeError("capture_engagement must be True or False.")
        sender["capture_engagement"] = args["capture_engagement"]
    return sender


def _addresses(field: str, value: Any) -> list[str]:
    addresses = [value] if isinstance(value, str) else value
    if not isinstance(addresses, Sequence) or not all(
        isinstance(address, str) and address.strip() for address in addresses
    ):
        raise ValueError(f"{field} must be one address or a list of addresses.")
    return list(addresses)


def _headers_field(headers: Any) -> dict[str, str]:
    if not isinstance(headers, Mapping) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in headers.items()
    ):
        raise TypeError("headers must map header names to string values.")
    return dict(headers)


def _tags(tags: Any) -> list[dict[str, str]]:
    if (
        isinstance(tags, str)
        or not isinstance(tags, Sequence)
        or not all(_is_tag(tag) for tag in tags)
    ):
        raise TypeError("tags must be a list of {'name': str, 'value': str}.")
    return [{"name": tag["name"], "value": tag["value"]} for tag in tags]


def _attachments(attachments: Any) -> list[dict[str, str]]:
    if isinstance(attachments, (str, bytes)) or not isinstance(attachments, Sequence):
        raise TypeError("attachments must be a list of files.")
    return [_attachment(index, file) for index, file in enumerate(attachments)]


def _attachment(index: int, file: Any) -> dict[str, str]:
    name = f"attachments[{index}]"
    if not isinstance(file, Mapping) or not {"filename", "content"} <= set(file):
        raise TypeError(f"{name} needs a filename and its content.")
    unexpected = set(file) - set(EmailAttachment.__annotations__)
    if unexpected:
        raise TypeError(
            f"{name} has fields an attachment doesn't take: {', '.join(sorted(unexpected))}."
        )
    if not all(isinstance(file[field], str) for field in file if field != "content"):
        raise TypeError(
            f"{name} needs its filename, content_type and content_id as strings."
        )
    return {**file, "content": _base64_content(name, file["content"])}


def _base64_content(name: str, content: Any) -> str:
    if isinstance(content, bytes):
        return base64.b64encode(content).decode("ascii")
    if isinstance(content, str):
        return content
    raise TypeError(
        f"{name}.content must be bytes or a Base64 string. "
        "Convert a list of byte values with bytes(content)."
    )


def _is_tag(tag: Any) -> bool:
    return (
        isinstance(tag, Mapping)
        and set(tag) == {"name", "value"}
        and isinstance(tag["name"], str)
        and isinstance(tag["value"], str)
    )


def _nonempty_text(args: Mapping[str, Any], name: str) -> str:
    value = args[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string.")
    return value


def _encode(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, cls=DatetimeSerializer, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _prepare_send(
    to: str,
    template_id: Union[str, UUID],
    template_data: Optional[Mapping[str, Any]],
    distinct_id: Optional[str],
    idempotency_key: Optional[str],
) -> _PreparedSend:
    to, distinct_id, idempotency_key = _recipient(to, distinct_id, idempotency_key)
    try:
        template_uuid = (
            template_id if isinstance(template_id, UUID) else UUID(template_id)
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("template_id must be the template's UUID.") from None
    if template_data is not None and not isinstance(template_data, Mapping):
        raise TypeError("template_data must be a mapping.")

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


def _recipient(
    to: Any, distinct_id: Any, idempotency_key: Any
) -> tuple[str, Optional[str], str]:
    if not isinstance(to, str):
        raise TypeError("to must be one email address as a string.")
    if not to.strip():
        raise ValueError("to must not be empty.")
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
    return to, distinct_id, idempotency_key


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
            "concurrent_idempotent_requests"
            if code in _IN_FLIGHT_CODES
            else "validation_error"
        )
    if status == 422:
        return "invalid_idempotent_request"
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
    raise _api_error(status, fields, retry_after_header)


def _parse_upload(status: int, payload: Any, retry_after_header: Optional[str]) -> None:
    if not 200 <= status < 300:
        fields = payload if isinstance(payload, dict) else {}
        raise _api_error(status, fields, retry_after_header)


def _api_error(
    status: int, fields: Mapping[str, Any], retry_after_header: Optional[str]
) -> EmailError:
    code = _text(fields.get("code")) or "api_error"
    return EmailError(
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


def _is_outdated(error: EmailError) -> bool:
    return error.status_code == 409 and error.code == "template_outdated"


def _log_warnings(result: SendEmailResult) -> None:
    for warning in result.warnings:
        log.warning("PostHog email warning %s: %s", warning.code, warning.message)


def _headers(secret_key: str, idempotency_key: Optional[str]) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"posthog-python/{VERSION}",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


_ResultT = TypeVar("_ResultT")
_Parse = Callable[[int, Any, Optional[str]], _ResultT]


class _BearerAuth(requests.auth.AuthBase):
    def __init__(self, secret_key: str, idempotency_key: Optional[str]) -> None:
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
            raise _sdk_error("app_host_required", self._host_error)
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

    @overload
    def send(self, **kwargs: Unpack[TemplateEmailArgs]) -> SendEmailResult: ...

    @overload
    def send(self, **kwargs: Unpack[CodeEmailArgs]) -> SendEmailResult: ...

    def send(self, **kwargs: Any) -> SendEmailResult:
        """Queue one email, from a template in PostHog or from content in your code.

        Pass exactly one form. The template form names a template made in
        PostHog with ``template_id``. The code form names the email with
        ``template_key`` and passes ``subject`` and ``html``, ``text`` or both:
        see :class:`CodeEmailArgs`.

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
            emails.send(
                to="user@example.com",
                template_key="password-reset",
                subject="Reset your password",
                html=rendered_html,
                distinct_id="user_123",
            )
            ```
        """
        prepared = _prepare(kwargs)
        if self._is_disabled():
            log.debug("PostHog client is disabled, so no email was sent.")
            return SendEmailResult(id=None)
        finish = self._start_send()
        try:
            return self._deliver(prepared)
        finally:
            finish()

    def _deliver(self, prepared: _PreparedSend) -> SendEmailResult:
        secret_key = self._secret_key()
        auth = _BearerAuth(secret_key, prepared.idempotency_key)
        try:
            result = self._request(
                "POST", prepared.path, prepared.body, auth, _parse_response
            )
        except EmailError as error:
            if prepared.upload is None or not _is_outdated(error):
                raise
            self._request(
                "PUT",
                prepared.upload.path,
                prepared.upload.body,
                _BearerAuth(secret_key, None),
                _parse_upload,
            )
            result = self._request(
                "POST", prepared.path, prepared.body, auth, _parse_response
            )
        _log_warnings(result)
        return result

    def _request(
        self,
        method: str,
        path: str,
        body: bytes,
        auth: _BearerAuth,
        parse: _Parse[_ResultT],
    ) -> _ResultT:
        session = self._get_session()
        url = self.host + path
        for attempt in range(self.max_retries + 1):
            try:
                response = session.request(
                    method,
                    url,
                    data=body,
                    auth=auth,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                try:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    return parse(
                        response.status_code,
                        payload,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    response.close()
            except requests.exceptions.RequestException:
                error = _sdk_error("network_error", "The request to PostHog failed.")
            except EmailError as exc:
                error = exc
            delay = _retry_delay(error, attempt, self.max_retries)
            if delay is None:
                raise error from None
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

    @overload
    async def send(self, **kwargs: Unpack[TemplateEmailArgs]) -> SendEmailResult: ...

    @overload
    async def send(self, **kwargs: Unpack[CodeEmailArgs]) -> SendEmailResult: ...

    async def send(self, **kwargs: Any) -> SendEmailResult:
        """Queue one email, from a template in PostHog or from content in your code.

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
        prepared = _prepare(kwargs)
        if self._is_disabled():
            log.debug("PostHog client is disabled, so no email was sent.")
            return SendEmailResult(id=None)
        finish = self._start_send()
        try:
            return await self._deliver(prepared)
        finally:
            finish()

    async def _deliver(self, prepared: _PreparedSend) -> SendEmailResult:
        secret_key = self._secret_key()
        headers = _headers(secret_key, prepared.idempotency_key)
        try:
            result = await self._request(
                "POST", prepared.path, prepared.body, headers, _parse_response
            )
        except EmailError as error:
            if prepared.upload is None or not _is_outdated(error):
                raise
            await self._request(
                "PUT",
                prepared.upload.path,
                prepared.upload.body,
                _headers(secret_key, None),
                _parse_upload,
            )
            result = await self._request(
                "POST", prepared.path, prepared.body, headers, _parse_response
            )
        _log_warnings(result)
        return result

    async def _request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        parse: _Parse[_ResultT],
    ) -> _ResultT:
        httpx, client = self._get_client()
        url = self.host + path
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.request(
                    method, url, content=body, headers=headers
                )
                try:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    return parse(
                        response.status_code,
                        payload,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    await response.aclose()
            except httpx.RequestError:
                error = _sdk_error("network_error", "The request to PostHog failed.")
            except EmailError as exc:
                error = exc
            delay = _retry_delay(error, attempt, self.max_retries)
            if delay is None:
                raise error from None
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
