"""Unit tests for the MCP analytics core pipeline (Milestone 1, no server)."""

from datetime import datetime, timezone

import pytest

from posthog.mcp.constants import (
    POSTHOG_MCP_ANALYTICS_SOURCE,
    PostHogMCPAnalyticsEvent,
    PostHogMCPAnalyticsProperty,
)
from posthog.mcp._event_types import MCPAnalyticsEventType
from posthog.mcp._exceptions import capture_exception
from posthog.mcp._ids import deterministic_prefixed_id, new_prefixed_id
from posthog.mcp._posthog_events import build_posthog_capture_events
from posthog.mcp._sanitization import (
    build_captured_mcp_parameters,
    redact_pii,
    sanitize_captured_value,
    sanitize_event,
)
from posthog.mcp._sink import McpCaptureOptions, process_mcp_event
from posthog.mcp._truncation import MAX_EVENT_BYTES, normalize, truncate_event

# --- ids ---------------------------------------------------------------------


def test_new_prefixed_id_shape():
    sid = new_prefixed_id("ses")
    assert sid.startswith("ses_")
    # uuid7 string form: 8-4-4-4-12
    uuid_part = sid[len("ses_") :]
    assert len(uuid_part.split("-")) == 5
    assert uuid_part[14] == "7"  # version nibble


def test_new_prefixed_id_unique_and_time_ordered():
    import time

    first = [new_prefixed_id("evt") for _ in range(25)]
    time.sleep(0.005)
    second = [new_prefixed_id("evt") for _ in range(25)]
    assert len(set(first + second)) == 50  # unique
    # uuidv7 is time-ordered across milliseconds: every id minted later sorts after earlier ones
    assert max(first) < min(second)


def test_deterministic_prefixed_id_is_stable():
    a = deterministic_prefixed_id("ses", "mcp-session-123")
    b = deterministic_prefixed_id("ses", "mcp-session-123")
    c = deterministic_prefixed_id("ses", "other")
    assert a == b
    assert a != c
    assert a.startswith("ses_")
    assert len(a[len("ses_") :]) == 32  # two 16-char fnv1a halves


# --- sanitization ------------------------------------------------------------


def test_sanitize_redacts_posthog_token():
    out = sanitize_captured_value("my key is phx_abcdefghijklmnopqrstuvwxyz123 ok")
    assert "phx_" not in out
    assert "[redacted]" in out


def test_sanitize_redacts_sensitive_keys():
    out = sanitize_captured_value(
        {"authorization": "Bearer x", "api_key": "k", "safe": "keep"}
    )
    assert out["authorization"] == "[redacted]"
    assert out["api_key"] == "[redacted]"
    assert out["safe"] == "keep"


def test_sanitize_redacts_large_base64():
    blob = "A" * 11000
    assert sanitize_captured_value(blob).startswith("[binary data redacted")


