"""Transactional email, sent from an email template in PostHog or from content in your code.

Setup:

1. In PostHog, open the email template under Workflows, make it transactional,
   and copy its ID. ``template_id`` is that UUID.
2. Create a project secret API key (``phs_...``) with the "Send transactional
   email" preset (``transactional_email:write``) in your project settings. Add
   ``transactional_email:read`` too if you read email statuses with a key that
   can't send. Keys made with ``hog_flow:write`` still work. The project API key
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

Moving from another email provider: set ``migrated_from``, such as
``"resend"``, on the client or on :class:`Emails`, so PostHog knows where your
sends came from.

Attachments: a code send takes ``attachments``, at most 10 files and 40 MB
per message after Base64 encoding, about 29 MB of files. The client checks
both before any request. It then asks PostHog which files it lacks, uploads
each of those to the URL PostHog answers, without the secret key, and sends the
email naming each file by its SHA-256. A file the project already sent uploads
nothing, and the same bytes in two attachments upload once. An upload retries
like a send when the storage is unreachable or answers a passing fault. A
failed upload sends no email. The upload URL's host is not the app host, so a
network that limits outbound requests must allow it.

Errors: a send that is not queued raises :class:`EmailError`. Branch on
``code``. The ones a setup usually hits:

- ``missing_secret_key``: the client has no ``secret_key``.
- ``missing_app_host``: the client's ``host`` is not PostHog Cloud. Set ``app_host``.
- ``authentication_failed`` (401) or ``permission_denied`` (403): the key is
  wrong, or it lacks ``transactional_email:write``.
- ``not_found`` (404): the template ID is wrong, or transactional email is not
  enabled for the project.
- ``client_disabled``: the client is disabled, so it sends nothing.
- ``client_closed``: the client is shut down.

A send with attachments can also raise:

- ``too_many_attachments`` or ``message_too_large``: the email is over the
  limits above. The client raises these before any request.
- ``invalid_attachment``: the client can't send a file, such as one without
  content.
- ``upload_failed``: PostHog's file storage refused an upload. ``message``
  and ``why`` carry its answer, and ``field`` names the attachment.
- ``network_error`` with ``field`` naming an attachment: the file storage
  didn't answer, such as when a firewall blocks its host.
- ``upload_quota_exceeded`` (429): the project uploaded its daily limit of
  file bytes. Files it already sent don't count.
- ``field_not_accepted``: the project can't send files yet.
- ``not_found`` (404) with ``field`` ``attachments``: this PostHog version
  doesn't take files yet, or sending email from code is off for the project.
  ``message`` carries PostHog's answer, and no email was sent.

Status: ``emails.get(message_id)`` reads what happened to a sent email, for
30 days after the send, as an :class:`EmailStatus`. A key with only
``transactional_email:read`` can read statuses but not send. ``get`` raises
``not_found`` (404) for an unknown ID, an email older than 30 days, or for a
few seconds after the send.

Cleanup: ``shutdown()`` on the client closes its email connections. Close a
standalone :class:`Emails` with ``close()`` or a ``with`` block.

Testing without network calls: pass your own transport, ``session`` to
:class:`Emails` or ``http_client`` to :class:`AsyncEmails`, with a fake adapter
or ``httpx.MockTransport``. Any library that mocks ``requests`` or ``httpx``
works too. A disabled client sends nothing and raises ``client_disabled``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
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
from datetime import datetime
from types import TracebackType
from typing import (
    Any,
    Awaitable,
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
    "EmailStatus",
    "EmailWarning",
    "Emails",
    "SendEmailResult",
    "TemplateEmailArgs",
]

log = logging.getLogger("posthog")

_SEND_PATH = "/api/projects/@current/messaging/send/email/"
_UPLOADS_PATH = "/api/projects/@current/messaging/attachments/uploads/"
_STATUS_PATH = "/api/projects/@current/messaging/emails/{message_id}/"
_TEMPLATE_KEY = re.compile(r"[a-zA-Z0-9_-]{1,400}")
_MAX_RETRY_DELAY = 30.0
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,255}$")
_HEADER_TOKEN = re.compile(r"[\x21-\x7e]+")
_IN_FLIGHT_CODES = frozenset({"idempotency_key_in_flight"})
_QUOTA_CODES = frozenset({"quota_limited"})
_DAILY_QUOTA_CODES = frozenset({"upload_quota_exceeded"})
_MAX_ATTACHMENTS = 10
_STORAGE_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_MESSAGE_BYTES = 40_000_000
_HEADERS_AND_MIME_BOUNDARIES_BYTES = 64 * 1024
_MESSAGE_LIMIT = "40 MB after Base64 encoding, about 29 MB of files"
_STORAGE_ERROR = re.compile(r"<(Code|Message)>([^<]{1,500})</\1>")


class EmailError(Exception):
    """PostHog did not queue the email, or the client could not tell whether it did.

    Branch on ``code``, PostHog's stable error code, or on ``name``, a coarse
    category of the failure. The message never contains the secret key or the
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
    """The queued email. ``id`` is its message ID."""

    id: str
    replayed: bool = False
    warnings: tuple[EmailWarning, ...] = ()


