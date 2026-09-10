# Portions of this package are derived from MCPCat/mcpcat-typescript-sdk
# Copyright (c) 2025 MCPcat
# Licensed under the MIT License: https://github.com/MCPCat/mcpcat-typescript-sdk/blob/main/LICENSE

"""Event sanitization: redact non-text response content blocks, large base64
strings, PostHog tokens, and sensitive keys. Pure functions that return new
objects without mutating the input; run after customer redaction (``before_send``
runs later in the pipeline) but before truncation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# SDK-injected arguments stripped from captured $mcp_parameters (they surface as
# dedicated properties: $mcp_intent and $mcp_conversation_id).
_INJECTED_ARGUMENT_NAMES = ("context", "conversation_id")
_REDACTED_VALUE = "[redacted]"
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/\n\r]+=*$")
_SIZE_GATE = 10_240
_POSTHOG_TOKEN_PATTERN = re.compile(r"\bph[a-z]_[A-Za-z0-9_-]{20,}\b")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"^(authorization|cookie|set-cookie|x-api-key|api[-_]?key|api[-_]?token|"
    r"access[-_]?token|refresh[-_]?token|token|password|secret|client[-_]?secret|"
    r"private[-_]?key)$",
    re.IGNORECASE,
)

# No leading `\b`: a word boundary needs a non-word character before the scheme,
# so `resource_https://user:pw@host` (an `_` before the scheme) would match
# nothing and stay unredacted. Without it the leftmost match wins — `foo.https://x`
# is read as scheme `foo.https`, which redacts the same credentials either way.
# `'` is a valid URI sub-delimiter, so it stays IN the match (`/o'reilly?token=x`
# must not be cut short of its query); `"`, `<` and `>` cannot appear unencoded in
# a URI, so they still terminate it.
#
# The authority is optional — an MCP resource uri need not have one
# (`resource:guide?token=...`, `file:/guide.md?token=...`), and `[^\s<>"]+`
# absorbs a `//host` when there is one. That over-matches prose (`Error:foo`,
# `at12:30`, `C:\path`), which is harmless: a match with nothing to redact is
# returned byte-for-byte, never re-serialized.
_URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]{0,63}:[^\s<>\"]+", re.IGNORECASE)
# Prose puts URLs in sentences ("see https://x?sig=a, then retry"), in parentheses
# and in single quotes, and the terminal class above swallows the punctuation that
# closes them. It is split off before parsing and re-appended to whatever comes
# back — including the `'` the pattern now keeps, since only a trailing one closes
# a quote. A character set stripped with `rstrip` rather than an anchored
# `[...]+$` pattern: backtracking that pattern over an interior run of punctuation
# is quadratic, and the URL comes from an attacker-influenceable request.
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}'"
# The length bound below caps parsing work on an attacker-shaped authority URL, so
# it only applies to a match that opens with one. A match this long with no
# authority is a data uri, which the bound must not eat — the binary-data branch
# deliberately keeps those, and the field count is already bounded when parsing.
_URL_AUTHORITY_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]{0,63}://", re.IGNORECASE)
_URL_AUTHORITY_SEARCH = re.compile(r"[a-z][a-z0-9+.-]{0,63}://", re.IGNORECASE)
_MAX_URL_LENGTH = 8192
_MAX_URL_QUERY_FIELDS = 128
# A query key is sensitive when ANY `-`/`_`/`.`-delimited segment matches, which
# covers the compound names credentials actually travel under: `private_token`,
# `oauth_signature`, `id_token`, `subscription-key`, `X-Amz-Security-Token`.
# Over-redacting a benign `sort_key` is the accepted trade for an analytics payload.
_SENSITIVE_QUERY_SEGMENT_PATTERN = re.compile(
    r"(^|[-_.])(auth|token|secret|password|passwd|pwd|credential|signature|sig|"
    r"key|hmac|sas|bearer|jwt|session|sessionid)([-_.]|$)",
    re.IGNORECASE,
)
# Matched whole rather than per segment: `code` (an OAuth authorization code) as a
# segment would eat `country_code`, `zip_code` and `lang_code`.
_SENSITIVE_QUERY_KEY_PATTERN = re.compile(
    r"^(code|AWSAccessKeyId|GoogleAccessId|Policy)$",
    re.IGNORECASE,
)

_UrlFields = List[Tuple[str, str]]


def _should_redact_query_key(key: str) -> bool:
    """Whether a URL query/fragment field's value must be dropped: the dict-key
    rule, plus the two URL-only rules above."""
    return bool(
        _should_redact_key(key)
        or _SENSITIVE_QUERY_SEGMENT_PATTERN.search(key)
        or _SENSITIVE_QUERY_KEY_PATTERN.match(key)
    )


def _sanitize_urls(text: str, *, nested: bool = True) -> str:
    # A string that IS one URL — a `$mcp_resource_name`, a `params.uri`, a query
    # field's value — has no prose around it, so nothing at its end is punctuation
    # closing a sentence: `?password=hunter2!!!` ends in the password itself.
    if _URL_PATTERN.fullmatch(text):
        return _sanitize_url(text, nested=nested, in_prose=False)
    return _URL_PATTERN.sub(
        lambda match: _sanitize_url(match.group(0), nested=nested, in_prose=True), text
    )


def _sanitize_url(value: str, *, nested: bool, in_prose: bool) -> str:
    if len(value) > _MAX_URL_LENGTH and _URL_AUTHORITY_PATTERN.match(value):
        return _REDACTED_VALUE
    # One match can hold a prose word in front of the address (`URL:https://...`,
    # `a:b:https://...`) or two addresses run together (`/doc,https://...`). Either
    # way the second address begins inside what would parse as the first one's
    # path, where nothing — its userinfo least of all — is redacted. So the match
    # is split at that authority and each part sanitized on its own. An authority
    # AFTER the first `?` or `#` is a query or fragment value instead, which the
    # field pass already handles (a sensitive key, or the nested pass).
    split = _split_at_second_address(value)
    if split is not None:
        return _sanitize_url(
            value[:split], nested=nested, in_prose=in_prose
        ) + _sanitize_url(value[split:], nested=nested, in_prose=in_prose)
    url_text = value.rstrip(_URL_TRAILING_PUNCTUATION) if in_prose else value
    suffix = value[len(url_text) :]
    try:
        url = urlsplit(url_text)
        query, sanitized_query = _sanitize_url_fields(url.query, nested=nested)
        # A fragment is only a field list when it looks like one; `#section-2` is
        # left byte-for-byte rather than re-serialized as `section-2=`.
        fragment, sanitized_fragment = (
            _sanitize_url_fields(url.fragment, nested=nested)
            if "=" in url.fragment
            else ([], [])
        )
        netloc = _redact_userinfo(url.netloc)
        if (netloc, sanitized_query, sanitized_fragment) == (
            url.netloc,
            query,
            fragment,
        ):
            return value
        # The split-off punctuation can be the tail of the credential rather than
        # the sentence's: `?password=hunter2!!!` would come back as
        # `?password=[redacted]!!!`. So when the last field of the part the URL
        # ends in was rewritten, its punctuation goes with it. Losing a comma from
        # the surrounding prose is the accepted cost.
        tail, sanitized_tail = (
            (fragment, sanitized_fragment) if url.fragment else (query, sanitized_query)
        )
        if tail and sanitized_tail[-1] != tail[-1]:
            suffix = ""
        # Only the part that changed is re-serialized, so an untouched query or
        # fragment keeps its original encoding.
        return (
            urlunsplit(
                (
                    url.scheme,
                    netloc,
                    url.path,
                    urlencode(sanitized_query)
                    if sanitized_query != query
                    else url.query,
                    urlencode(sanitized_fragment)
                    if sanitized_fragment != fragment
                    else url.fragment,
                )
            )
            + suffix
        )
    except ValueError:
        return _REDACTED_VALUE + suffix


def _split_at_second_address(value: str) -> Optional[int]:
    """Where a second address starts inside ``value``, or None. Each half is
    strictly shorter than the whole, so the split recursion terminates."""
    boundary = min(
        (value.index(char) for char in "?#" if char in value), default=len(value)
    )
    return next(
        (
            match.start()
            for match in _URL_AUTHORITY_SEARCH.finditer(value)
            if 0 < match.start() < boundary
        ),
        None,
    )


def _redact_userinfo(netloc: str) -> str:
    if "@" not in netloc:
        return netloc
    return "%5Bredacted%5D@" + netloc.rsplit("@", 1)[1]


def _sanitize_url_fields(text: str, *, nested: bool) -> Tuple[_UrlFields, _UrlFields]:
    """Parse a query (or fragment) and return both the original and the sanitized
    fields, so the caller can tell whether anything was redacted. ``;`` is
    normalized to ``&``: servers still emit it as a field separator, and a query
    split only on ``&`` would hide the credential behind it."""
    fields = parse_qsl(
        text.replace(";", "&"),
        keep_blank_values=True,
        max_num_fields=_MAX_URL_QUERY_FIELDS,
    )
    return fields, [
        (key, _sanitize_url_field_value(key, value, nested=nested))
        for key, value in fields
    ]


def _sanitize_url_field_value(key: str, value: str, *, nested: bool) -> str:
    if _should_redact_query_key(key):
        return _REDACTED_VALUE
    # A retained value can carry a URL of its own (a gateway's `?url=`). The budget
    # for that is one level: sanitize the first, and drop any value still carrying
    # a URL past it rather than trusting what we did not look inside.
    if _URL_PATTERN.search(value):
        return _sanitize_urls(value, nested=False) if nested else _REDACTED_VALUE
    return value


# PII redaction for the agent-narrated intent string only. $mcp_intent is free
# text the calling LLM writes into the injected `context` argument, so it can
# carry personal data the model read aloud despite being told not to. We redact
# well-defined *structured identifiers* — the kind regex can match with high
# precision. Person names and postal addresses are deliberately out of scope:
# they need an NER model that a client SDK cannot ship, and naive patterns would
# over-redact ordinary prose. Patterns are ordered so an earlier pass never eats
# digits a later pass needs (email before phone, IPs before phone, cards before
# the generic phone pass). See `redact_pii`.
#
# The `\d`/`\w`-based patterns are compiled with re.ASCII to match the JS
# semantics they are ported from: JS `\d`/`\w`/`\b` are ASCII-only, whereas
# Python's default is Unicode and would over-match (e.g. Unicode digits).
#
# Horizontal Unicode spaces (NBSP, narrow NBSP, ideographic space, ...) are what
# appear when text is copied from web pages or PDFs. `redact_pii` normalizes them
# to an ASCII space first so the separator-based card/phone/SSN candidates match
# them instead of leaking the identifier they group.
_UNICODE_SPACE_PATTERN = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
# Quantifiers are bounded to RFC-ish limits (local-part <=64, domain <=255,
# TLD <=24) rather than open-ended `+`. Unbounded `+` here is quadratic: on a
# long run of local-part chars with no valid `.tld`, `sub` rescans from every
# start position. $mcp_intent is attacker-influenceable free text seen before
# truncation, so an open-ended pattern is a reachable event-loop stall.
_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}"
)
_IPV4_PATTERN = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b",
    re.ASCII,
)
# Four forms: full 8-group, `::`-terminated (`2001:db8::`), a middle `::`
# (`2001:db8::8a2e:1`), and a leading `::` (`::1`). The compressed branches use a
# `(?<![\w:])` boundary so a hex-looking C++ scope like `std::bad` — whose left
# side is not a valid hex group — is not mistaken for an address, while a
# genuinely address-shaped `dead::beef` still matches.
_IPV6_PATTERN = re.compile(
    r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b"
    r"|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,7}:(?![\w:])"
    r"|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5}(?![\w])"
    r"|(?<![\w:])::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})(?![\w])",
    re.ASCII,
)
# A separator (space, dot, or dash) is required between the 3-2-4 groups so bare
# 9-digit IDs are never mistaken for an SSN.
_US_SSN_PATTERN = re.compile(r"\b\d{3}[ .-]\d{2}[ .-]\d{4}\b", re.ASCII)
# A run of >=13 digits optionally grouped by a single space, dot, dash, or
# slash. This only marks the numeric region; `_redact_card_in_match` then looks
# for the actual card as a run of whole separator-delimited groups that passes
# Luhn, so an adjacent field such as an expiry (`4111 1111 1111 1111 12/30`) is
# not absorbed into a failing check that would leak the card.
_CREDIT_CARD_CANDIDATE_PATTERN = re.compile(r"\b\d(?:[ ./-]?\d){12,}\b", re.ASCII)
# Matches each separator-delimited digit group inside a card candidate.
_DIGIT_GROUP_PATTERN = re.compile(r"\d+", re.ASCII)
# Phone matching is structural rather than "any 10-15 digits", so dates
# (`2024-01-15 12:30`) and dotted versions are not mistaken for numbers. Two
# forms: a North-American 3-3-4 grouping, and an international number that must
# start with `+` and a country code. The area code is either `(415)` (the
# separator after it is optional, so `(415)555-0142` matches) or a bare `415`
# that must be followed by a separator (space, dot, dash, or slash) — so a bare
# digit run is never taken for a phone number.
_PHONE_NANP_PATTERN = re.compile(
    r"(?<![\w+])(?:\+?1[ ./-]?)?(?:\(\d{3}\)[ ./-]?|\d{3}[ ./-])\d{3}[ ./-]\d{4}(?![\w])",
    re.ASCII,
)
_PHONE_INTL_PATTERN = re.compile(
    r"(?<!\w)\+\d{1,3}(?:[ ./()-]{0,2}\d){7,13}(?![\w])", re.ASCII
)


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _should_redact_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.match(key))


def _sanitize_string(value: str) -> str:
    if len(value) >= _SIZE_GATE and _BASE64_PATTERN.match(value):
        return "[binary data redacted - not supported by PostHog MCP analytics]"
    # PostHog tokens before URLs: rewriting a query percent-encodes `/`, and a
    # `?ref=/phx_...` token would then sit behind `%2F` where the pattern's `\bph`
    # boundary no longer matches it.
    value = _POSTHOG_TOKEN_PATTERN.sub(_REDACTED_VALUE, value)
    return _redact_secret_tokens(_sanitize_urls(value))


def _sanitize_resource_name(value: Any) -> Any:
    """Sanitize a ``resource_name``: either an identifier (a tool or prompt name)
    or a resource uri, so only the passes that matter for a uri run. The entropy
    detector ``sanitize_captured_value`` applies to free text is deliberately left
    out — it reads a name like ``Get_Organization_Memberships`` as a credential,
    and a redacted name costs every per-tool metric the event exists for. A name
    with no url in it comes back untouched."""
    if not isinstance(value, str):
        return value
    return _sanitize_urls(_POSTHOG_TOKEN_PATTERN.sub(_REDACTED_VALUE, value))


def _redact_secret_tokens(value: str) -> str:
    """Redact credential-looking words, leaving the surrounding text intact.

    The PostHog-token pattern above only knows ``phc_``/``phx_``; a failure
    message like ``auth failed for sk-proj-...`` carries someone else's key.
    Rather than enumerate every vendor's format — an arms race that fails
    quietly in both directions — this reuses the SDK's own detector
    (``exception_utils._looks_like_secret``: entropy, known formats such as AWS
    key ids, PEM markers), which the code-variables path already ships.

    Applied per whitespace-separated token, not to the whole string: redacting
    an entire exception message would destroy the diagnostic value that
    ``$mcp_error_message`` exists to provide, and ordinary prose is left alone
    because no single word in it looks like a credential.
    """
    if " " not in value:
        return _REDACTED_VALUE if _is_secret(value) else value
    return " ".join(
        _REDACTED_VALUE if _is_secret(word) else word for word in value.split(" ")
    )


def _is_secret(word: str) -> bool:
    try:
        from posthog.exception_utils import _looks_like_secret

        return bool(word) and _looks_like_secret(word)
    except Exception:  # noqa: BLE001 - redaction must never break capture
        return False


def _passes_luhn(digits: str) -> bool:
    total = 0
    double = False
    for index in range(len(digits) - 1, -1, -1):
        digit = ord(digits[index]) - 48
        if digit < 0 or digit > 9:
            return False
        if double:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        double = not double
    return total % 10 == 0


def _redact_card_in_match(match: re.Match[str]) -> str:
    """Within a card candidate, redact every actual card — each run of whole
    separator-delimited digit groups whose joined digits are 13-19 long and pass
    Luhn — leaving any adjacent field (an expiry, a following ID) in place. For
    each starting group it takes the longest such run, redacts it, then resumes
    scanning after it so a second card in the same span (e.g. two numbers listed
    together) is caught too. Checking group-aligned runs rather than arbitrary
    digit windows keeps the false-positive rate at Luhn's own ~1-in-10, instead of
    letting a chance-valid sub-window of an ordinary long ID trigger redaction."""
    text = match.group(0)
    groups = [
        (m.group(0), m.start(), m.end()) for m in _DIGIT_GROUP_PATTERN.finditer(text)
    ]
    output = ""
    cursor = 0
    first = 0
    while first < len(groups):
        digits = ""
        matched_last = -1
        for last in range(first, len(groups)):
            digits += groups[last][0]
            if len(digits) > 19:
                break
            if len(digits) >= 13 and _passes_luhn(digits):
                matched_last = last
        if matched_last >= 0:
            output += text[cursor : groups[first][1]] + _REDACTED_VALUE
            cursor = groups[matched_last][2]
            first = matched_last + 1
        else:
            first += 1
    return output + text[cursor:]


def redact_pii(value: Any) -> Any:
    """Redact structured personal identifiers (emails, IP addresses, credit-card
    numbers, US SSNs, and phone numbers) from a free-text string. Intended for the
    agent-narrated $mcp_intent value only — not for structured tool parameters or
    responses, where the same shapes are often legitimate data. Horizontal Unicode
    spaces are first normalized to an ASCII space so copy-pasted identifiers still
    match. Returns a new string; leaves the input's identifiers untouched when
    nothing matches. Non-string input (e.g. a non-string ``user_intent`` reaching
    the custom-event API) is returned unchanged, matching the pass-through
    behavior of ``sanitize_captured_value`` for non-str values."""
    if not isinstance(value, str):
        return value
    result = _UNICODE_SPACE_PATTERN.sub(" ", value)
    result = _EMAIL_PATTERN.sub(_REDACTED_VALUE, result)
    result = _IPV4_PATTERN.sub(_REDACTED_VALUE, result)
    result = _IPV6_PATTERN.sub(_REDACTED_VALUE, result)
    result = _CREDIT_CARD_CANDIDATE_PATTERN.sub(_redact_card_in_match, result)
    result = _US_SSN_PATTERN.sub(_REDACTED_VALUE, result)
    result = _PHONE_NANP_PATTERN.sub(_REDACTED_VALUE, result)
    result = _PHONE_INTL_PATTERN.sub(_REDACTED_VALUE, result)
    return result


def sanitize_captured_value(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, list):
        return [sanitize_captured_value(item) for item in value]
    # bool is an int subclass; both pass through unchanged.
    if not isinstance(value, dict):
        return value

    result: Dict[str, Any] = {}
    for key, nested in value.items():
        result[key] = (
            _REDACTED_VALUE
            if _should_redact_key(str(key))
            else sanitize_captured_value(nested)
        )
    return result


def sanitize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize an event's response, parameters, user_intent and error. Returns
    a new shallow copy; does not mutate the input."""
    result = {**event}

    if result.get("response") is not None:
        result["response"] = _sanitize_response(result["response"])

    if result.get("parameters") is not None:
        result["parameters"] = sanitize_captured_value(result["parameters"])

    if result.get("resource_name") is not None:
        result["resource_name"] = _sanitize_resource_name(result["resource_name"])

    # The intent comes straight from an agent-narrated `context` string, so it
    # can contain a secret the LLM read aloud or personal data it narrated about
    # the user. Strip structured PII (emails, phone numbers, IPs, cards, SSNs)
    # first, while the narration is still raw: the generic pass rewrites any URL
    # it finds, and a rewritten query percent-encodes `@`, which would hide
    # `?email=alice@example.com` from the email pattern. Then redact it like any
    # other captured value. PII redaction is scoped to the intent only —
    # structured tool parameters and responses often hold the same shapes as
    # legitimate data.
    if result.get("user_intent") is not None:
        result["user_intent"] = sanitize_captured_value(
            redact_pii(result["user_intent"])
        )

    if result.get("llm_model") is not None:
        result["llm_model"] = sanitize_captured_value(result["llm_model"])

    # An exception message is free text a server wrote, and it reaches PostHog
    # on the $exception sibling and — since it is also surfaced as
    # $mcp_error_message — on the primary event, so run it through the same
    # sanitizer as every other captured value.
    #
    # That sanitizer redacts PostHog tokens and sensitive-looking keys; it is
    # deliberately not a general credential scrubber, because enumerating every
    # vendor's key format is an arms race that fails quietly in both directions.
    # A host with strict requirements should gate free text in `before_send`.
    # Same scope as @posthog/mcp's sanitizeCapturedValue.
    if result.get("error") is not None:
        result["error"] = _sanitize_exception_values(result["error"])

    return result


