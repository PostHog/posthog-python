"""Tests for the ``send_feedback`` virtual tool (the ``collect_feedback`` option)."""

import mcp.types as mcp_types
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import Server

from posthog.mcp import (
    SEND_FEEDBACK_TOOL_NAME,
    CollectFeedbackOptions,
    PostHogMCP,
    instrument,
    send_feedback_result,
)
from posthog.mcp.feedback import (
    build_feedback_event_properties,
    build_feedback_intent,
    get_feedback_tool_descriptor,
    parse_feedback_report,
    send_feedback_result_text,
)
from posthog.mcp.types import MCPAnalyticsOptions
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)

_REPORT_ARGS = {
    "feedback_type": "missing_capability",
    "summary": "No tool to delete multiple cohorts in one call.",
    "details": "Deleted 20 cohorts one by one via cohort-delete.",
    "friction_points": "cohort-delete accepts a single id; no batch variant.",
    "suggested_improvement": "Add a bulk delete tool.",
    "sentiment": "negative",
    "task_completed": True,
}


def make_fastmcp():
    server = FastMCP("feedback-fastmcp")

    @server.tool()
    def add(a: int, b: int) -> str:
        return f"sum is {a + b}"

    return server


def make_lowlevel():
    server = Server("feedback-lowlevel")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="echo",
                description="Echo",
                inputSchema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text=str(arguments.get("msg")))]

    return server


def _call_request(name, arguments):
    return mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )


async def _list_tools_lowlevel(server):
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    return await handler(mcp_types.ListToolsRequest(method="tools/list"))


# --- descriptor + config validation -------------------------------------------


def test_descriptor_defaults():
    descriptor = get_feedback_tool_descriptor()
    assert descriptor["name"] == SEND_FEEDBACK_TOOL_NAME
    assert "missing capability" in descriptor["description"]
    assert descriptor["inputSchema"]["required"] == ["feedback_type", "summary"]
    assert set(descriptor["inputSchema"]["properties"]) == {
        "feedback_type",
        "summary",
        "details",
        "friction_points",
        "suggested_improvement",
        "tool_name",
        "sentiment",
        "task_completed",
    }
    assert descriptor["annotations"] == {
        "title": "Send feedback",
        "readOnlyHint": True,
        "openWorldHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
    }


def test_descriptor_merges_extras_and_custom_name():
    options = CollectFeedbackOptions(
        tool_name="report_feedback",
        description="Tell us.",
        extra_properties={"product_area": {"type": "string", "description": "Area."}},
        extra_required=["product_area"],
    )
    descriptor = get_feedback_tool_descriptor(options)
    assert descriptor["name"] == "report_feedback"
    assert descriptor["description"] == "Tell us."
    assert descriptor["inputSchema"]["properties"]["product_area"]["type"] == "string"
    assert descriptor["inputSchema"]["required"] == [
        "feedback_type",
        "summary",
        "product_area",
    ]


def test_descriptor_deep_copies_host_fragments():
    fragment = {"type": "string", "enum": ["a", "b"]}
    options = CollectFeedbackOptions(extra_properties={"area": fragment})
    descriptor = get_feedback_tool_descriptor(options)
    fragment["enum"].append("mutated")
    descriptor["inputSchema"]["properties"]["feedback_type"]["enum"].append("bogus")
    assert get_feedback_tool_descriptor(options)["inputSchema"]["properties"]["area"][
        "enum"
    ] == ["a", "b", "mutated"]
    assert (
        "bogus"
        not in get_feedback_tool_descriptor()["inputSchema"]["properties"][
            "feedback_type"
        ]["enum"]
    )


@pytest.mark.parametrize(
    "key", ["summary", "type", "tool", "context", "conversation_id", "llm_model"]
)
def test_descriptor_rejects_reserved_extra_keys(key):
    options = CollectFeedbackOptions(extra_properties={key: {"type": "string"}})
    with pytest.raises(ValueError, match="collides"):
        get_feedback_tool_descriptor(options)


