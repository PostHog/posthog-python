"""Tests for jlowin's standalone FastMCP 2.0 (the `fastmcp` package), distinct
from the official SDK's mcp.server.fastmcp.FastMCP."""

import inspect

import pytest

pytest.importorskip("fastmcp")

import mcp.types as mcp_types  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

from posthog.mcp import instrument  # noqa: E402
from posthog.mcp.types import MCPAnalyticsOptions  # noqa: E402
from posthog.test.mcp._helpers import (  # noqa: E402
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)


def make_server():
    server = FastMCP("jlowin-probe")

    @server.tool
    def add(a: int, b: int) -> int:
        return a + b

    return server


def _tool_result_type():
    try:
        from fastmcp.tools.tool import ToolResult
    except ImportError:
        pytest.skip("this FastMCP predates ToolResult")
    return ToolResult


async def _list(server):
    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    return await handler(mcp_types.ListToolsRequest(method="tools/list"))


async def _call(server, name, arguments):
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]
    return await handler(
        mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
    )


async def test_jlowin_list_injects_context():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list(server)
    await _flush()

    add_tool = next(t for t in result.root.tools if t.name == "add")
    assert "context" in add_tool.inputSchema["properties"]
    assert _events(client, "$mcp_tools_list")


async def test_jlowin_call_strips_context_so_validation_passes():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    out = await _call(
        server,
        "add",
        {"a": 2, "b": 3, "context": "summing two numbers for the monthly report"},
    )
    await _flush()

    # Without stripping, jlowin rejects the extra `context` kwarg with isError.
    assert out.root.isError is False
    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_tool_name"] == "add"
    assert (
        calls[0]["properties"]["$mcp_intent"]
        == "summing two numbers for the monthly report"
    )
    assert calls[0]["properties"]["$mcp_is_error"] is False


async def test_jlowin_tool_owning_context_keeps_its_real_argument():
    # A v2 tool that declares its own `context` must NOT have it stripped (we only
    # strip the keys we inject). Works without a prior tools/list, since ownership is
    # read from the tool's signature — important for stateless per-request servers.
    server = FastMCP("v2-ctx-owner")

    @server.tool
    def summarize(text: str, context: str) -> str:
        return f"{text}|ctx={context}"

    client = FakeClient()
    instrument(server, client)

    out = await _call(
        server, "summarize", {"text": "hi", "context": "the user's real context"}
    )
    await _flush()

    assert out.root.isError is False
    texts = [c.text for c in out.root.content if getattr(c, "type", None) == "text"]
    assert any("ctx=the user's real context" in t for t in texts)


async def test_jlowin_report_missing_advertises_get_more_tools():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(report_missing=True))

    result = await _list(server)
    assert "get_more_tools" in [t.name for t in result.root.tools]

    out = await _call(
        server, "get_more_tools", {"context": "need a tool that exports to CSV"}
    )
    await _flush()
    assert out.root.isError is False
    assert _events(client, "$mcp_missing_capability")


async def test_strict_input_validation_still_accepts_calls():
    """This adapter strips the injected parameters before the SDK validates, so
    the advertised schema must not mark them required — otherwise every call
    under `strict_input_validation=True` fails on a parameter the SDK never
    sees."""
    import mcp.types as t

    server = FastMCP("strict", strict_input_validation=True)

    @server.tool()
    def echo(msg: str) -> str:
        return msg

    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(enable_conversation_id=True))

    low = server._mcp_server
    await low.request_handlers[t.ListToolsRequest](
        t.ListToolsRequest(method="tools/list")
    )
    result = await low.request_handlers[t.CallToolRequest](
        t.CallToolRequest(
            method="tools/call",
            params=t.CallToolRequestParams(
                name="echo", arguments={"msg": "hi", "context": "strict validation"}
            ),
        )
    )
    await _flush()

    assert result.root.isError is False, result.root.content[0].text
    assert _events(client, "$mcp_tool_call")[0]["properties"]["$mcp_intent"] == (
        "strict validation"
    )