class EmailAttachment(TypedDict):
    """A file sent with the email.

    Args:
        filename: The file name the recipient sees, such as ``invite.ics``.
        content: The file as ``bytes``, ``bytearray`` or ``memoryview``, or
            Base64 encoded as a string. PostHog can't fetch a file from a path
            or URL.
        content_type: The MIME type. Without it, the type follows the file name.
        content_id: Shows the file inline: reference it in ``html`` as
            ``<img src="cid:logo">`` with ``content_id`` ``logo``.
    """

    filename: str
    content: Union[bytes, bytearray, memoryview, str]
    content_type: NotRequired[str]
    content_id: NotRequired[str]


class _RecipientArgs(TypedDict):
    to: str
    distinct_id: NotRequired[Optional[str]]
    idempotency_key: NotRequired[Optional[str]]
    max_retries: NotRequired[Optional[int]]


class TemplateEmailArgs(_RecipientArgs):
    """A send of an email template made in PostHog, named by its ID.

    Args:
        to: One recipient email address.
        template_id: The UUID of a template made transactional in PostHog.
        template_data: Values for the template's ``{{ variables.* }}``
            placeholders.
        distinct_id: The recipient's distinct ID in your product.
        idempotency_key: 1 to 255 visible ASCII characters.
        max_retries: Overrides the client's maximum number of retries for this
            send. ``0`` turns retries off.
    """

    template_id: Union[str, UUID]
    template_data: NotRequired[Optional[Mapping[str, Any]]]


class CodeEmailArgs(_RecipientArgs):
    """A send of email content that your code rendered for one recipient, named by a key.

    The content goes out as is. To send a React email, render it to HTML in
    your app first.

    An optional field passed as ``None`` counts as left out.

    A PostHog version that doesn't take a field yet, such as ``cc``, refuses
    the send with an error that names the field in ``EmailError.field``. It
    never drops the field and sends the rest.

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
        reply_to: One address or a list of addresses. An empty list sends no reply-to.
        cc: One address or a list of addresses that get a visible copy, at
            most 10.
        bcc: One address or a list of addresses that get a hidden copy, at
            most 10.
        attachments: Files sent with the email: at most 10 files and 40 MB
            per message after Base64 encoding, about 29 MB of files. The
            client checks both before any request. It uploads each file that
            PostHog doesn't hold yet, then sends the email. A file the client
            can't send raises :class:`EmailError` with ``code``
            ``invalid_attachment``.
        capture_engagement: Capture the email's opens and clicks as events of
            the recipient. Defaults to ``True``. A project whose admin turned
            engagement capture off captures nothing, whatever the send asks. A
            template made in PostHog follows the project's setting, so the
            template form doesn't take it.
        distinct_id: The recipient's distinct ID in your product.
        idempotency_key: 1 to 255 visible ASCII characters. Template sends
            and code sends share one set of keys: a key already used for
            another email, or for a ``template_id`` send, raises
            ``idempotency_key_reused``. A retry with the same key returns the
            first send's result, also when its workflow was paused since.
        max_retries: Overrides the client's maximum number of retries for this
            send. ``0`` turns retries off.
    """

    template_key: str
    subject: str
    html: NotRequired[Optional[str]]
    text: NotRequired[Optional[str]]
    from_: NotRequired[Optional[str]]
    reply_to: NotRequired[Optional[Union[str, Sequence[str]]]]
    cc: NotRequired[Optional[Union[str, Sequence[str]]]]
    bcc: NotRequired[Optional[Union[str, Sequence[str]]]]
    attachments: NotRequired[Optional[Sequence[EmailAttachment]]]
    capture_engagement: NotRequired[Optional[bool]]


_ATTACHMENT_FIELDS = frozenset(EmailAttachment.__annotations__)
_OPTIONAL_CODE_FIELDS = frozenset(
    {
        "html",
        "text",
        "from_",
        "reply_to",
        "cc",
        "bcc",
        "attachments",
        "capture_engagement",
    }
)