def test_descriptor_rejects_undeclared_extra_required():
    options = CollectFeedbackOptions(extra_required=["ghost"])
    with pytest.raises(ValueError, match="not declared"):
        get_feedback_tool_descriptor(options)


def test_instrument_fails_fast_on_config_error():
    server = make_fastmcp()
    options = MCPAnalyticsOptions(
        collect_feedback=CollectFeedbackOptions(
            extra_properties={"context": {"type": "string"}}
        )
    )
    with pytest.raises(ValueError, match="collides"):
        instrument(server, FakeClient(), options)


# --- parsing -------------------------------------------------------------------


def test_parse_falls_back_and_keeps_only_declared_extras():
    options = CollectFeedbackOptions(extra_properties={"area": {"type": "string"}})
    report = parse_feedback_report(
        {
            "feedback_type": "bogus",
            "summary": "   ",
            "sentiment": "angry",
            "task_completed": "yes",
            "area": "cohorts",
            "invented": "never captured",
        },
        options,
    )
    assert report.feedback_type == "other"
    assert report.summary == ""
    assert report.sentiment is None
    assert report.task_completed is None
    assert report.extras == {"area": "cohorts"}
    assert report.raw["invented"] == "never captured"


def test_parse_handles_missing_arguments():
    report = parse_feedback_report(None)
    assert report.feedback_type == "other"
    assert report.summary == ""
    assert report.extras == {} and report.raw == {}


def test_parse_reads_all_core_fields():
    report = parse_feedback_report({**_REPORT_ARGS, "tool_name": "cohort-delete"})
    assert report.feedback_type == "missing_capability"
    assert report.summary == _REPORT_ARGS["summary"]
    assert report.details == _REPORT_ARGS["details"]
    assert report.friction_points == _REPORT_ARGS["friction_points"]
    assert report.suggested_improvement == _REPORT_ARGS["suggested_improvement"]
    assert report.tool_name == "cohort-delete"
    assert report.sentiment == "negative"
    assert report.task_completed is True


# --- event properties + intent ---------------------------------------------------


def test_properties_carry_all_fields_and_redact_pii():
    report = parse_feedback_report(
        {
            **_REPORT_ARGS,
            "details": "Reach me at jane@example.com about it.",
            "tool_name": "cohort-delete",
        }
    )
    props = build_feedback_event_properties(report)
    assert props["$mcp_feedback_type"] == "missing_capability"
    assert props["$mcp_feedback_summary"] == _REPORT_ARGS["summary"]
    assert props["$mcp_feedback_details"] == "Reach me at [redacted] about it."
    assert props["$mcp_feedback_friction_points"] == _REPORT_ARGS["friction_points"]
    assert (
        props["$mcp_feedback_suggested_improvement"]
        == _REPORT_ARGS["suggested_improvement"]
    )
    assert props["$mcp_feedback_tool"] == "cohort-delete"
    assert props["$mcp_feedback_sentiment"] == "negative"
    assert props["$mcp_feedback_task_completed"] is True


def test_properties_bound_free_text_and_tool_name():
    report = parse_feedback_report(
        {"feedback_type": "issue", "summary": "s" * 5000, "tool_name": "t" * 500}
    )
    props = build_feedback_event_properties(report)
    assert len(props["$mcp_feedback_summary"]) == 2048 + 3
    assert props["$mcp_feedback_summary"].endswith("...")
    assert len(props["$mcp_feedback_tool"]) == 256 + 3


def test_properties_capture_declared_extras_only():
    options = CollectFeedbackOptions(
        extra_properties={
            "area": {"type": "string"},
            "score": {"type": "number"},
            "tags": {"type": "array"},
        }
    )
    report = parse_feedback_report(
        {
            "feedback_type": "praise",
            "summary": "Great tools.",
            "area": "mail me: jane@example.com",
            "score": 9,
            "tags": ["a", "b"],
            "invented": "nope",
        },
        options,
    )
    props = build_feedback_event_properties(report)
    assert props["$mcp_feedback_area"] == "mail me: [redacted]"
    assert props["$mcp_feedback_score"] == 9
    assert props["$mcp_feedback_tags"] == '["a", "b"]'
    assert "$mcp_feedback_invented" not in props