@pytest.mark.parametrize(
    "value, expected",
    [
        (
            "https://example.com/guide?token=fakesecret&token=fakeaccess&empty=",
            "https://example.com/guide?token=%5Bredacted%5D&token=%5Bredacted%5D&empty=",
        ),
        (
            "https://example.com/guide?X-Goog-Credential=fakecredential&X-Goog-Signature=fakesignature",
            "https://example.com/guide?X-Goog-Credential=%5Bredacted%5D&X-Goog-Signature=%5Bredacted%5D",
        ),
        (
            "https://example.com/guide?sig=fakesignature&Signature=fakesignature&X-Amz-Security-Token=fakesecret",
            "https://example.com/guide?sig=%5Bredacted%5D&Signature=%5Bredacted%5D&X-Amz-Security-Token=%5Bredacted%5D",
        ),
        (
            "https://fakeuser@example.com/guide",
            "https://%5Bredacted%5D@example.com/guide",
        ),
        (
            "https://example.com/guide?%61=hello%20world&empty=#part",
            "https://example.com/guide?%61=hello%20world&empty=#part",
        ),
        (
            "Cannot read https://fakeuser:fakepass@example.com/guide or https://example.com/guide?token=fakesecret",
            "Cannot read https://%5Bredacted%5D@example.com/guide or https://example.com/guide?token=%5Bredacted%5D",
        ),
        ("https://fakeuser:fakepass@[invalid/guide?token=fakesecret", "[redacted]"),
        (
            "https://app.example.com/cb#access_token=fakeaccess&token_type=bearer",
            "https://app.example.com/cb#access_token=%5Bredacted%5D&token_type=%5Bredacted%5D",
        ),
        ("https://example.com/doc#section-2", "https://example.com/doc#section-2"),
        # A plain fragment is text, and text can carry an address: a match ends at
        # the first `#`, so this pass is the only one that sees that address.
        (
            "[a](https://public.test/#intro)[b](https://fakeuser:fakepass@private.test/doc)",
            "[a](https://public.test/#intro)[b](https://%5Bredacted%5D@private.test/doc)",
        ),
        (
            "[a](https://public.test/#intro)[b](https://private.test/doc)",
            "[a](https://public.test/#intro)[b](https://private.test/doc)",
        ),
        # A route prefix is that same plain text: it is kept verbatim, so it has to
        # be sanitized too.
        (
            "https://public.test/#https://fakeuser:fakepass@private.test/doc?page=1",
            "https://public.test/#https://%5Bredacted%5D@private.test/doc?page=1",
        ),
        (
            "[a](https://public.test/#intro)[b](https://fakeuser:fakepass@private.test/doc?page=1)",
            "[a](https://public.test/#intro)[b](https://%5Bredacted%5D@private.test/doc?page=1)",
        ),
        # Neither the fragment text pass nor the address split may recurse per URL:
        # both of these are one match, and both used to grow the stack with it.
        pytest.param(
            "resource:x#" * 10_000 + "intro",
            "resource:x#resource:x#[redacted]",
            id="fragment-chain",
        ),
        pytest.param(
            "https://a.test/x," * 10_000 + "https://fakeuser:fakepass@b.test/doc",
            "https://a.test/x," * 10_000 + "https://%5Bredacted%5D@b.test/doc",
            id="address-chain",
        ),
        # A hash-routed URL puts the route in the fragment: it stays verbatim, and
        # only what follows the first `?` is a field list.
        (
            "https://example.com/#/callback?token=fakesecret",
            "https://example.com/#/callback?token=%5Bredacted%5D",
        ),
        ("https://example.com/#/docs?page=2", "https://example.com/#/docs?page=2"),
        ("https://example.com/#/callback", "https://example.com/#/callback"),
        # Shape says nothing: this half of the fragment holds a `=`, so it is read
        # as fields even though it looks like a path.
        (
            "https://example.com/#/docs/id=1?token=fakesecret",
            "https://example.com/#/docs/id=1?token=%5Bredacted%5D",
        ),
        # ... but when the half before the `?` ends in a value we just redacted,
        # that `?` may be a character of the credential rather than a boundary, so
        # what follows it goes too.
        (
            "https://example.com/#password=prefix?fakesecret",
            "https://example.com/#password=%5Bredacted%5D?[redacted]",
        ),
        (
            "https://example.com/#password=prefix?token=x&page=1",
            "https://example.com/#password=%5Bredacted%5D?[redacted]",
        ),
        # The head is byte-identical here — the PostHog-token pass had already
        # redacted that value — so what marks the tail as suspect is the key.
        (
            "https://example.com/#password=phx_EXAMPLEONLYFAKEVALUE00000000000?private-suffix",
            "https://example.com/#password=[redacted]?[redacted]",
        ),
        ("https://example.com/#/docs/id=1", "https://example.com/#/docs/id=1"),
        # A field list whose key happens to start with a `/`; re-serializing the
        # redacted field percent-encodes that `/`.
        (
            "https://example.com/#/token=fakesecret",
            "https://example.com/#%2Ftoken=%5Bredacted%5D",
        ),
        # A `?` splits a fragment in two, and each half is a field list or plain
        # text on its own terms — the half before the `?` here is fields, and the
        # half after keeps its own encoding.
        (
            "https://example.com/#access_token=fakesecret&next=https://other.test/?page=1",
            "https://example.com/#access_token=%5Bredacted%5D"
            "&next=https%3A%2F%2Fother.test%2F?page=1",
        ),
        (
            "https://example.com/#/token=fakesecret&next=https://other.test/?page=1",
            "https://example.com/#%2Ftoken=%5Bredacted%5D"
            "&next=https%3A%2F%2Fother.test%2F?page=1",
        ),
        # A `;` is a field separator to some servers and a value character to
        # others, so a value holding one goes whole rather than being split: the
        # legacy field is still redacted, and `password=pre;fix` keeps its tail
        # out of the payload.
        (
            "https://example.com/x?a=1;token=fakesecret",
            "https://example.com/x?a=%5Bredacted%5D",
        ),
        (
            "https://example.com/guide?password=prefix;remainingsecret",
            "https://example.com/guide?password=%5Bredacted%5D",
        ),
        ("https://example.com/x?a=1;b=2", "https://example.com/x?a=1;b=2"),
        (
            "https://example.com/x?jwt=fakejwt&sessionid=fakesession&code=fakecode&country_code=BR",
            "https://example.com/x?jwt=%5Bredacted%5D&sessionid=%5Bredacted%5D&code=%5Bredacted%5D&country_code=BR",
        ),
        # A redacted LAST field takes the split-off punctuation with it: the
        # punctuation may be the credential's own tail (`?password=hunter2!!!`).
        (
            "See https://example.com/x?sig=fakesignature, then retry.",
            "See https://example.com/x?sig=%5Bredacted%5D then retry.",
        ),
        (
            "Failed (https://example.com/x?sig=fakesignature).",
            "Failed (https://example.com/x?sig=%5Bredacted%5D",
        ),
        (
            "See https://example.com/x?password=fakepass!, then retry.",
            "See https://example.com/x?password=%5Bredacted%5D then retry.",
        ),
        # ... but only the last field: anything after it proves where the URL ended.
        (
            "See https://example.com/x?sig=fakesignature&page=2, then retry.",
            "See https://example.com/x?sig=%5Bredacted%5D&page=2, then retry.",
        ),
        (
            "See https://example.com/x?sig=fakesignature#intro, then retry.",
            "See https://example.com/x?sig=%5Bredacted%5D#intro, then retry.",
        ),
        ("Failed (https://example.com/x?a=b).", "Failed (https://example.com/x?a=b)."),
        (
            "resource_https://fakeuser:fakepass@example.com/doc",
            "resource_https://%5Bredacted%5D@example.com/doc",
        ),
        (
            "https://gitlab.example.com/api?private_token=fakesecret&oauth_signature=fakesignature"
            "&id_token=fakeaccess&subscription-key=fakekey&sort_key=name",
            "https://gitlab.example.com/api?private_token=%5Bredacted%5D&oauth_signature=%5Bredacted%5D"
            "&id_token=%5Bredacted%5D&subscription-key=%5Bredacted%5D&sort_key=%5Bredacted%5D",
        ),
        (
            "https://gateway.example.com/fetch?url=https://svc:fakepass@internal.example.com/doc%3Ftoken%3Dfakesecret",
            "https://gateway.example.com/fetch?url=https%3A%2F%2F%255Bredacted%255D%40internal.example.com"
            "%2Fdoc%3Ftoken%3D%255Bredacted%255D",
        ),
        (
            "https://en.wikipedia.org/wiki/Foo_(bar)",
            "https://en.wikipedia.org/wiki/Foo_(bar)",
        ),
        (
            "https://fakeuser:fakepass@en.wikipedia.org/wiki/Foo_(bar).",
            "https://%5Bredacted%5D@en.wikipedia.org/wiki/Foo_(bar).",
        ),
        ("file:///guide.md", "file:///guide.md"),
        # An MCP resource uri need not have an authority, so the `//` is optional.
        # `urlunsplit` re-serializes an authority-less `file:` uri as `file:///`,
        # which is also what JS's `new URL()` produces.
        (
            "file:/guide.md?token=fakesecret",
            "file:///guide.md?token=%5Bredacted%5D",
        ),
        ("resource:guide?token=fakesecret", "resource:guide?token=%5Bredacted%5D"),
        (
            "see:resource:guide?token=fakesecret",
            "see:resource:guide?token=%5Bredacted%5D",
        ),
        # An optional authority over-matches prose, which costs nothing: a match
        # with nothing to redact is returned byte-for-byte.
        ("Error: see resource:guide.", "Error: see resource:guide."),
        ("Meet at12:30 today", "Meet at12:30 today"),
        ("resource:guide", "resource:guide"),
        # A match that holds a prose word in front of the address, or two addresses
        # run together, is split at the second address and each part sanitized on
        # its own — otherwise the second one hides in the first one's path.
        (
            "Failed URL:https://fakeuser:fakepass@example.com/doc",
            "Failed URL:https://%5Bredacted%5D@example.com/doc",
        ),
        (
            "URL:https://example.com/x?token=fakesecret",
            "URL:https://example.com/x?token=%5Bredacted%5D",
        ),
        ("Note:https://example.com/doc", "Note:https://example.com/doc"),
        (
            "a:b:https://fakeuser:fakepass@example.com/doc",
            "a:b:https://%5Bredacted%5D@example.com/doc",
        ),
        (
            "https://example.com/doc,https://fakeuser:fakepass@other.example.com/doc",
            "https://example.com/doc,https://%5Bredacted%5D@other.example.com/doc",
        ),
        # An adjacent address is adjacent wherever it sits: a query key holds one
        # as readily as a path, and a key is never redacted on its own.
        (
            "[a](https://public.test/?download)[b](https://fakeuser:fakepass@private.test/doc)",
            "[a](https://public.test/?download)[b](https://%5Bredacted%5D@private.test/doc)",
        ),
        # ... unless it sits in a field's value, where splitting it off would cut
        # that value in two and publish the tail. The nearest structural character
        # before the authority decides: `=` means value, `/` and the field
        # separators mean a new address.
        (
            "https://host/x?token=foo%20https://secret.test/private",
            "https://host/x?token=%5Bredacted%5D",
        ),
        # A second `?` inside a value is a character of that value, not a
        # delimiter, so the address after it belongs to the token.
        (
            "https://example.com/?token=prefix?https://secret.example/private",
            "https://example.com/?token=%5Bredacted%5D",
        ),
        (
            "https://example.com/?q=see,https://fakeuser:fakepass@x.test/doc",
            "https://example.com/?q=see%2Chttps%3A%2F%2F%255Bredacted%255D%40x.test%2Fdoc",
        ),
        (
            "https://host/a=b/c,https://fakeuser:fakepass@x.test/doc",
            "https://host/a=b/c,https://%5Bredacted%5D@x.test/doc",
        ),
        # Only the fields region holds values, so a `=` in the path never makes
        # the address that follows it part of one.
        (
            "https://example.com/redirect=https://fakeuser:fakepass@private.example.com/doc",
            "https://example.com/redirect=https://%5Bredacted%5D@private.example.com/doc",
        ),
        (
            "https://example.com/?https://fakeuser:fakepass@x.test/doc",
            "https://example.com/?https://%5Bredacted%5D@x.test/doc",
        ),
        # The closing `)` goes with the redacted trailing field, by the rule above:
        # punctuation after a rewritten last field may be the credential's own.
        (
            "[a](https://example.com/a)[b](https://example.com/b?token=fakesecret)",
            "[a](https://example.com/a)[b](https://example.com/b?token=%5Bredacted%5D",
        ),
        # An authority after the first `?` is a query value, not a second address:
        # the outer URI is parsed whole, which is what redacts its own password.
        (
            "file:/guide?password=fakepass&url=https://example.com",
            "file:///guide?password=%5Bredacted%5D&url=https%3A%2F%2Fexample.com",
        ),
        # The `+` decodes to a space, so the inner address is part of the token's
        # value and goes with it.
        (
            "resource:g?token=fakesecret+https://fakeuser:fakepass@b",
            "resource:g?token=%5Bredacted%5D",
        ),
        # The credential detectors run before the URL pass: rewriting a URL can
        # push a word past the window the detector scans, and a known token format
        # in a long URL would survive.
        (
            "https://example.com/" + "a" * 120 + "?ref=ghp_" + "A" * 36 + "&token=x",
            "[redacted]",
        ),
        (
            "https://example.com/o'reilly?token=fakesecret",
            "https://example.com/o'reilly?token=%5Bredacted%5D",
        ),
        (
            "https://fakeuser:fake'pass@example.com/doc",
            "https://%5Bredacted%5D@example.com/doc",
        ),
        # A string that is nothing but a URL has no prose, so its tail belongs to
        # the URL: `!!!` is part of the password, `.` is part of the path.
        (
            "https://example.com/login?password=fakepass!!!",
            "https://example.com/login?password=%5Bredacted%5D",
        ),
        ("https://example.com/x?a=b.", "https://example.com/x?a=b."),
        # One level of nesting is sanitized; a value still carrying a URL past that
        # is dropped rather than trusted.
        (
            "https://gateway.example.com/fetch?url=https%3A%2F%2Fgateway2.example.com%2Ffetch"
            "%3Furl%3Dhttps%253A%252F%252Finternal.test%252Fdoc%253Ftoken%253Dfakesecret",
            "https://gateway.example.com/fetch?url=https%3A%2F%2Fgateway2.example.com%2Ffetch"
            "%3Furl%3D%255Bredacted%255D",
        ),
        # PostHog tokens are redacted before the URL is rewritten: percent-encoding
        # the `/` would put `%2F` where the token pattern's `\bph` boundary needs a
        # word boundary.
        (
            "https://example.com/?ref=/phx_EXAMPLEONLYFAKEVALUE00000000000&token=fakesecret",
            "https://example.com/?ref=%2F%5Bredacted%5D&token=%5Bredacted%5D",
        ),
        (
            "Read 'https://example.com/x?sig=fakesignature' first.",
            "Read 'https://example.com/x?sig=%5Bredacted%5D first.",
        ),
    ],
)
def test_sanitize_url_credentials(value: str, expected: str) -> None:
    assert sanitize_captured_value(value) == expected
    assert sanitize_captured_value(expected) == expected