@dataclass(frozen=True)
class EmailStatus:
    """What happened to an email that ``send()`` queued.

    ``status`` is one of:

    - ``queued``: the email waits to go out.
    - ``sent``: PostHog handed the email to the email provider.
    - ``failed``: the email was not sent, and ``reason`` says why.
    - ``delivered``: the recipient's mail server accepted the email.
    - ``bounced`` and ``complained``: the recipient's mail provider reported
      them after delivery.

    Later SDK versions can see values this list does not have yet.
    ``reason`` says why a failed email was not sent, such as
    ``provider_error`` or ``recipient_suppressed``, or gives the bounce type of
    a bounced email: ``hard``, ``soft`` or ``unknown``. It is ``None`` otherwise.
    """

    id: str
    status: str
    created_at: datetime
    status_at: datetime
    reason: Optional[str] = None


@dataclass(frozen=True)
class _File:
    sha256: str
    content: bytes
    first_attachment_index: int

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class _Upload:
    file: _File
    url: str
    method: str
    headers: dict[str, str]


@dataclass(frozen=True)
class _PreparedSend:
    path: str
    body: bytes
    idempotency_key: str
    max_retries: Optional[int] = None
    files: tuple[_File, ...] = ()


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
            args.get("max_retries"),
        )
    return _prepare_code_send(args)


def _prepare_code_send(args: Mapping[str, Any]) -> _PreparedSend:
    args = {
        name: value
        for name, value in args.items()
        if value is not None or name not in _OPTIONAL_CODE_FIELDS
    }
    if "subject" not in args:
        raise TypeError("send() is missing the argument subject.")
    to, distinct_id, idempotency_key = _recipient(
        args["to"], args.get("distinct_id"), args.get("idempotency_key")
    )
    max_retries = _retry_limit(args.get("max_retries"))
    template_key = args["template_key"]
    if not isinstance(template_key, str) or not _TEMPLATE_KEY.fullmatch(template_key):
        raise ValueError(
            "template_key can only have letters, digits, hyphens (-) and underscores (_), at most 400."
        )
    content = {"subject": _nonempty_text(args, "subject")}
    content.update(
        {field: _string(args, field) for field in ("html", "text") if field in args}
    )
    if "html" not in content and "text" not in content:
        raise TypeError("send() needs html, text or both.")
    if not (content.get("html") or content.get("text")):
        raise ValueError("html or text must be a nonempty string.")

    references, files = _attachments(args.get("attachments", []), content)

    payload: dict[str, Any] = {"to": to, "template_key": template_key, **content}
    if distinct_id is not None:
        payload["distinct_id"] = distinct_id
    payload.update(_sender(args))
    if references:
        payload["attachments"] = references
    return _PreparedSend(
        path=_SEND_PATH,
        body=_encode(payload),
        idempotency_key=idempotency_key,
        max_retries=max_retries,
        files=_each_file_once(files),
    )


def _sender(args: Mapping[str, Any]) -> dict[str, Any]:
    sender: dict[str, Any] = {}
    if "from_" in args:
        sender["from"] = _nonempty_text(args, "from_")
    for field in ("reply_to", "cc", "bcc"):
        addresses = _addresses(field, args[field]) if field in args else []
        if addresses:
            sender[field] = addresses
    capture_engagement = args.get("capture_engagement", True)
    if not isinstance(capture_engagement, bool):
        raise TypeError("capture_engagement must be True or False.")
    sender["capture_engagement"] = capture_engagement
    return sender


def _addresses(field: str, value: Any) -> list[str]:
    addresses = [value] if isinstance(value, str) else value
    if not isinstance(addresses, Sequence) or not all(
        isinstance(address, str) for address in addresses
    ):
        raise TypeError(f"{field} must be one address or a list of addresses.")
    if not all(address.strip() for address in addresses):
        raise ValueError(f"{field} must not hold a blank address.")
    return list(addresses)


def _each_file_once(files: Sequence[_File]) -> tuple[_File, ...]:
    by_hash: dict[str, _File] = {}
    for file in files:
        by_hash.setdefault(file.sha256, file)
    return tuple(by_hash.values())


def _invalid_attachment(message: str, field: str) -> EmailError:
    return EmailError(
        name="validation_error",
        code="invalid_attachment",
        message=message,
        field=field,
    )


def _attachments(
    attachments: Any, content: Mapping[str, str]
) -> tuple[list[dict[str, str]], tuple[_File, ...]]:
    if isinstance(
        attachments, (str, bytes, bytearray, memoryview, Mapping)
    ) or not isinstance(attachments, Sequence):
        raise _invalid_attachment("attachments must be a list of files.", "attachments")
    if len(attachments) > _MAX_ATTACHMENTS:
        raise _over_the_limit(
            "too_many_attachments",
            f"An email carries at most {_MAX_ATTACHMENTS} files, so no email was sent.",
            fix=f"Send at most {_MAX_ATTACHMENTS} files, and link to any others "
            "from the email body.",
        )
    checked = [
        _checked_attachment(index, file) for index, file in enumerate(attachments)
    ]
    _check_message_size(
        [
            _content_size(f"attachments__{index}", file.get("content"))
            for index, file in enumerate(checked)
        ],
        content,
    )
    prepared = [_attachment(index, file) for index, file in enumerate(checked)]
    return [reference for reference, _ in prepared], tuple(file for _, file in prepared)