def test_extras_must_match_declared_type_and_enum():
    options = CollectFeedbackOptions(
        extra_properties={
            "score": {"type": "integer"},
            "channel": {"type": "string", "enum": ["web", "app"]},
        }
    )
    report = parse_feedback_report(
        {
            "feedback_type": "praise",
            "summary": "Great tools.",
            "score": "very high",
            "channel": "email",
        },
        options,
    )
    # Mismatches stay out of extras and the captured properties; raw keeps them.
    assert report.extras == {}
    assert report.raw["score"] == "very high" and report.raw["channel"] == "email"
    props = build_feedback_event_properties(report)
    assert "$mcp_feedback_score" not in props and "$mcp_feedback_channel" not in props

    conforming = parse_feedback_report(
        {"feedback_type": "praise", "summary": "s", "score": 9, "channel": "web"},
        options,
    )
    assert conforming.extras == {"score": 9, "channel": "web"}


def test_tool_name_gets_pii_redaction():
    report = parse_feedback_report(
        {"feedback_type": "issue", "summary": "s", "tool_name": "ask jane@example.com"}
    )
    props = build_feedback_event_properties(report)
    assert props["$mcp_feedback_tool"] == "ask [redacted]"


def test_pii_inside_urls_is_redacted_on_every_surface():
    # Guards the ordering of the free-text pass: running the URL rewrite before
    # PII redaction percent-encodes the "@" the email pattern anchors on, letting
    # the email through (the TS SDK's veria finding on this feature).
    url = "https://example.com/?email=jane@example.com"
    options = CollectFeedbackOptions(extra_properties={"area": {"type": "string"}})
    report = parse_feedback_report(
        {
            "feedback_type": "issue",
            "summary": f"Login fails at {url}",
            "details": f"see {url} too",
            "friction_points": f"url {url} slow",
            "suggested_improvement": f"fix {url}",
            "tool_name": url,
            "area": url,
        },
        options,
    )
    props = build_feedback_event_properties(report)
    for key in (
        "$mcp_feedback_summary",
        "$mcp_feedback_details",
        "$mcp_feedback_friction_points",
        "$mcp_feedback_suggested_improvement",
        "$mcp_feedback_tool",
        "$mcp_feedback_area",
    ):
        assert "jane@example.com" not in props[key], key
        assert "jane%40example.com" not in props[key], key
        assert "[redacted]" in props[key], key


def test_nested_extras_keep_key_based_redaction():
    # The feedback path walks extras with the free-text pass; nothing else
    # asserts that credential-named keys inside a nested extra still redact by
    # key name, so a stringify-first refactor could drop that protection silently.
    options = CollectFeedbackOptions(extra_properties={"meta": {"type": "object"}})
    report = parse_feedback_report(
        {
            "feedback_type": "issue",
            "summary": "s",
            "meta": {"note": "ping jane@example.com", "password": "hunter2"},
        },
        options,
    )
    props = build_feedback_event_properties(report)
    assert '"password": "[redacted]"' in props["$mcp_feedback_meta"]
    assert "hunter2" not in props["$mcp_feedback_meta"]
    assert '"note": "ping [redacted]"' in props["$mcp_feedback_meta"]


def test_intent_joins_summary_and_details():
    report = parse_feedback_report(_REPORT_ARGS)
    assert (
        build_feedback_intent(report)
        == f"{_REPORT_ARGS['summary']}\n\n{_REPORT_ARGS['details']}"
    )
    summary_only = parse_feedback_report({"summary": "Just this."})
    assert build_feedback_intent(summary_only) == "Just this."
    assert build_feedback_intent(parse_feedback_report(None)) == ""


# --- instrument(): FastMCP -------------------------------------------------------


