"""Unit tests for branch-heavy internal helpers: intent resolution, conversation_id
schema/loop-back, session-id rollover, and the identity cache. These complement the
end-to-end adapter tests by exercising edge branches directly."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from posthog.mcp._conversation_id import (
    add_conversation_id_to_schema,
    can_inject_prompt_back,
    extract_conversation_id,
    inject_prompt_back,
    resolve_conversation_id,
)
from posthog.mcp._instrumentation import (
    VIRTUAL_TOOL_FEEDBACK,
    VIRTUAL_TOOL_MISSING_CAPABILITY,
    enabled_virtual_tool_names,
    is_first_listing_page,
    mutate_tool_schema,
    resolve_virtual_tool_injection,
)
from posthog.mcp._intent import _get_context_argument, resolve_tool_call_intent
from posthog.mcp._internal import (
    IdentityCache,
    MCPAnalyticsData,
    are_identities_equal,
    merge_identities,
)
from posthog.mcp.session import (
    derive_session_id_from_mcp_session,
    new_session_id,
    resolve_session_id,
)
from posthog.mcp.types import (
    CollectFeedbackOptions,
    MCPAnalyticsOptions,
    UserIdentity,
)


def _data(**opts):
    return MCPAnalyticsData(options=MCPAnalyticsOptions(**opts))


def _call(name="tool", args=None):
    return {"method": "tools/call", "params": {"name": name, "arguments": args or {}}}


# --- intent ------------------------------------------------------------------


async def test_intent_from_context_argument():
    out = await resolve_tool_call_intent(
        _data(), _call(args={"context": "do the thing"})
    )
    assert out == ("do the thing", "context_parameter")


async def test_intent_skips_context_for_missing_capability_tool():
    # a get_more_tools call's context is a capability report, not a tool-call
    # intent — but only while the SDK actually owns that name
    out = await resolve_tool_call_intent(
        _data(report_missing=True),
        _call(name="get_more_tools", args={"context": "need csv export"}),
    )
    assert out is None


async def test_intent_keeps_context_for_a_real_tool_named_get_more_tools():
    # With report_missing off the SDK advertises no such tool, so one by that
    # name is the host's own and its context is an ordinary intent. Interception
    # has always been gated on report_missing, so dropping the intent here left
    # a real tool dispatched but unattributed.
    out = await resolve_tool_call_intent(
        _data(), _call(name="get_more_tools", args={"context": "need csv export"})
    )
    assert out == ("need csv export", "context_parameter")


async def test_intent_fallback_sync():
    data = _data(intent_fallback=lambda req, extra: "inferred it")
    assert await resolve_tool_call_intent(data, _call()) == ("inferred it", "inferred")


async def test_intent_fallback_async():
    async def fb(req, extra):
        return "async inferred"

    assert await resolve_tool_call_intent(_data(intent_fallback=fb), _call()) == (
        "async inferred",
        "inferred",
    )


async def test_intent_fallback_error_is_swallowed():
    def boom(req, extra):
        raise ValueError("nope")

    assert await resolve_tool_call_intent(_data(intent_fallback=boom), _call()) is None


async def test_intent_fallback_blank_returns_none():
    data = _data(intent_fallback=lambda req, extra: "   ")
    assert await resolve_tool_call_intent(data, _call()) is None


def test_get_context_argument_ignores_non_string_and_blank():
    assert _get_context_argument({"params": {"arguments": {"context": 123}}}) is None
    assert _get_context_argument({"params": {"arguments": {"context": "  "}}}) is None
    assert _get_context_argument({}) is None


# --- virtual tools -----------------------------------------------------------
# The kind-keyed resolver both virtual tools share. Duck-typed on `.name`, so it
# drives from plain namespaces and these run under both MCP SDK majors.


def _tool(name):
    return SimpleNamespace(name=name)


def test_first_page_is_an_absent_cursor():
    assert is_first_listing_page(None) is True
    assert is_first_listing_page(SimpleNamespace(cursor=None)) is True


def test_a_present_cursor_is_a_continuation_page():
    # `""` is a valid opaque cursor, not the absence of one. Reading it as
    # falsy would re-append the virtual tools to that page.
    assert is_first_listing_page(SimpleNamespace(cursor="")) is False
    assert is_first_listing_page(SimpleNamespace(cursor="abc")) is False


def test_enabled_names_follow_the_switches_and_renames():
    assert enabled_virtual_tool_names(_data()) == {}
    assert enabled_virtual_tool_names(_data(report_missing=True)) == {
        VIRTUAL_TOOL_MISSING_CAPABILITY: "get_more_tools"
    }
    both = enabled_virtual_tool_names(
        _data(
            report_missing=True,
            missing_capability_tool_name="find_tools",
            collect_feedback=CollectFeedbackOptions(tool_name="tell_posthog"),
        )
    )
    assert both == {
        VIRTUAL_TOOL_MISSING_CAPABILITY: "find_tools",
        VIRTUAL_TOOL_FEEDBACK: "tell_posthog",
    }


def test_resolver_injects_on_a_first_page():
    data = _data(report_missing=True, collect_feedback=True)
    injection = resolve_virtual_tool_injection(
        data, [_tool("echo")], is_first_page=True
    )
    assert injection == {
        VIRTUAL_TOOL_MISSING_CAPABILITY: "get_more_tools",
        VIRTUAL_TOOL_FEEDBACK: "send_feedback",
    }


def test_resolver_injects_nothing_on_a_continuation_page():
    data = _data(report_missing=True, collect_feedback=True)
    injection = resolve_virtual_tool_injection(
        data, [_tool("echo")], is_first_page=False
    )
    assert injection == {}


def test_resolver_skips_injection_on_a_first_page_collision():
    data = _data(report_missing=True)
    injection = resolve_virtual_tool_injection(
        data, [_tool("get_more_tools")], is_first_page=True
    )
    assert injection == {}


def test_resolver_decides_each_page_on_its_own_tools():
    # Stateless: a collision on one first page does not shadow the virtual tool
    # on the next, so dropping the colliding tool needs no re-instrumentation.
    data = _data(report_missing=True)
    blocked = resolve_virtual_tool_injection(
        data, [_tool("get_more_tools")], is_first_page=True
    )
    assert blocked == {}

    injection = resolve_virtual_tool_injection(
        data, [_tool("echo")], is_first_page=True
    )
    assert injection == {VIRTUAL_TOOL_MISSING_CAPABILITY: "get_more_tools"}


def test_resolver_warns_once_per_kind_name_and_variant():
    data = _data(report_missing=True)
    for _ in range(3):
        resolve_virtual_tool_injection(
            data, [_tool("get_more_tools")], is_first_page=True
        )
    assert data.warned_virtual_tool_collisions == {
        (VIRTUAL_TOOL_MISSING_CAPABILITY, "get_more_tools", "blocked")
    }


def test_resolver_keeps_missing_capability_when_both_share_a_name():
    # Every call path checks missing-capability first, so advertising both under
    # one name would dead-letter the feedback path.
    data = _data(
        report_missing=True,
        missing_capability_tool_name="ask_posthog",
        collect_feedback=CollectFeedbackOptions(tool_name="ask_posthog"),
    )
    injection = resolve_virtual_tool_injection(
        data, [_tool("echo")], is_first_page=True
    )
    assert injection == {VIRTUAL_TOOL_MISSING_CAPABILITY: "ask_posthog"}
    assert (
        VIRTUAL_TOOL_FEEDBACK,
        "ask_posthog",
        "duplicate",
    ) in data.warned_virtual_tool_collisions


# --- conversation_id ---------------------------------------------------------


def test_add_conversation_id_adds_property():
    out = add_conversation_id_to_schema(
        {"type": "object", "properties": {"x": {"type": "string"}}}, "t"
    )
    assert out["properties"]["conversation_id"]["type"] == "string"


def test_add_conversation_id_skips_when_already_present():
    schema = {"type": "object", "properties": {"conversation_id": {"type": "string"}}}
    assert add_conversation_id_to_schema(schema, "t") is schema


def test_schema_pipeline_does_not_warn_for_owned_conversation_id(monkeypatch):
    warnings = []
    monkeypatch.setattr("posthog.mcp._conversation_id.log", warnings.append)
    schema = {"type": "object", "properties": {"conversation_id": {"type": "string"}}}
    tool = SimpleNamespace(name="t", input_schema=schema)

    mutate_tool_schema(
        _data(context=False, capture_model=False, enable_conversation_id=True),
        tool,
        schema_attribute="input_schema",
        owns_context=False,
        context_required=False,
        is_sdk_virtual_tool=False,
    )

    assert tool.input_schema is schema
    assert warnings == []


def test_add_conversation_id_skips_complex_schema():
    schema = {"oneOf": [{"type": "object"}]}
    assert add_conversation_id_to_schema(schema, "t") is schema


def test_add_conversation_id_preserves_additional_properties_false():
    out = add_conversation_id_to_schema(
        {"type": "object", "properties": {}, "additionalProperties": False}, "t"
    )
    assert out["additionalProperties"] is False
    assert "conversation_id" in out["properties"]


def test_add_conversation_id_handles_none_schema():
    out = add_conversation_id_to_schema(None, "t")
    assert "conversation_id" in out["properties"]


def test_extract_conversation_id():
    assert extract_conversation_id({"conversation_id": " abc "}) == "abc"
    assert extract_conversation_id({"conversation_id": 123}) is None
    assert extract_conversation_id({"conversation_id": "   "}) is None
    assert extract_conversation_id("not a dict") is None


def test_resolve_conversation_id_disabled():
    assert resolve_conversation_id(False, {}) == (None, False)


def test_resolve_conversation_id_applies_to_virtual_tools():
    cid, minted = resolve_conversation_id(True, {})
    assert minted is True and cid


def test_resolve_conversation_id_uses_supplied_when_mintable_shape():
    # Only an echo of a handle we could have minted (a uuidv7) is accepted —
    # the handle becomes $session_id, so an invented value ("conv-1") must not
    # anchor two unrelated callers to one session (parity with posthog-js).
    handle = "0198d3a7-1111-7222-8333-444455556666"
    assert resolve_conversation_id(True, {"conversation_id": handle}) == (
        handle,
        False,
    )


def test_resolve_conversation_id_replaces_invented_values():
    cid, minted = resolve_conversation_id(True, {"conversation_id": "conv-1"})
    assert minted is True and cid != "conv-1"


def test_resolve_conversation_id_mints_when_absent():
    cid, minted = resolve_conversation_id(True, {})
    assert minted is True and isinstance(cid, str) and cid


def test_can_inject_prompt_back():
    assert can_inject_prompt_back({"content": []}) is True
    # Errored results carry the prompt-back on purpose: a first-call failure is
    # exactly when the agent needs the handle (parity with posthog-js).
    assert can_inject_prompt_back({"content": [], "isError": True}) is True
    assert can_inject_prompt_back({"content": "not a list"}) is False
    assert can_inject_prompt_back("not a dict") is False


def test_inject_prompt_back_appends_block():
    out = inject_prompt_back({"content": [{"type": "text", "text": "hi"}]}, "conv-9")
    assert len(out["content"]) == 2 and "conv-9" in out["content"][1]["text"]


def test_inject_prompt_back_noop_when_not_injectable():
    result = {"content": "not a list"}
    assert inject_prompt_back(result, "conv-9") is result


# --- session id rollover -----------------------------------------------------


def _session_data():
    data = _data()
    data.session_id = new_session_id()
    return data


def test_derive_session_id_is_deterministic():
    a = derive_session_id_from_mcp_session("mcp-123")
    assert a == derive_session_id_from_mcp_session("mcp-123")
    assert a != derive_session_id_from_mcp_session("mcp-456")


async def test_resolve_session_id_uses_mcp_session():
    data = _session_data()
    sid = await resolve_session_id(data, "mcp-abc")
    assert sid == derive_session_id_from_mcp_session("mcp-abc")
    assert data.session_source == "mcp"


async def test_resolve_session_id_keeps_mcp_session_without_fragmenting():
    data = _session_data()
    first = await resolve_session_id(data, "mcp-abc")
    # a later request with no MCP session id must not fragment the session
    assert await resolve_session_id(data, None) == first


async def test_resolve_session_id_no_rollover_within_timeout():
    data = _session_data()
    before = data.session_id
    assert await resolve_session_id(data, None) == before


async def test_resolve_session_id_rolls_over_after_inactivity():
    data = _session_data()
    before = data.session_id
    data.last_activity = datetime.now(timezone.utc) - timedelta(minutes=31)
    after = await resolve_session_id(data, None)
    assert after != before and data.session_source == "generated"


# --- identity cache / merge --------------------------------------------------


def test_are_identities_equal():
    a = UserIdentity(distinct_id="u1", properties={"plan": "pro"}, groups={"org": "o1"})
    b = UserIdentity(distinct_id="u1", properties={"plan": "pro"}, groups={"org": "o1"})
    assert are_identities_equal(a, b)
    assert not are_identities_equal(a, UserIdentity(distinct_id="u2"))


def test_merge_identities_merges_properties_and_keeps_distinct_id():
    merged = merge_identities(
        UserIdentity(distinct_id="u1", properties={"a": 1}),
        UserIdentity(distinct_id="u1", properties={"b": 2}),
    )
    assert merged.properties == {"a": 1, "b": 2}


def test_merge_identities_with_no_previous_returns_next():
    nxt = UserIdentity(distinct_id="u1")
    assert merge_identities(None, nxt) is nxt


def test_identity_cache_evicts_least_recently_used():
    cache = IdentityCache(max_size=2)
    cache.set("s1", UserIdentity(distinct_id="u1"))
    cache.set("s2", UserIdentity(distinct_id="u2"))
    cache.get("s1")  # touch s1 so s2 becomes the LRU entry
    cache.set("s3", UserIdentity(distinct_id="u3"))  # evicts s2
    assert cache.has("s1") and cache.has("s3") and not cache.has("s2")