def _sanitize_exception_values(error: Any) -> Any:
    """Redact the ``value`` of every frame in an ``$exception_list``, leaving
    the rest of the error-tracking shape untouched."""
    if not isinstance(error, dict):
        return error
    exception_list = error.get("$exception_list")
    if not isinstance(exception_list, list):
        return error
    return {
        **error,
        "$exception_list": [
            {**exception, "value": sanitize_captured_value(exception.get("value"))}
            if isinstance(exception, dict)
            else exception
            for exception in exception_list
        ],
    }


def _sanitize_response(response: Any) -> Any:
    if response is None or not isinstance(response, (dict, list, str)):
        return sanitize_captured_value(response)

    sanitized = sanitize_captured_value(response)
    if not _is_record(sanitized):
        return sanitized

    result = {**sanitized}
    content = result.get("content")
    if isinstance(content, list):
        result["content"] = [_sanitize_content_block(block) for block in content]

    if result.get("structuredContent") is not None and isinstance(
        result["structuredContent"], (dict, list)
    ):
        result["structuredContent"] = sanitize_captured_value(
            result["structuredContent"]
        )

    return result


def _sanitize_content_block(block: Any) -> Any:
    if not _is_record(block):
        return block

    block_type = block.get("type")
    if block_type == "text":
        return sanitize_captured_value(block)
    if block_type == "image":
        return {
            "type": "text",
            "text": "[image content redacted - not supported by PostHog MCP analytics]",
        }
    if block_type == "audio":
        return {
            "type": "text",
            "text": "[audio content redacted - not supported by PostHog MCP analytics]",
        }
    if block_type == "resource":
        return _sanitize_resource_block(block)
    if block_type == "resource_link":
        return sanitize_captured_value(block)
    return {
        "type": "text",
        "text": f'[unsupported content type "{block_type}" redacted - not supported by PostHog MCP analytics]',
    }