def _checked_attachment(index: int, file: Any) -> Mapping[str, Any]:
    field = f"attachments__{index}"
    if not isinstance(file, Mapping):
        raise _invalid_attachment(
            "An attachment is a mapping with filename and content. "
            "Pass the file's bytes as its content.",
            field,
        )
    if "path" in file:
        raise _invalid_attachment(
            "PostHog can't fetch an attachment from a path or URL. "
            "Read the file and pass its bytes as content instead.",
            f"{field}__path",
        )
    unknown = [name for name in file if name not in _ATTACHMENT_FIELDS]
    if unknown:
        raise _invalid_attachment(
            "An attachment takes filename, content, content_type and content_id, "
            f"not {unknown[0]}.",
            f"{field}__{unknown[0]}",
        )
    if not isinstance(file.get("filename"), str) or not file["filename"]:
        raise _invalid_attachment(
            "An attachment needs a filename, such as 'invoice.pdf'.",
            f"{field}__filename",
        )
    for name in ("content_type", "content_id"):
        if name in file and not isinstance(file[name], str):
            raise _invalid_attachment(
                f"An attachment's {name} must be a string.", f"{field}__{name}"
            )
    return file


def _content_size(field: str, content: Any) -> int:
    if isinstance(content, (bytes, bytearray, memoryview)):
        size = memoryview(content).nbytes
    elif isinstance(content, str):
        size = len(content.rstrip("=")) * 3 // 4
    else:
        raise _invalid_attachment(
            "An attachment needs content: bytes or a Base64 string. "
            "Convert a list of byte values with bytes(content).",
            f"{field}__content",
        )
    if not size:
        raise _invalid_attachment(
            "An attachment's content is empty, and an empty file gives the "
            "recipient nothing to open. Leave the file out.",
            f"{field}__content",
        )
    return size


def _attachment(index: int, file: Mapping[str, Any]) -> tuple[dict[str, str], _File]:
    content = _file_bytes(f"attachments__{index}", file["content"])
    sha256 = hashlib.sha256(content).hexdigest()
    reference = {"sha256": sha256, "filename": file["filename"]}
    for name in ("content_type", "content_id"):
        if name in file:
            reference[name] = file[name]
    return reference, _File(
        sha256=sha256, content=content, first_attachment_index=index
    )


def _file_bytes(field: str, content: Union[bytes, bytearray, memoryview, str]) -> bytes:
    if not isinstance(content, str):
        return bytes(content)
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid_attachment(
            "An attachment's content string must be Base64. "
            "Pass the file's bytes instead, such as text.encode() for text.",
            f"{field}__content",
        ) from None


def _check_message_size(file_sizes: Sequence[int], content: Mapping[str, str]) -> None:
    body_sizes = [
        len(body.encode("utf-8"))
        for body in (content.get("html"), content.get("text"))
        if body
    ]
    if (
        file_sizes
        and _estimated_message_bytes([*file_sizes, *body_sizes]) > _MAX_MESSAGE_BYTES
    ):
        raise _over_the_limit(
            "message_too_large",
            f"These files make the email larger than {_MESSAGE_LIMIT}, so no email was sent.",
            fix="Send fewer or smaller files, or link to large files from the email body.",
        )


def _over_the_limit(code: str, message: str, *, fix: str) -> EmailError:
    return EmailError(
        name="validation_error",
        code=code,
        message=message,
        why=f"An email carries at most {_MAX_ATTACHMENTS} files and {_MESSAGE_LIMIT}.",
        fix=fix,
        field="attachments",
    )


def _estimated_message_bytes(sizes: Sequence[int]) -> int:
    return _HEADERS_AND_MIME_BOUNDARIES_BYTES + sum(
        _base64_transfer_bytes(size) for size in sizes
    )


def _base64_transfer_bytes(size: int) -> int:
    return _ceil_div(_ceil_div(size, 3) * 4 * 78, 76)