@pytest.mark.parametrize(
    "uri, oversized",
    [
        pytest.param("https://example.com/" + "a/" * 4086, False, id="length-limit"),
        pytest.param(
            "https://example.com/" + "a/" * 4086 + "a", True, id="length-over-limit"
        ),
        pytest.param(
            "https://example.com/?" + "&".join(["page=1"] * 128),
            False,
            id="field-limit",
        ),
        pytest.param(
            "https://example.com/?" + "&".join(["page=1"] * 129),
            True,
            id="fields-over-limit",
        ),
        pytest.param(
            "https://example.com/?" + "&" * 128 + "token=fakesecret",
            True,
            id="empty-fields",
        ),
        # The length bound guards authority parsing, so it must not swallow a long
        # data uri — not valid base64, so the binary-data branch keeps it too.
        pytest.param(
            "data:application/octet-stream;base64,AAAA%ZZ" + "A" * 10_000,
            False,
            id="authority-less-over-limit",
        ),
    ],
)
def test_sanitize_url_bounds(uri: str, oversized: bool) -> None:
    expected = "[redacted]" if oversized else uri
    assert sanitize_captured_value(uri) == expected
    assert sanitize_captured_value(f"Cannot read {uri}") == f"Cannot read {expected}"


def test_sanitize_event_replaces_image_and_audio_blocks():
    event = {
        "response": {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": "base64...", "mimeType": "image/png"},
                {"type": "audio", "data": "base64...", "mimeType": "audio/wav"},
            ]
        }
    }
    out = sanitize_event(event)
    blocks = out["response"]["content"]
    assert blocks[0] == {"type": "text", "text": "hello"}
    assert blocks[1]["type"] == "text" and "image content redacted" in blocks[1]["text"]
    assert blocks[2]["type"] == "text" and "audio content redacted" in blocks[2]["text"]