@pytest.mark.parametrize("listed", [True, False], ids=["listed", "cold"])
async def test_jlowin_call_strips_llm_model_and_records_it(listed):
    # Ownership comes from the registered schema, so a cold instance (one that
    # never served the listing that advertised llm_model, as in a multi-replica
    # deployment) strips it before jlowin validates and still records it.
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    if listed:
        await _list(server)
    out = await _call(
        server, "add", {"a": 2, "b": 3, "context": "sum", "llm_model": "model-a"}
    )
    await _flush()

    assert out.root.isError is False
    calls = _events(client, "$mcp_tool_call")
    assert calls[0]["properties"]["$mcp_llm_model"] == "model-a"


async def test_jlowin_middleware_defaults_work_across_fresh_instances(monkeypatch):
    from types import SimpleNamespace

    from fastmcp.server.middleware import Middleware

    from posthog.mcp import _instrument_lowlevel

    monkeypatch.setattr(
        _instrument_lowlevel,
        "_request_context",
        lambda _: SimpleNamespace(meta={"x-codex-turn-metadata": {"model": "gpt-5"}}),
    )
    client = FakeClient()

    class PassThrough(Middleware):
        async def on_call_tool(self, context, call_next):
            return await call_next(context)

    def fresh():
        server = make_server()
        server.add_middleware(PassThrough())
        instrument(server, client)
        return server

    listing = await _list(fresh())
    schema = listing.root.tools[0].inputSchema
    # A cold replica cannot distinguish pass-through from tool-replacing
    # middleware, so discovery must not request an argument it cannot strip.
    assert "llm_model" not in schema["properties"]
    result = await _call(fresh(), "add", {"a": 2, "b": 3, "context": "sum"})
    assert result.root.isError is False
    assert result.root.content[0].text == "5"
    await _flush()
    event = _events(client, "$mcp_tool_call")[0]["properties"]
    assert event["$mcp_llm_model"] == "gpt-5"
    assert event["$mcp_llm_model_source"] == "client_metadata"


_OWN_MODEL = {"llm_model": {"type": "string"}}


@pytest.mark.parametrize("listed", [True, False], ids=["listed", "cold"])
@pytest.mark.parametrize("capture_model", [True, False])
@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "object", "properties": _OWN_MODEL, "required": ["llm_model"]},
        {
            "allOf": [
                {"type": "object", "properties": _OWN_MODEL, "required": ["llm_model"]}
            ]
        },
    ],
    ids=["properties", "allOf"],
)
async def test_jlowin_schema_declared_llm_model_is_kept(
    capture_model, parameters, listed
):
    # A Tool subclass declares its arguments in `parameters` and has no `fn`.
    # Its own llm_model must reach the tool and must never be read as the
    # model, with or without a prior listing.
    from fastmcp.tools import Tool

    ToolResult = _tool_result_type()

    class Router(Tool):
        async def run(self, arguments):
            return ToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=arguments["llm_model"])
                ]
            )

    server = FastMCP("jlowin-schema-owner")
    server.add_tool(Router(name="route", parameters=parameters))
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(capture_model=capture_model))

    if listed:
        await _list(server)
    out = await _call(server, "route", {"llm_model": "gpt-5", "context": "routing"})
    await _flush()

    assert out.root.isError is False
    assert out.root.content[0].text == "gpt-5"
    assert "$mcp_llm_model" not in _events(client, "$mcp_tool_call")[0]["properties"]