async def test_fastmcp_advertises_and_captures_feedback():
    server = make_fastmcp()
    client = FakeClient()
    instrument(
        server, client, MCPAnalyticsOptions(collect_feedback=True, capture_model=True)
    )

    list_handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    virtual = [t for t in result.root.tools if t.name == "send_feedback"]
    assert virtual
    schema_props = virtual[0].inputSchema["properties"]
    # Its intent rides its own arguments; the model argument is still advertised.
    assert "context" not in schema_props and "conversation_id" not in schema_props
    assert "llm_model" in schema_props

    canned = await server._tool_manager.call_tool("send_feedback", dict(_REPORT_ARGS))
    await _flush()

    assert canned[0].text == send_feedback_result_text()
    feedback = _events(client, "$mcp_feedback")
    assert len(feedback) == 1
    props = feedback[0]["properties"]
    assert props["$mcp_feedback_type"] == "missing_capability"
    assert props["$mcp_feedback_task_completed"] is True
    assert (
        props["$mcp_intent"]
        == f"{_REPORT_ARGS['summary']}\n\n{_REPORT_ARGS['details']}"
    )
    assert props["$mcp_intent_source"] == "context_parameter"
    # The raw arguments are agent-narrated free text — never captured.
    assert "$mcp_parameters" not in props
    # A send_feedback call is NOT a normal tool call.
    assert _events(client, "$mcp_tool_call") == []


async def test_fastmcp_invalid_type_falls_back_to_other():
    server = make_fastmcp()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(collect_feedback=True))

    await server._tool_manager.call_tool("send_feedback", {"feedback_type": "bogus"})
    await _flush()

    feedback = _events(client, "$mcp_feedback")
    assert feedback[0]["properties"]["$mcp_feedback_type"] == "other"
    assert "$mcp_intent" not in feedback[0]["properties"]


async def test_fastmcp_collision_fails_open():
    server = make_fastmcp()

    @server.tool()
    def send_feedback(note: str) -> str:
        return f"real tool got {note}"

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(collect_feedback=True))

    list_handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    named = [t for t in result.root.tools if t.name == "send_feedback"]
    # Advertised once — the real tool, with normal context injection.
    assert len(named) == 1
    assert "context" in named[0].inputSchema["properties"]

    out = await server._tool_manager.call_tool(
        "send_feedback", {"note": "hi", "context": "using the real tool"}
    )
    await _flush()

    assert "real tool got hi" in str(out)
    assert _events(client, "$mcp_feedback") == []
    assert _events(client, "$mcp_tool_call")


async def test_fastmcp_collision_fails_open_before_any_listing():
    server = make_fastmcp()

    @server.tool()
    def send_feedback(note: str) -> str:
        return f"real tool got {note}"

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(collect_feedback=True))

    # No tools/list served yet — the live registry probe must protect the tool.
    out = await server._tool_manager.call_tool("send_feedback", {"note": "early"})
    await _flush()

    assert "real tool got early" in str(out)
    assert _events(client, "$mcp_feedback") == []
    assert _events(client, "$mcp_tool_call")


async def test_fastmcp_custom_tool_name():
    server = make_fastmcp()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            collect_feedback=CollectFeedbackOptions(tool_name="report_feedback")
        ),
    )

    list_handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    names = [t.name for t in result.root.tools]
    assert "report_feedback" in names and "send_feedback" not in names

    await server._tool_manager.call_tool(
        "report_feedback", {"feedback_type": "praise", "summary": "Nice."}
    )
    await _flush()
    assert _events(client, "$mcp_feedback")