def test_sanitize_event_redacts_blob_resource():
    event = {
        "response": {
            "content": [{"type": "resource", "resource": {"blob": "AAAA", "uri": "x"}}]
        }
    }
    out = sanitize_event(event)
    assert "binary resource content redacted" in out["response"]["content"][0]["text"]


def test_sanitize_does_not_mutate_input():
    event = {"parameters": {"token": "phx_aaaaaaaaaaaaaaaaaaaaaaaa"}}
    sanitize_event(event)
    assert event["parameters"]["token"] == "phx_aaaaaaaaaaaaaaaaaaaaaaaa"


# --- intent PII redaction ----------------------------------------------------

_NBSP = "\u00a0"
_NNBSP = "\u202f"


@pytest.mark.parametrize(
    "label, text, expected",
    [
        (
            "an email address",
            "Looking up orders for jane.doe@acme.co.uk before refunding.",
            "Looking up orders for [redacted] before refunding.",
        ),
        (
            "an email with a maximal 64-char local part",
            f"from {'a' * 64}@example.com now",
            "from [redacted] now",
        ),
        (
            "an IPv4 address",
            "Blocking traffic from 203.0.113.42 after abuse.",
            "Blocking traffic from [redacted] after abuse.",
        ),
        (
            "an IPv6 address with a middle ::",
            "Tracing request from 2001:db8::ff00:42:8329 across the mesh.",
            "Tracing request from [redacted] across the mesh.",
        ),
        (
            "an IPv6 address ending in ::",
            "Routing host 2001:db8:: for now.",
            "Routing host [redacted] for now.",
        ),
        (
            "an IPv6 loopback ::1",
            "Health check from ::1 passed.",
            "Health check from [redacted] passed.",
        ),
        (
            "a NANP phone with dashes",
            "Reference ticket for number 415-555-0142 escalation.",
            "Reference ticket for number [redacted] escalation.",
        ),
        (
            "a NANP phone with slashes",
            "Call the customer on 415/555/0142 today.",
            "Call the customer on [redacted] today.",
        ),
        (
            "a NANP phone with parens and +1",
            "Calling back on +1 (415) 555-0142 about the outage.",
            "Calling back on [redacted] about the outage.",
        ),
        (
            "a NANP phone with a parenthesized area code and no following separator",
            "Reaching them at (415)555-0142 today.",
            "Reaching them at [redacted] today.",
        ),
        (
            "an international phone with a + country code",
            "Ring +44 (0) 20 7946 0958 please.",
            "Ring [redacted] please.",
        ),
        (
            "a phone grouped with NBSP spaces",
            f"Calling the customer on 415{_NNBSP}555{_NNBSP}0132 today.",
            "Calling the customer on [redacted] today.",
        ),
        (
            "a Luhn-valid card with spaces",
            "Charging the saved card 4111 1111 1111 1111 for the renewal.",
            "Charging the saved card [redacted] for the renewal.",
        ),
        (
            "a card grouped with dots",
            "Charging card 4111.1111.1111.1111 today.",
            "Charging card [redacted] today.",
        ),
        (
            "a card grouped with slashes",
            "Charging card 4111/1111/1111/1111 today.",
            "Charging card [redacted] today.",
        ),
        (
            "a card grouped with NBSP spaces",
            f"Charging card 4111{_NBSP}1111{_NBSP}1111{_NBSP}1111 now.",
            "Charging card [redacted] now.",
        ),
        (
            "a card without absorbing an adjacent expiry field",
            "Charging card 4111 1111 1111 1111 12/30 for renewal.",
            "Charging card [redacted] 12/30 for renewal.",
        ),
        (
            "every card when two appear in one span",
            "Moving funds 4111 1111 1111 1111 5555 5555 5555 4444 now.",
            "Moving funds [redacted] [redacted] now.",
        ),
        (
            "an SSN with dashes",
            "Verifying SSN 123-45-6789 for the claim.",
            "Verifying SSN [redacted] for the claim.",
        ),
        (
            "an SSN with spaces",
            "Verifying SSN 123 45 6789 for the claim.",
            "Verifying SSN [redacted] for the claim.",
        ),
        (
            "an SSN with dots",
            "Verifying SSN 123.45.6789 for the claim.",
            "Verifying SSN [redacted] for the claim.",
        ),
    ],
)
def test_redact_pii_redacts(label, text, expected):
    assert redact_pii(text) == expected