@pytest.mark.parametrize(
    "declares_model",
    ["referenced", "sibling", "chained", "escaped", None],
    ids=["referenced", "sibling", "chained", "escaped", "plain"],
)
async def test_jlowin_root_ref_schema_is_dereferenced_for_ownership(declares_model):
    # FastMCP dereferences a root `$ref` before the client sees the listing, so
    # ownership must be read from every node along the reference chain and from
    # the root's own sibling properties: a key declared in any of them stays,
    # and an undeclared llm_model is stripped so validation passes.
    from fastmcp.tools import Tool

    ToolResult = _tool_result_type()

    properties = {"a": {"type": "integer"}}
    extra = {}
    if declares_model == "referenced":
        properties["llm_model"] = {"type": "string"}
    if declares_model == "sibling":
        extra = {"properties": {"llm_model": {"type": "string"}}}
    if declares_model == "escaped":
        # A JSON Pointer escapes "/" as "~1"; FastMCP resolves it, so must we.
        extra = {
            "$ref": "#/$defs/Args~1Input",
            "$defs": {
                "Args/Input": {
                    "type": "object",
                    "properties": {**properties, "llm_model": {"type": "string"}},
                }
            },
        }
    if declares_model == "chained":
        extra = {
            "$defs": {
                "Args": {"$ref": "#/$defs/Real"},
                "Real": {
                    "type": "object",
                    "properties": {**properties, "llm_model": {"type": "string"}},
                },
            }
        }

    class Echo(Tool):
        async def run(self, arguments):
            return ToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=",".join(sorted(arguments)))
                ]
            )

    options = {}
    if declares_model == "chained":
        # FastMCP's own dereferencer recurses forever on a chained reference,
        # instrumented or not; the chain case only exists with it turned off.
        if "dereference_schemas" not in inspect.signature(FastMCP.__init__).parameters:
            pytest.skip("this FastMCP cannot disable schema dereferencing")
        options = {"dereference_schemas": False}
    server = FastMCP("jlowin-root-ref", **options)
    server.add_tool(
        Echo(
            name="echo",
            parameters={
                "$ref": "#/$defs/Args",
                "$defs": {"Args": {"type": "object", "properties": properties}},
                **extra,
            },
        )
    )
    client = FakeClient()
    instrument(server, client)

    out = await _call(server, "echo", {"a": 1, "context": "c", "llm_model": "gpt-5"})
    await _flush()

    assert out.root.isError is False
    # A FastMCP that never dereferences advertises the `$ref` itself, which the
    # listing never injects into, so the plain case keeps the argument there.
    dereferences = (
        "dereference_schemas" in inspect.signature(FastMCP.__init__).parameters
    )
    stripped = declares_model is None and dereferences
    assert out.root.content[0].text == ("a" if stripped else "a,llm_model")
    recorded = _events(client, "$mcp_tool_call")[0]["properties"].get("$mcp_llm_model")
    assert recorded == ("gpt-5" if stripped else None)


async def test_jlowin_ownership_follows_the_dispatched_tool_version():
    # A request may pin a version in `_meta`; only a FastMCP that exposes
    # `extract_version_spec` honours it when dispatching, every other release
    # calls the highest version. Ownership must strip for the version that
    # actually runs, or the call fails validation.
    pytest.importorskip("fastmcp.utilities.versions")
    try:
        from fastmcp.server.dependencies import extract_version_spec  # noqa: F401

        expected = "v1:gpt-5"
    except ImportError:
        expected = "v2"
    server = FastMCP("jlowin-versions")

    @server.tool(name="route", version="1")
    def route_v1(prompt: str, llm_model: str) -> str:
        return f"v1:{llm_model}"

    @server.tool(name="route", version="2")
    def route_v2(prompt: str) -> str:
        return "v2"

    client = FakeClient()
    instrument(server, client)
    handler = server._mcp_server.request_handlers[mcp_types.CallToolRequest]

    out = await handler(
        mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="route",
                arguments={"prompt": "p", "context": "c", "llm_model": "gpt-5"},
                **{"_meta": {"fastmcp": {"version": "1"}}},
            ),
        )
    )
    await _flush()

    assert out.root.isError is False, out.root.content
    assert out.root.content[0].text == expected
    recorded = _events(client, "$mcp_tool_call")[0]["properties"].get("$mcp_llm_model")
    assert recorded == (None if expected.startswith("v1") else "gpt-5")