async def test_fastmcp_coexists_with_report_missing():
    server = make_fastmcp()
    client = FakeClient()
    instrument(
        server, client, MCPAnalyticsOptions(report_missing=True, collect_feedback=True)
    )

    list_handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    result = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    names = [t.name for t in result.root.tools]
    assert "get_more_tools" in names and "send_feedback" in names

    await server._tool_manager.call_tool("get_more_tools", {"context": "need csv"})
    await server._tool_manager.call_tool(
        "send_feedback", {"feedback_type": "praise", "summary": "Nice."}
    )
    await _flush()

    assert len(_events(client, "$mcp_missing_capability")) == 1
    assert len(_events(client, "$mcp_feedback")) == 1
    assert _events(client, "$mcp_tool_call") == []


# --- on_feedback -----------------------------------------------------------------


async def test_on_feedback_custom_reply():
    server = make_fastmcp()
    client = FakeClient()
    seen = []

    def on_feedback(report):
        seen.append(report)
        return "Thanks - your feedback reached the team."

    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            collect_feedback=CollectFeedbackOptions(on_feedback=on_feedback)
        ),
    )

    canned = await server._tool_manager.call_tool("send_feedback", dict(_REPORT_ARGS))
    await _flush()

    assert canned[0].text == "Thanks - your feedback reached the team."
    assert seen and seen[0].feedback_type == "missing_capability"
    assert _events(client, "$mcp_feedback")


async def test_on_feedback_async_handler():
    server = make_fastmcp()
    client = FakeClient()

    async def on_feedback(report):
        return "async thanks"

    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            collect_feedback=CollectFeedbackOptions(on_feedback=on_feedback)
        ),
    )

    canned = await server._tool_manager.call_tool("send_feedback", dict(_REPORT_ARGS))
    await _flush()
    assert canned[0].text == "async thanks"


async def test_on_feedback_raise_falls_back_and_still_captures():
    server = make_fastmcp()
    client = FakeClient()

    def on_feedback(report):
        raise RuntimeError("backend down")

    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            collect_feedback=CollectFeedbackOptions(on_feedback=on_feedback)
        ),
    )

    canned = await server._tool_manager.call_tool("send_feedback", dict(_REPORT_ARGS))
    await _flush()

    assert canned[0].text == send_feedback_result_text()
    assert _events(client, "$mcp_feedback")


# --- instrument(): low-level v1 ----------------------------------------------------


async def test_lowlevel_advertises_and_captures_feedback():
    server = make_lowlevel()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(collect_feedback=True))

    result = await _list_tools_lowlevel(server)
    assert "send_feedback" in [t.name for t in result.root.tools]

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    out = await call_handler(_call_request("send_feedback", dict(_REPORT_ARGS)))
    await _flush()

    assert out.root.isError is False
    assert out.root.content[0].text == send_feedback_result_text()
    feedback = _events(client, "$mcp_feedback")
    assert len(feedback) == 1
    assert "$mcp_parameters" not in feedback[0]["properties"]
    assert _events(client, "$mcp_tool_call") == []


async def test_lowlevel_collision_fails_open_after_listing():
    server = Server("feedback-lowlevel-collision")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name="send_feedback",
                description="A real application tool",
                inputSchema={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        return [mcp_types.TextContent(type="text", text="real tool ran")]

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(collect_feedback=True))

    result = await _list_tools_lowlevel(server)
    assert [t.name for t in result.root.tools].count("send_feedback") == 1

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    out = await call_handler(_call_request("send_feedback", {"note": "hi"}))
    await _flush()

    assert out.root.content[0].text == "real tool ran"
    assert _events(client, "$mcp_feedback") == []
    assert _events(client, "$mcp_tool_call")


async def test_feedback_never_mints_conversation_id():
    server = make_lowlevel()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(collect_feedback=True, enable_conversation_id=True),
    )

    result = await _list_tools_lowlevel(server)
    virtual = [t for t in result.root.tools if t.name == "send_feedback"][0]
    assert "conversation_id" not in virtual.inputSchema["properties"]

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    out = await call_handler(_call_request("send_feedback", dict(_REPORT_ARGS)))
    await _flush()

    feedback = _events(client, "$mcp_feedback")
    assert "$mcp_conversation_id" not in feedback[0]["properties"]
    # No prompt-back block appended to the acknowledgement.
    assert len(out.root.content) == 1