@pytest.mark.parametrize(
    "label, text",
    [
        (
            "a bare numeric identifier without grouping",
            "Fetching record 4155550142 from the ledger service.",
        ),
        (
            "a bare 9-digit number that is not an SSN",
            "Looking up record 123456789 in the ledger.",
        ),
        (
            "a Luhn-invalid long digit run",
            "Correlating with order 1234567890123456 in the warehouse.",
        ),
        (
            "a date and time that resembles a phone number",
            "Deploying at 2024-01-15 12:30 UTC after review.",
        ),
        (
            "a dotted version/build number",
            "Upgrading to build 2024.11.05.1830 for the team.",
        ),
        (
            "a C++ scope expression that resembles IPv6",
            "Calling std::bad and std::vector helpers for the team.",
        ),
        (
            "ordinary prose with versions, dates, and code separators",
            "Upgrading to v1.2.3 on 2024-01-15 by refactoring std::vector usage.",
        ),
        (
            "prose with no personal data",
            "Searching the organization repositories to prioritize open performance issues.",
        ),
    ],
)
def test_redact_pii_leaves_untouched(label, text):
    assert redact_pii(text) == text


def test_redact_pii_redacts_multiple_identifiers():
    assert (
        redact_pii(
            "Emailing bob@example.com and calling +1-202-555-0170 about the issue."
        )
        == "Emailing [redacted] and calling [redacted] about the issue."
    )