async def test_jlowin_disabled_capture_leaves_llm_model_for_permissive_tools():
    # With capture off nothing injected llm_model, so it is the application's
    # even when the schema does not declare it: a Tool accepting arbitrary keys
    # must still receive it.
    from fastmcp.tools import Tool

    ToolResult = _tool_result_type()

    class Bag(Tool):
        async def run(self, arguments):
            return ToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=",".join(sorted(arguments)))
                ]
            )

    server = FastMCP("jlowin-permissive")
    server.add_tool(
        Bag(
            name="bag",
            parameters={"type": "object", "additionalProperties": {"type": "string"}},
        )
    )
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(capture_model=False))

    out = await _call(server, "bag", {"llm_model": "mine", "context": "c"})
    await _flush()

    assert out.root.isError is False
    assert out.root.content[0].text == "llm_model"
    assert "$mcp_llm_model" not in _events(client, "$mcp_tool_call")[0]["properties"]


@pytest.mark.parametrize("listed", [True, False], ids=["listed", "cold"])
async def test_jlowin_middleware_provided_tool_keeps_its_llm_model(listed):
    # The registry does not know a middleware-provided tool, so the effective
    # listing may differ from dispatch. Model injection stays off and the
    # application argument stays intact, even without a prior listing.
    pytest.importorskip("fastmcp.server.middleware")
    from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
    from fastmcp.tools import Tool

    def route(prompt: str, llm_model: str) -> str:
        return llm_model

    server = FastMCP("jlowin-middleware-tool")
    server.add_middleware(ToolInjectionMiddleware(tools=[Tool.from_function(route)]))
    client = FakeClient()
    instrument(server, client)

    if listed:
        await _list(server)
    out = await _call(
        server, "route", {"prompt": "p", "context": "c", "llm_model": "own"}
    )
    await _flush()

    assert out.root.isError is False, out.root.content
    assert out.root.content[0].text == "own"
    recorded = _events(client, "$mcp_tool_call")[0]["properties"].get("$mcp_llm_model")
    assert recorded is None


@pytest.mark.parametrize(
    "shadow, listed",
    [
        ("listing", True),
        ("listing", False),
        ("dispatch", False),
        ("dispatch", True),
        ("listing-last", True),
        ("late-dispatch", False),
        ("builtin-subclass", False),
    ],
    ids=[
        "listing-listed",
        "listing-cold",
        "dispatch-cold",
        "dispatch-listed",
        "listing-last",
        "late-dispatch",
        "builtin-subclass-cold",
    ],
)
async def test_jlowin_middleware_shadowed_tool_keeps_its_llm_model(shadow, listed):
    # A registered tool without llm_model is shadowed by middleware serving one
    # that declares it, either by also advertising it or only at dispatch. The
    # registry would answer for the wrong tool, so with such middleware present
    # it is not trusted: the argument stays and is not read as a self-report,
    # regardless of listing order or a stale registered-tool verdict.
    pytest.importorskip("fastmcp.server.middleware")
    from fastmcp.server.middleware import Middleware
    from fastmcp.server.middleware.tool_injection import ToolInjectionMiddleware
    from fastmcp.tools import Tool

    server = FastMCP("jlowin-shadow")

    @server.tool(name="route")
    def registered(prompt: str) -> str:
        return "registered"

    def route(prompt: str, llm_model: str) -> str:
        return llm_model

    shadowing = Tool.from_function(route)

    class DispatchShadow(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            return [*tools, shadowing] if shadow == "listing-last" else tools

        async def on_call_tool(self, context, call_next):
            if context.message.name == "route":
                return await shadowing.run(context.message.arguments or {})
            return await call_next(context)

    if shadow == "builtin-subclass":
        # An application subclass of one of FastMCP's own middleware is still
        # the application's: its overrides count.
        builtins = [type(m) for m in FastMCP("probe").middleware]
        if not builtins:
            pytest.skip("this FastMCP installs no built-in middleware")
        builtin = builtins[0]

        class DispatchShadow(builtin):  # type: ignore[no-redef,misc]
            async def on_call_tool(self, context, call_next):
                if context.message.name == "route":
                    return await shadowing.run(context.message.arguments or {})
                return await call_next(context)

    client = FakeClient()
    instrument(server, client)
    if shadow == "late-dispatch":
        await _list(server)
    server.add_middleware(
        ToolInjectionMiddleware(tools=[shadowing])
        if shadow == "listing"
        else DispatchShadow()
    )
    if listed:
        await _list(server)
    out = await _call(
        server, "route", {"prompt": "p", "context": "c", "llm_model": "own"}
    )
    await _flush()

    assert out.root.isError is False, out.root.content
    assert out.root.content[0].text == "own"
    recorded = _events(client, "$mcp_tool_call")[0]["properties"].get("$mcp_llm_model")
    assert recorded is None


async def test_jlowin_without_dereferencing_a_root_ref_is_never_injected_into():
    # With dereferencing off the client sees the `$ref` itself, which the
    # listing never injects llm_model into, so a cold call must not strip it
    # from a permissive tool either.
    from fastmcp.tools import Tool

    ToolResult = _tool_result_type()

    if "dereference_schemas" not in inspect.signature(FastMCP.__init__).parameters:
        pytest.skip("this FastMCP cannot disable schema dereferencing")

    class Bag(Tool):
        async def run(self, arguments):
            return ToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=",".join(sorted(arguments)))
                ]
            )

    server = FastMCP("jlowin-raw-ref", dereference_schemas=False)
    server.add_tool(
        Bag(
            name="bag",
            parameters={
                "$ref": "#/$defs/Args",
                "$defs": {"Args": {"type": "object", "additionalProperties": True}},
            },
        )
    )
    client = FakeClient()
    instrument(server, client)

    out = await _call(server, "bag", {"llm_model": "mine", "context": "c"})
    await _flush()

    assert out.root.isError is False
    assert out.root.content[0].text == "llm_model"


