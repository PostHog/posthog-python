"""Unit tests for branch-heavy internal helpers: intent resolution, conversation_id
schema/loop-back, session-id rollover, and the identity cache. These complement the
end-to-end adapter tests by exercising edge branches directly."""

from datetime import datetime, timedelta, timezone

from posthog.mcp._conversation_id import (
    add_conversation_id_to_schema,
    can_inject_prompt_back,
    extract_conversation_id,
    inject_prompt_back,
    resolve_conversation_id,
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
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity


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
    # a get_more_tools call's context is a capability report, not a tool-call intent
    out = await resolve_tool_call_intent(
        _data(), _call(name="get_more_tools", args={"context": "need csv export"})
    )
    assert out is None


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


# --- conversation_id ---------------------------------------------------------


def test_add_conversation_id_adds_property():
    out = add_conversation_id_to_schema(
        {"type": "object", "properties": {"x": {"type": "string"}}}, "t"
    )
    assert out["properties"]["conversation_id"]["type"] == "string"


def test_add_conversation_id_skips_when_already_present():
    schema = {"type": "object", "properties": {"conversation_id": {"type": "string"}}}
    assert add_conversation_id_to_schema(schema, "t") is schema


def test_add_conversation_id_skips_complex_schema():
    schema = {"oneOf": [{"type": "object"}]}
    assert add_conversation_id_to_schema(schema, "t") is schema


def test_add_conversation_id_strips_additional_properties_false():
    out = add_conversation_id_to_schema(
        {"type": "object", "properties": {}, "additionalProperties": False}, "t"
    )
    assert "additionalProperties" not in out
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
    assert resolve_conversation_id(False, {}, "t", "get_more_tools") == (None, False)


def test_resolve_conversation_id_skips_missing_capability_tool():
    assert resolve_conversation_id(True, {}, "get_more_tools", "get_more_tools") == (
        None,
        False,
    )


def test_resolve_conversation_id_uses_supplied():
    assert resolve_conversation_id(
        True, {"conversation_id": "conv-1"}, "t", "get_more_tools"
    ) == ("conv-1", False)


def test_resolve_conversation_id_mints_when_absent():
    cid, minted = resolve_conversation_id(True, {}, "t", "get_more_tools")
    assert minted is True and isinstance(cid, str) and cid


def test_can_inject_prompt_back():
    assert can_inject_prompt_back({"content": []}) is True
    assert can_inject_prompt_back({"content": [], "isError": True}) is False
    assert can_inject_prompt_back({"content": "not a list"}) is False
    assert can_inject_prompt_back("not a dict") is False


def test_inject_prompt_back_appends_block():
    out = inject_prompt_back({"content": [{"type": "text", "text": "hi"}]}, "conv-9")
    assert len(out["content"]) == 2 and "conv-9" in out["content"][1]["text"]


def test_inject_prompt_back_noop_when_not_injectable():
    result = {"isError": True, "content": []}
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