def _ceil_div(dividend: int, divisor: int) -> int:
    return -(-dividend // divisor)


def _string(args: Mapping[str, Any], name: str) -> str:
    value = args[name]
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    return value


def _nonempty_text(args: Mapping[str, Any], name: str) -> str:
    value = _string(args, name)
    if not value:
        raise ValueError(f"{name} must not be empty.")
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
    max_retries: Optional[int] = None,
) -> _PreparedSend:
    to, distinct_id, idempotency_key = _recipient(to, distinct_id, idempotency_key)
    max_retries = _retry_limit(max_retries)
    try:
        template_uuid = (
            template_id if isinstance(template_id, UUID) else UUID(template_id)
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("template_id must be the template's UUID.") from None
    if template_data is not None and not isinstance(template_data, Mapping):
        raise TypeError("template_data must be a mapping.")

    payload: dict[str, Any] = {
        "template_id": str(template_uuid),
        "to": to,
        "message_data": clean(dict(template_data or {})),
    }
    if distinct_id is not None:
        payload["distinct_id"] = distinct_id
    return _PreparedSend(
        path=_SEND_PATH,
        body=json.dumps(payload, cls=DatetimeSerializer).encode("utf-8"),
        idempotency_key=idempotency_key,
        max_retries=max_retries,
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


def _retry_limit(max_retries: Any) -> Optional[int]:
    if max_retries is not None and (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be a nonnegative integer.")
    return max_retries


def _status_path(message_id: Union[str, UUID]) -> str:
    try:
        message_uuid = message_id if isinstance(message_id, UUID) else UUID(message_id)
    except (TypeError, ValueError, AttributeError):
        raise ValueError(
            "message_id must be the message ID that send() returned."
        ) from None
    return _STATUS_PATH.format(message_id=message_uuid)


def _validate_options(
    timeout: float, max_retries: int, migrated_from: Optional[str]
) -> None:
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
    if migrated_from is not None and not (
        isinstance(migrated_from, str) and _HEADER_TOKEN.fullmatch(migrated_from)
    ):
        raise ValueError(
            "migrated_from must be visible ASCII characters, such as 'resend'."
        )


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
        if code in _DAILY_QUOTA_CODES:
            return "daily_quota_exceeded"
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


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_response(
    status: int, body: bytes, retry_after_header: Optional[str]
) -> SendEmailResult:
    fields = _json_object(body)
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
    raise _response_error(fields, status, retry_after_header)


def _timestamp(value: Any) -> Optional[datetime]:
    text = _text(value)
    if text is None:
        return None
    try:
        # fromisoformat reads a trailing Z only from Python 3.11.
        return datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None


def _parse_status_response(
    status: int, body: bytes, retry_after_header: Optional[str]
) -> EmailStatus:
    fields = _json_object(body)
    if not 200 <= status < 300:
        raise _response_error(fields, status, retry_after_header)
    message_id = _text(fields.get("id"))
    delivery_status = _text(fields.get("status"))
    created_at = _timestamp(fields.get("created_at"))
    status_at = _timestamp(fields.get("status_at"))
    if (
        message_id is None
        or delivery_status is None
        or created_at is None
        or status_at is None
    ):
        raise _sdk_error(
            "api_error", "PostHog answered without an email status.", status
        )
    return EmailStatus(
        id=message_id,
        status=delivery_status,
        created_at=created_at,
        status_at=status_at,
        reason=_text(fields.get("reason")),
    )


def _pending_uploads_parser(files: Sequence[_File]) -> _Parse[list[_Upload]]:
    def parse(
        status: int, body: bytes, retry_after_header: Optional[str]
    ) -> list[_Upload]:
        fields = _json_object(body)
        if status == 404:
            raise _upload_route_not_found(fields, retry_after_header)
        if not 200 <= status < 300:
            raise _response_error(fields, status, retry_after_header)
        slots = fields.get("files")
        if not isinstance(slots, list) or len(slots) != len(files):
            raise _no_upload_slots(status)
        uploads = (
            _pending_upload(slot, file, status) for slot, file in zip(slots, files)
        )
        return [upload for upload in uploads if upload is not None]

    return parse


def _pending_upload(slot: Any, file: _File, status: int) -> Optional[_Upload]:
    if not isinstance(slot, dict) or slot.get("sha256") != file.sha256:
        raise _no_upload_slots(status)
    if slot.get("status") == "present":
        return None
    url, method, headers = (
        _text(slot.get("url")),
        _text(slot.get("method", "PUT")),
        slot.get("headers"),
    )
    if (
        slot.get("status") != "upload"
        or url is None
        or method is None
        or not isinstance(headers, dict)
        or not all(isinstance(value, str) for value in headers.values())
    ):
        raise _no_upload_slots(status)
    return _Upload(file=file, url=url, method=method, headers=headers)


def _upload_route_not_found(
    fields: Mapping[str, Any], retry_after_header: Optional[str]
) -> EmailError:
    unanswered = {
        "code": "not_found",
        "why": "This PostHog version doesn't take attachments yet, or sending "
        "email from code is off for the project.",
        "fix": "Send the email without attachments, or check that the project "
        "can send email from code.",
    }
    answered = {key: value for key, value in fields.items() if value is not None}
    return _response_error(
        {**unanswered, **answered, "attr": "attachments"}, 404, retry_after_header
    )


def _no_upload_slots(status: int) -> EmailError:
    return _sdk_error(
        "api_error", "PostHog answered without an upload slot for each file.", status
    )


def _upload_parser(index: int) -> _Parse[None]:
    def parse(status: int, body: bytes, retry_after_header: Optional[str]) -> None:
        if not 200 <= status < 300:
            raise _upload_error(index, status, body, retry_after_header)

    return parse


def _upload_error(
    index: int, status: int, body: bytes, retry_after_header: Optional[str]
) -> EmailError:
    answer = dict(_STORAGE_ERROR.findall(body.decode("utf-8", "replace")))
    storage_answer = " ".join(filter(None, (f"HTTP {status}", answer.get("Code"))))
    return EmailError(
        name=_storage_error_name(status),
        code="upload_failed",
        message=f"PostHog's file storage refused the upload of attachments[{index}] "
        f"with {storage_answer}, so no email was sent.",
        status_code=status,
        why=answer.get("Message"),
        fix="Send the email again. If the upload keeps failing, check that the "
        "file's bytes don't change while it is sent.",
        field=f"attachments__{index}",
        retry_after=_retry_after(retry_after_header),
    )


def _storage_error_name(status: int) -> str:
    if status == 429:
        return "rate_limit_exceeded"
    if status in _STORAGE_RETRY_STATUSES:
        return "service_unavailable"
    return "application_error"


def _response_error(
    fields: Mapping[str, Any], status: int, retry_after_header: Optional[str]
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
    # A send carries an idempotency key, so a retry can't queue a second email. A status read changes nothing.
    if error.status_code is None:
        return error.code == "network_error"
    if error.code == "upload_failed":
        return error.status_code in _STORAGE_RETRY_STATUSES
    if error.status_code == 409:
        return error.code in _IN_FLIGHT_CODES
    if error.status_code == 429:
        return error.code not in _QUOTA_CODES | _DAILY_QUOTA_CODES
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


def _headers(
    secret_key: str,
    migrated_from: Optional[str],
    *,
    has_body: bool = False,
    idempotency_key: Optional[str] = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Accept": "application/json",
        "User-Agent": f"posthog-python/{VERSION}",
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if migrated_from is not None:
        headers["X-PostHog-Migrated-From"] = migrated_from
    return headers


_SEND_NOT_DONE = "no email was sent"
_GET_NOT_DONE = "no email status was read"
_SEND_KEY_NEEDED = "Sending email needs secret_key, a project secret API key with the transactional_email:write scope."
_GET_KEY_NEEDED = (
    "Reading an email status needs secret_key, a project secret API key with the "
    "transactional_email:read or transactional_email:write scope."
)
_ResultT = TypeVar("_ResultT")
_Parse = Callable[[int, bytes, Optional[str]], _ResultT]


class _BearerAuth(requests.auth.AuthBase):
    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        # Setting auth explicitly stops requests from substituting .netrc credentials.
        request.headers.update(self._headers)
        return request


def _storage_unreachable(index: int, exc: Exception) -> EmailError:
    return EmailError(
        name="service_unavailable",
        code="network_error",
        message=f"PostHog's file storage didn't answer the upload of attachments[{index}] "
        f"({type(exc).__name__}), so no email was sent.",
        fix="Send the email again. If it keeps failing, check that your network "
        "lets the app reach PostHog's file storage.",
        field=f"attachments__{index}",
    )


def _files_body(files: Sequence[_File]) -> bytes:
    return _encode(
        {"files": [{"sha256": file.sha256, "size": file.size} for file in files]}
    )


class _EmailsBase:
    def __init__(
        self,
        secret_key: Optional[str],
        *,
        host: Optional[str],
        timeout: float,
        max_retries: int,
        disabled: bool,
        migrated_from: Optional[str],
    ) -> None:
        _validate_options(timeout, max_retries, migrated_from)
        self.secret_key = secret_key.strip() if isinstance(secret_key, str) else None
        self.host = determine_app_host(host)
        self.timeout = timeout
        self.max_retries = max_retries
        self.disabled = disabled
        self.migrated_from = migrated_from
        self._parent_disabled: Callable[[], bool] = lambda: False
        self._admit: Callable[[], Optional[Callable[[], None]]] = lambda: _no_op
        self._host_error: Optional[str] = None

    def _is_disabled(self) -> bool:
        return self.disabled or self._parent_disabled()

    def _start(self, not_done: str) -> Callable[[], None]:
        if self._is_disabled():
            raise _sdk_error(
                "client_disabled", f"The PostHog client is disabled, so {not_done}."
            )
        finish = self._admit()
        if finish is None:
            raise _sdk_error(
                "client_closed", f"The PostHog client is shut down, so {not_done}."
            )
        return finish

    def _secret_key(self, key_needed: str) -> str:
        if self._host_error:
            raise _sdk_error("missing_app_host", self._host_error)
        if not self.secret_key:
            raise _sdk_error("missing_secret_key", key_needed)
        return self.secret_key


class Emails(_EmailsBase):
    """Sends transactional email from PostHog email templates or from content in your code.

    ``host`` is your PostHog app host. PostHog Cloud ingestion hosts map to the
    app host, so ``us.i.posthog.com`` sends to ``us.posthog.com``. Any other
    host is used as given.

    Each send retries network errors, rate limits and an unavailable service up
    to ``max_retries`` times, with the same idempotency key. Set
    ``max_retries=0`` when a job runner already retries the send. ``timeout``
    is in seconds, per request. Pass ``session`` to send through your own
    ``requests.Session``, for example a test double. ``migrated_from``, such
    as ``"resend"``, tells PostHog which provider your sends came from.
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
        migrated_from: Optional[str] = None,
    ) -> None:
        super().__init__(
            secret_key,
            host=host,
            timeout=timeout,
            max_retries=max_retries,
            disabled=disabled,
            migrated_from=migrated_from,
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
            max_retries: Overrides the client's maximum number of retries for
                this send. ``0`` turns retries off.

        Returns:
            The queued email. A result means PostHog queued the email, not that
            it was delivered.

        Raises:
            EmailError: PostHog did not queue the email, or the client could
                not tell whether it did.
                With attachments, ``code`` is ``too_many_attachments``,
                ``message_too_large`` or ``invalid_attachment`` before any
                request, ``upload_failed`` when the storage refused an upload,
                and ``network_error`` with ``field`` naming the attachment
                when the storage didn't answer.
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
        finish = self._start(_SEND_NOT_DONE)
        try:
            result = self._deliver(prepared, self._secret_key(_SEND_KEY_NEEDED))
        finally:
            finish()
        _log_warnings(result)
        return result

    def _deliver(self, prepared: _PreparedSend, secret_key: str) -> SendEmailResult:
        if prepared.files:
            self._upload(prepared, secret_key)
        headers = _headers(
            secret_key,
            self.migrated_from,
            has_body=True,
            idempotency_key=prepared.idempotency_key,
        )
        return self._call(
            self._posthog_request("POST", prepared.path, prepared.body, headers),
            _parse_response,
            prepared.max_retries,
        )

    def _upload(self, prepared: _PreparedSend, secret_key: str) -> None:
        headers = _headers(secret_key, self.migrated_from, has_body=True)
        uploads = self._call(
            self._posthog_request(
                "POST", _UPLOADS_PATH, _files_body(prepared.files), headers
            ),
            _pending_uploads_parser(prepared.files),
            prepared.max_retries,
        )
        for upload in uploads:
            self._call(
                self._storage_request(upload),
                _upload_parser(upload.file.first_attachment_index),
                prepared.max_retries,
            )

    def get(self, message_id: Union[str, UUID]) -> EmailStatus:
        """Read what happened to an email that :meth:`send` queued.

        Args:
            message_id: The ``id`` of the :class:`SendEmailResult` that
                :meth:`send` returned.

        Returns:
            The email's latest status, for 30 days after the send.

        Raises:
            EmailError: PostHog did not answer with a status. ``code`` is
                ``not_found`` for an unknown ID, an email older than 30 days,
                or for a few seconds after the send.
            ValueError: ``message_id`` is not a message ID.

        Examples:
            ```python
            result = emails.send(to="user@example.com", template_id="...")
            status = emails.get(result.id)
            if status.status == "bounced":
                print(status.reason)
            ```
        """
        path = _status_path(message_id)
        finish = self._start(_GET_NOT_DONE)
        try:
            headers = _headers(self._secret_key(_GET_KEY_NEEDED), self.migrated_from)
            return self._call(
                self._posthog_request("GET", path, None, headers),
                _parse_status_response,
            )
        finally:
            finish()

    def _posthog_request(
        self, method: str, path: str, body: Optional[bytes], headers: dict[str, str]
    ) -> Callable[[], requests.Response]:
        session, auth = self._get_session(), _BearerAuth(headers)
        return lambda: session.request(
            method,
            self.host + path,
            data=body,
            auth=auth,
            timeout=self.timeout,
            allow_redirects=False,
        )

    def _storage_request(self, upload: _Upload) -> Callable[[], requests.Response]:
        session = self._get_session()
        request = requests.Request(
            upload.method, upload.url, data=upload.file.content, headers=upload.headers
        ).prepare()
        transport = session.merge_environment_settings(upload.url, {}, None, None, None)

        def send() -> requests.Response:
            try:
                return session.send(
                    request, timeout=self.timeout, allow_redirects=False, **transport
                )
            except requests.exceptions.RequestException as exc:
                raise _storage_unreachable(
                    upload.file.first_attachment_index, exc
                ) from None

        return send

    def _call(
        self,
        send: Callable[[], requests.Response],
        parse: _Parse[_ResultT],
        max_retries: Optional[int] = None,
    ) -> _ResultT:
        retries = self.max_retries if max_retries is None else max_retries
        for attempt in range(retries + 1):
            try:
                response = send()
                try:
                    return parse(
                        response.status_code,
                        response.content,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    response.close()
            except requests.exceptions.RequestException as exc:
                error = _sdk_error("network_error", "The request to PostHog failed.")
                error.__cause__ = exc
            except EmailError as exc:
                error = exc
            delay = _retry_delay(error, attempt, retries)
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
        migrated_from: Optional[str] = None,
    ) -> None:
        super().__init__(
            secret_key,
            host=host,
            timeout=timeout,
            max_retries=max_retries,
            disabled=disabled,
            migrated_from=migrated_from,
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
        finish = self._start(_SEND_NOT_DONE)
        try:
            result = await self._deliver(prepared, self._secret_key(_SEND_KEY_NEEDED))
        finally:
            finish()
        _log_warnings(result)
        return result

    async def _deliver(
        self, prepared: _PreparedSend, secret_key: str
    ) -> SendEmailResult:
        if prepared.files:
            await self._upload(prepared, secret_key)
        headers = _headers(
            secret_key,
            self.migrated_from,
            has_body=True,
            idempotency_key=prepared.idempotency_key,
        )
        return await self._call(
            self._posthog_request("POST", prepared.path, prepared.body, headers),
            _parse_response,
            prepared.max_retries,
        )

    async def _upload(self, prepared: _PreparedSend, secret_key: str) -> None:
        headers = _headers(secret_key, self.migrated_from, has_body=True)
        uploads = await self._call(
            self._posthog_request(
                "POST", _UPLOADS_PATH, _files_body(prepared.files), headers
            ),
            _pending_uploads_parser(prepared.files),
            prepared.max_retries,
        )
        for upload in uploads:
            await self._call(
                self._storage_request(upload),
                _upload_parser(upload.file.first_attachment_index),
                prepared.max_retries,
            )

    async def get(self, message_id: Union[str, UUID]) -> EmailStatus:
        """Read what happened to an email that :meth:`send` queued.

        Takes the same argument and raises the same errors as
        :meth:`Emails.get`.
        """
        path = _status_path(message_id)
        finish = self._start(_GET_NOT_DONE)
        try:
            headers = _headers(self._secret_key(_GET_KEY_NEEDED), self.migrated_from)
            return await self._call(
                self._posthog_request("GET", path, None, headers),
                _parse_status_response,
            )
        finally:
            finish()

    def _posthog_request(
        self, method: str, path: str, body: Optional[bytes], headers: dict[str, str]
    ) -> Callable[[], Awaitable[Any]]:
        _, client = self._get_client()
        # An injected client may carry its own auth and timeout, so set both per request: auth=None keeps
        # the secret key's Bearer header, and the request timeout applies whatever the client's default.
        return lambda: client.request(
            method,
            self.host + path,
            content=body,
            headers=headers,
            auth=None,
            timeout=self.timeout,
        )

    def _storage_request(self, upload: _Upload) -> Callable[[], Awaitable[Any]]:
        httpx, client = self._get_client()
        request = httpx.Request(
            upload.method,
            upload.url,
            content=upload.file.content,
            headers=upload.headers,
            extensions={"timeout": httpx.Timeout(self.timeout).as_dict()},
        )

        async def send() -> Any:
            try:
                return await client.send(request, auth=None)
            except httpx.RequestError as exc:
                raise _storage_unreachable(
                    upload.file.first_attachment_index, exc
                ) from None

        return send

    async def _call(
        self,
        send: Callable[[], Awaitable[Any]],
        parse: _Parse[_ResultT],
        max_retries: Optional[int] = None,
    ) -> _ResultT:
        httpx, _ = self._get_client()
        retries = self.max_retries if max_retries is None else max_retries
        for attempt in range(retries + 1):
            try:
                response = await send()
                try:
                    return parse(
                        response.status_code,
                        response.content,
                        response.headers.get("Retry-After"),
                    )
                finally:
                    await response.aclose()
            except httpx.RequestError as exc:
                error = _sdk_error("network_error", "The request to PostHog failed.")
                error.__cause__ = exc
            except EmailError as exc:
                error = exc
            delay = _retry_delay(error, attempt, retries)
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
    migrated_from: Optional[str],
) -> _EmailsT:
    if app_host:
        emails = factory(
            secret_key,
            host=app_host,
            timeout=timeout,
            max_retries=max_retries,
            migrated_from=migrated_from,
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
        migrated_from=migrated_from,
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