async def test_jlowin_cold_call_survives_a_fastmcp_without_middleware(monkeypatch):
    # FastMCP releases before middleware existed have no
    # `fastmcp.server.middleware`; ownership inference must not import its way
    # into breaking dispatch, and the registry is then simply trusted.
    import sys

    monkeypatch.setitem(sys.modules, "fastmcp.server.middleware", None)
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    out = await _call(
        server, "add", {"a": 2, "b": 3, "context": "sum", "llm_model": "model-a"}
    )
    await _flush()

    assert out.root.isError is False, out.root.content
    assert (
        _events(client, "$mcp_tool_call")[0]["properties"]["$mcp_llm_model"]
        == "model-a"
    )


async def test_a_failed_registry_lookup_delegates_instead_of_swallowing():
    # A real tool of the host's owns the virtual tool's name, so the SDK must
    # never answer that call itself. fastmcp resolves a tool through a provider
    # chain that can reach a mounted or proxied upstream over the network, so
    # `get_tool` raising means "could not look it up", not "the name is free" --
    # answering False there swallowed the host's tool and returned PostHog's
    # canned reply as a success.
    server = FastMCP("jlowin-flaky-registry")

    @server.tool
    def get_more_tools(context: str) -> str:
        return "real tool ran"

    client = FakeClient()
    messages = []
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            report_missing=True, capture_model=False, logger=messages.append
        ),
    )

    await _list(server)

    original_get_tool = server.get_tool
    failed = []

    async def flaky_get_tool(name, *args, **kwargs):
        # Transient, as a network blip is: the SDK's ownership lookup hits it,
        # the host's own dispatch that follows does not.
        if name == "get_more_tools" and not failed:
            failed.append(name)
            raise ConnectionError("upstream provider unreachable")
        return await original_get_tool(name, *args, **kwargs)

    server.get_tool = flaky_get_tool

    out = await _call(server, "get_more_tools", {"context": "need csv export"})
    await _flush()

    assert "real tool ran" in str(out.root.content[0].text)
    assert _events(client, "$mcp_missing_capability") == []
    assert any("delegating the call to your server" in m for m in messages)