def test_sanitize_url_is_not_quadratic_on_many_addresses():
    # Every address here sits after the same `?`, which is the worst case for a
    # value-position check that scans backwards from each one. The forward pass
    # sees each character once; a regression to a per-address scan would spend
    # seconds on this, on the server's own event loop.
    import time

    pathological = "https://a.test/?" + "https://b.test/x," * 4_000
    start = time.monotonic()
    assert sanitize_captured_value(pathological) == pathological
    assert time.monotonic() - start < 1.0


def test_redact_pii_is_not_quadratic_on_pathological_input():
    # A 100k-char run with an `@` but no valid TLD is the worst case for an
    # unbounded email pattern. With bounded quantifiers this stays linear; a
    # regression to `+` would blow up the runtime instead.
    import time

    pathological = f"{'a' * 50_000}@{'a' * 50_000}"
    start = time.monotonic()
    assert redact_pii(pathological) == pathological
    assert time.monotonic() - start < 1.0


@pytest.mark.parametrize(
    "event_type, resource_name, expected",
    [
        # A tool name is an identifier, not free text: the entropy detector that
        # guards captured values reads this one as a credential, and a redacted
        # name costs every per-tool metric the event exists for.
        (
            MCPAnalyticsEventType.MCP_TOOLS_CALL,
            "Get_Organization_Memberships",
            "Get_Organization_Memberships",
        ),
        (
            MCPAnalyticsEventType.IDENTIFY,
            "https://fakeuser:fakepass@example.com/doc",
            "https://%5Bredacted%5D@example.com/doc",
        ),
        (
            MCPAnalyticsEventType.MCP_RESOURCES_READ,
            "https://example.com/guide?token=fakesecret",
            "https://example.com/guide?token=%5Bredacted%5D",
        ),
        # An authority-less resource uri is redacted the same way, whatever prose
        # the authority-less pattern absorbed in front of it.
        (
            MCPAnalyticsEventType.MCP_RESOURCES_READ,
            "resource:guide?token=fakesecret",
            "resource:guide?token=%5Bredacted%5D",
        ),
        (
            MCPAnalyticsEventType.MCP_RESOURCES_READ,
            "see:resource:guide?token=fakesecret",
            "see:resource:guide?token=%5Bredacted%5D",
        ),
    ],
)
def test_sanitize_event_resource_name_keeps_identifiers_and_redacts_uris(
    event_type: str, resource_name: str, expected: str
) -> None:
    result = sanitize_event({"event_type": event_type, "resource_name": resource_name})
    assert result["resource_name"] == expected


def test_sanitize_event_redacts_pii_from_intent():
    event = {
        "user_intent": "Looking up orders for jane.doe@acme.com and calling +1 (415) 555-0142 about a refund.",
    }
    result = sanitize_event(event)
    assert (
        result["user_intent"]
        == "Looking up orders for [redacted] and calling [redacted] about a refund."
    )


@pytest.mark.parametrize(
    "label, intent, expected",
    [
        (
            "posthog-token-and-email",
            "Rotating token phc_123456789012345678901234567890 for user carol@example.org.",
            "Rotating token [redacted] for user [redacted].",
        ),
        # PII is stripped before the generic pass rewrites the URL: a rewritten
        # query percent-encodes `@`, and `email=alice%40example.com` no longer
        # looks like an email address. The host survives either way.
        (
            "email-inside-a-url",
            "Open https://example.com/?email=alice@example.com&token=fakesecret",
            "Open https://example.com/?email=%5Bredacted%5D&token=%5Bredacted%5D",
        ),
        # The binary gate runs before PII: splicing a redaction into a base64 blob
        # would stop it looking like base64, and the blob would be captured whole.
        (
            "base64-blob-with-a-card-shaped-run",
            "AAAA/" * 2052 + "4111111111111111/AAA",
            "[binary data redacted - not supported by PostHog MCP analytics]",
        ),
    ],
)
def test_sanitize_event_composes_pii_and_token_redaction_on_intent(
    label: str, intent: str, expected: str
) -> None:
    result = sanitize_event({"user_intent": intent})
    assert result["user_intent"] == expected


def test_sanitize_event_does_not_redact_pii_shapes_from_structured_data():
    event = {
        "user_intent": "Enriching the profile for dave@example.com from the CRM.",
        "parameters": {"email": "dave@example.com", "ip": "203.0.113.42"},
        "response": {
            "content": [
                {"type": "text", "text": "Matched dave@example.com at 203.0.113.42."}
            ]
        },
    }
    result = sanitize_event(event)
    assert result["user_intent"] == "Enriching the profile for [redacted] from the CRM."
    # Structured tool data keeps the same shapes: they are often legitimate here.
    assert result["parameters"] == {"email": "dave@example.com", "ip": "203.0.113.42"}
    assert (
        result["response"]["content"][0]["text"]
        == "Matched dave@example.com at 203.0.113.42."
    )