def _sanitize_resource_block(block: Dict[str, Any]) -> Any:
    resource = block.get("resource")
    if isinstance(resource, dict) and "blob" in resource:
        return {
            "type": "text",
            "text": "[binary resource content redacted - not supported by PostHog MCP analytics]",
        }
    return sanitize_captured_value(block)


def build_captured_mcp_parameters(
    request: Any, *, strip_llm_model: bool = False
) -> Dict[str, Any]:
    """Build the sanitized ``$mcp_parameters`` payload from a request, stripping
    the injected ``context`` argument before logging."""
    if not _is_record(request):
        return {"request": sanitize_captured_value(request)}

    captured_request: Dict[str, Any] = {}
    for key in ("id", "jsonrpc", "method"):
        if key in request:
            captured_request[key] = sanitize_captured_value(request[key])

    if "params" in request:
        captured_request["params"] = _build_captured_mcp_params(
            request["params"], strip_llm_model=strip_llm_model
        )

    return {"request": captured_request}


def _build_captured_mcp_params(params: Any, *, strip_llm_model: bool) -> Any:
    if not _is_record(params):
        return sanitize_captured_value(params)

    captured: Dict[str, Any] = {}
    for key, value in params.items():
        captured[key] = (
            _build_captured_mcp_arguments(value, strip_llm_model=strip_llm_model)
            if key == "arguments"
            else sanitize_captured_value(value)
        )
    return captured


def _build_captured_mcp_arguments(arguments: Any, *, strip_llm_model: bool) -> Any:
    if not _is_record(arguments):
        return sanitize_captured_value(arguments)

    captured: Dict[str, Any] = {}
    for key, value in arguments.items():
        if key in _INJECTED_ARGUMENT_NAMES or (strip_llm_model and key == "llm_model"):
            continue
        captured[key] = sanitize_captured_value(value)
    return captured