# --- PostHogMCP custom dispatcher ---------------------------------------------------


def make_client(**kwargs):
    client = PostHogMCP("phc_test", host="https://us.i.posthog.com", **kwargs)
    captured = []
    client.capture = lambda event, **kw: captured.append({"event": event, **kw})
    return client, captured


def test_posthogmcp_constructor_fails_fast_on_config_error():
    with pytest.raises(ValueError, match="collides"):
        PostHogMCP(
            "phc_test",
            collect_feedback=CollectFeedbackOptions(
                extra_properties={"llm_model": {"type": "string"}}
            ),
        )


async def test_posthogmcp_prepare_tool_list_appends_descriptor():
    client, _ = make_client(collect_feedback=True)
    tools = [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]

    prepared = client.prepare_tool_list(tools, collect_feedback=True)
    names = [t["name"] for t in prepared]
    assert names == ["search", "send_feedback"]
    virtual = prepared[-1]
    # The virtual tool never gets the injected context argument.
    assert "context" not in virtual["inputSchema"]["properties"]

    # Not appended without the per-call flag, nor over a real tool by the name.
    assert len(client.prepare_tool_list(tools)) == 1
    collided = client.prepare_tool_list(
        [
            {
                "name": "send_feedback",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ],
        collect_feedback=True,
    )
    assert [t["name"] for t in collided] == ["send_feedback"]


async def test_posthogmcp_prepare_tool_list_requires_constructor_option():
    client, _ = make_client()
    tools = [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]
    prepared = client.prepare_tool_list(tools, collect_feedback=True)
    assert [t["name"] for t in prepared] == ["search"]


async def test_posthogmcp_prepare_tool_call_flags_feedback():
    client, _ = make_client(collect_feedback=True)
    call = client.prepare_tool_call("send_feedback", dict(_REPORT_ARGS))
    assert call.is_feedback is True
    assert call.feedback_report is not None
    assert call.feedback_report.feedback_type == "missing_capability"

    ordinary = client.prepare_tool_call("search", {"q": "x"})
    assert ordinary.is_feedback is False and ordinary.feedback_report is None


async def test_posthogmcp_prepare_tool_call_without_opt_in_never_flags():
    client, _ = make_client()
    call = client.prepare_tool_call("send_feedback", dict(_REPORT_ARGS))
    assert call.is_feedback is False and call.feedback_report is None


async def test_posthogmcp_capture_feedback_event_shape():
    client, captured = make_client(collect_feedback=True)
    call = client.prepare_tool_call("send_feedback", dict(_REPORT_ARGS))
    client.capture_feedback(
        report=call.feedback_report,
        distinct_id="user_1",
        session_id="s1",
        llm_model="claude-opus-4-8",
        llm_model_source="self_reported",
        properties={"host_prop": "kept", "$mcp_feedback_type": "spoofed"},
    )
    await _flush()

    events = _events(captured, "$mcp_feedback")
    assert len(events) == 1
    props = events[0]["properties"]
    assert props["$mcp_resource_name"] == "send_feedback"
    # Feedback properties win over the caller's, matching the instrument() path.
    assert props["$mcp_feedback_type"] == "missing_capability"
    assert props["$mcp_intent"].startswith(_REPORT_ARGS["summary"])
    assert props["$mcp_llm_model"] == "claude-opus-4-8"
    assert props["host_prop"] == "kept"
    assert "$mcp_parameters" not in props
    assert events[0]["distinct_id"] == "user_1"


def test_posthogmcp_warns_when_on_feedback_is_set():
    from posthog.mcp import set_logger

    messages = []
    set_logger(messages.append)
    try:
        make_client(
            collect_feedback=CollectFeedbackOptions(on_feedback=lambda report: None)
        )
    finally:
        set_logger(None)
    assert any("on_feedback is ignored" in message for message in messages)


def test_send_feedback_result_shape():
    result = send_feedback_result()
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == send_feedback_result_text()