def test_sanitize_event_does_not_mutate_intent():
    original = "Paging on-call about ticket from user@example.com right now."
    event = {"user_intent": original}
    sanitize_event(event)
    assert event["user_intent"] == original


def test_sanitize_event_passes_through_non_string_intent():
    # user_intent is typed as Any on the custom-event API (Event = Dict[str, Any]),
    # so a non-string value must not raise; it should pass through unchanged, same
    # as sanitize_captured_value does for other non-str/list/dict values.
    result = sanitize_event({"user_intent": 123})
    assert result["user_intent"] == 123


# --- truncation --------------------------------------------------------------


def test_normalize_caps_long_strings():
    out = normalize("x" * 40000)
    assert out.endswith("...")
    assert len(out) == 32_768 + 3


def test_normalize_detects_cycles():
    a = {}
    a["self"] = a
    out = normalize(a)
    assert out["self"] == "[Circular ~]"


def test_normalize_limits_depth():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": "deep"}}}}}}
    out = normalize(deep, depth=2)
    # at depth 2 the nested object should be collapsed to a marker
    assert out["a"]["b"] in ("[Object]", {"c": "[Object]"}) or isinstance(
        out["a"]["b"], (dict, str)
    )


def test_normalize_handles_nan_and_infinity():
    assert normalize(float("nan")) == "[NaN]"
    assert normalize(float("inf")) == "[Infinity]"
    assert normalize(float("-inf")) == "[-Infinity]"


def test_truncate_event_enforces_byte_budget():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_x",
        "timestamp": datetime.now(timezone.utc),
        "parameters": {"big": ["y" * 5000 for _ in range(60)]},
    }
    out = truncate_event(event)
    import json

    size = len(json.dumps(out, default=str, separators=(",", ":")).encode("utf-8"))
    assert size <= MAX_EVENT_BYTES


# --- posthog_events ----------------------------------------------------------


def test_build_tool_call_event_properties():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "resource_name": "search_events",
        "tool_description": "Search events",
        "tool_category": "Logs",
        "duration": 12.5,
        "protocol_version": "2025-06-18",
        "user_intent": "find churn cohort",
        "user_intent_source": "context_parameter",
        "llm_model": "gpt-5.6-sol",
        "llm_model_source": "client_metadata",
        "is_error": False,
        "timestamp": datetime.now(timezone.utc),
    }
    [capture] = build_posthog_capture_events(event)
    props = capture["properties"]
    assert capture["event"] == PostHogMCPAnalyticsEvent.TOOL_CALL
    assert capture["distinct_id"] == "ses_abc"
    assert props[PostHogMCPAnalyticsProperty.SOURCE] == POSTHOG_MCP_ANALYTICS_SOURCE
    assert props[PostHogMCPAnalyticsProperty.TOOL_NAME] == "search_events"
    assert props[PostHogMCPAnalyticsProperty.TOOL_CATEGORY] == "Logs"
    assert props[PostHogMCPAnalyticsProperty.PROTOCOL_VERSION] == "2025-06-18"
    assert props[PostHogMCPAnalyticsProperty.INTENT] == "find churn cohort"
    assert props[PostHogMCPAnalyticsProperty.INTENT_SOURCE] == "context_parameter"
    assert props[PostHogMCPAnalyticsProperty.LLM_MODEL] == "gpt-5.6-sol"
    assert props[PostHogMCPAnalyticsProperty.LLM_MODEL_SOURCE] == "client_metadata"
    assert props[PostHogMCPAnalyticsProperty.SESSION_ID] == "ses_abc"
    # anonymous (no identity) => person processing disabled
    assert props["$process_person_profile"] is False


def test_protocol_version_on_primary_and_exception_events():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "resource_name": "search_events",
        "protocol_version": "2025-06-18",
        "is_error": True,
        "error": {"$exception_list": [{"type": "ValueError", "value": "boom"}]},
        "timestamp": datetime.now(timezone.utc),
    }
    captures = build_posthog_capture_events(event)
    # Both the primary $mcp_tool_call and the $exception sibling carry it.
    assert len(captures) == 2
    for capture in captures:
        assert (
            capture["properties"][PostHogMCPAnalyticsProperty.PROTOCOL_VERSION]
            == "2025-06-18"
        )


def test_identity_enables_person_processing_and_set():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "identify_actor_given_id": "user_1",
        "identify_actor_data": {"email": "a@b.com"},
        "groups": {"organization": "org_1"},
        "timestamp": datetime.now(timezone.utc),
    }
    [capture] = build_posthog_capture_events(event)
    props = capture["properties"]
    assert capture["distinct_id"] == "user_1"
    assert "$process_person_profile" not in props
    assert props["$set"] == {"email": "a@b.com"}
    assert props["$groups"] == {"organization": "org_1"}


