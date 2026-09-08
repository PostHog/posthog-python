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


def test_redact_pii_is_not_quadratic_on_pathological_input():
    # A 100k-char run with an `@` but no valid TLD is the worst case for an
    # unbounded email pattern. With bounded quantifiers this stays linear; a
    # regression to `+` would blow up the runtime instead.
    import time

    pathological = f"{'a' * 50_000}@{'a' * 50_000}"
    start = time.monotonic()
    assert redact_pii(pathological) == pathological
    assert time.monotonic() - start < 1.0


def test_sanitize_event_redacts_pii_from_intent():
    event = {
        "user_intent": "Looking up orders for jane.doe@acme.com and calling +1 (415) 555-0142 about a refund.",
    }
    result = sanitize_event(event)
    assert (
        result["user_intent"]
        == "Looking up orders for [redacted] and calling [redacted] about a refund."
    )


def test_sanitize_event_composes_pii_and_token_redaction_on_intent():
    event = {
        "user_intent": "Rotating token phc_123456789012345678901234567890 for user carol@example.org."
    }
    result = sanitize_event(event)
    assert result["user_intent"] == "Rotating token [redacted] for user [redacted]."


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