def test_listed_tool_names_only_on_tools_list():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_LIST,
        "session_id": "ses_abc",
        "listed_tool_names": ["a", "b"],
        "timestamp": datetime.now(timezone.utc),
    }
    [capture] = build_posthog_capture_events(event)
    assert capture["event"] == PostHogMCPAnalyticsEvent.TOOLS_LIST
    assert capture["properties"][PostHogMCPAnalyticsProperty.LISTED_TOOL_NAMES] == [
        "a",
        "b",
    ]


def test_custom_event_name_is_verbatim():
    event = {
        "event_type": MCPAnalyticsEventType.CUSTOM,
        "event_name": "feedback_submitted",
        "session_id": "ses_abc",
        "properties": {"rating": 5},
        "timestamp": datetime.now(timezone.utc),
    }
    [capture] = build_posthog_capture_events(event)
    assert capture["event"] == "feedback_submitted"
    assert capture["properties"]["rating"] == 5


def test_exception_fan_out():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "resource_name": "broken_tool",
        "is_error": True,
        "error": capture_exception(ValueError("boom")),
        "timestamp": datetime.now(timezone.utc),
    }
    captures = build_posthog_capture_events(event)
    assert len(captures) == 2
    main, exc = captures
    assert main["event"] == PostHogMCPAnalyticsEvent.TOOL_CALL
    assert exc["event"] == PostHogMCPAnalyticsEvent.EXCEPTION
    assert exc["properties"]["$exception_list"][0]["value"] == "boom"
    assert exc["properties"][PostHogMCPAnalyticsProperty.TOOL_NAME] == "broken_tool"


def test_exception_fan_out_disabled():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "is_error": True,
        "error": capture_exception("boom"),
        "timestamp": datetime.now(timezone.utc),
    }
    captures = build_posthog_capture_events(event, enable_exception_autocapture=False)
    assert len(captures) == 1


# --- exceptions --------------------------------------------------------------


def test_capture_exception_from_exception_has_stacktrace():
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as e:
        props = capture_exception(e)
    assert props["$exception_level"] == "error"
    entry = props["$exception_list"][0]
    assert entry["value"] == "kaboom"
    assert entry["type"] == "RuntimeError"
    assert "stacktrace" in entry


def test_capture_exception_from_call_tool_result_dict():
    result = {
        "isError": True,
        "content": [{"type": "text", "text": "tool failed badly"}],
    }
    props = capture_exception(result)
    assert props["$exception_list"][0]["value"] == "tool failed badly"


def test_capture_exception_from_string():
    props = capture_exception("plain message")
    assert props["$exception_list"][0]["value"] == "plain message"
    assert props["$exception_list"][0]["type"] == "Error"


# --- process_mcp_event (full pipeline) ---------------------------------------


def test_build_captured_mcp_parameters_strips_context():
    request = {
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"q": "x", "context": "intent text"}},
    }
    captured = build_captured_mcp_parameters(request)
    args = captured["request"]["params"]["arguments"]
    assert (
        "context" not in args
    )  # the injected analytics param never lands in $mcp_parameters
    assert args["q"] == "x"
    assert captured["request"]["method"] == "tools/call"


@pytest.mark.parametrize(
    ("strip_llm_model", "expected_model"),
    [(True, None), (False, "application-owned-model")],
)
def test_build_captured_mcp_parameters_only_strips_sdk_owned_model(
    strip_llm_model, expected_model
):
    request = {
        "method": "tools/call",
        "params": {
            "name": "route",
            "arguments": {"llm_model": "application-owned-model"},
        },
    }

    captured = build_captured_mcp_parameters(request, strip_llm_model=strip_llm_model)
    assert captured["request"]["params"]["arguments"].get("llm_model") == expected_model


async def test_process_mcp_event_basic():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "resource_name": "t",
        "parameters": build_captured_mcp_parameters(
            {"params": {"arguments": {"q": "x", "context": "intent text"}}}
        ),
        "timestamp": datetime.now(timezone.utc),
    }
    result = await process_mcp_event(event, McpCaptureOptions())
    assert result is not None
    full_event, captures = result
    assert full_event["id"].startswith("evt_")
    assert len(captures) == 1
    args = captures[0]["properties"][PostHogMCPAnalyticsProperty.PARAMETERS]["request"][
        "params"
    ]["arguments"]
    assert "context" not in args
    assert args["q"] == "x"


async def test_before_send_can_drop_event():
    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "timestamp": datetime.now(timezone.utc),
    }
    result = await process_mcp_event(
        event, McpCaptureOptions(before_send=lambda e: None)
    )
    assert result is not None
    _, captures = result
    assert captures == []


async def test_before_send_can_mutate_event_async():
    async def before_send(e):
        e["properties"]["added"] = True
        return e

    event = {
        "event_type": MCPAnalyticsEventType.MCP_TOOLS_CALL,
        "session_id": "ses_abc",
        "timestamp": datetime.now(timezone.utc),
    }
    result = await process_mcp_event(event, McpCaptureOptions(before_send=before_send))
    _, captures = result
    assert captures[0]["properties"]["added"] is True
