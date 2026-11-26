"""End-to-end tests for the FastMCP adapter (Milestone 2)."""

import asyncio
import gc
import os
import weakref
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP

from posthog.mcp import instrument, set_logger
from posthog.mcp.types import MCPAnalyticsOptions, UserIdentity
from posthog.test.mcp._helpers import (
    FakeClient,
    events_named as _events,
    flush_background as _flush,
)


def make_server():
    server = FastMCP("test-server")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    def boom() -> str:
        raise ValueError("explode")

    return server


async def _list_tools(server):
    handler = server._mcp_server.request_handlers[mcp_types.ListToolsRequest]
    return await handler(mcp_types.ListToolsRequest(method="tools/list"))


# --- tools/list --------------------------------------------------------------


async def test_list_tools_injects_context_and_captures():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    result = await _list_tools(server)
    await _flush()

    tools = result.root.tools
    add_tool = next(t for t in tools if t.name == "add")
    assert "context" in add_tool.inputSchema["properties"]
    assert "context" in add_tool.inputSchema["required"]

    listed = _events(client, "$mcp_tools_list")
    assert listed
    assert set(listed[0]["properties"]["$mcp_listed_tool_names"]) == {"add", "boom"}


async def test_context_injection_can_be_disabled():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(context=False))

    result = await _list_tools(server)
    add_tool = next(t for t in result.root.tools if t.name == "add")
    assert "context" not in add_tool.inputSchema.get("properties", {})


async def test_list_tools_injects_model_into_real_and_virtual_tools():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(capture_model=True, report_missing=True),
    )

    result = await _list_tools(server)
    tools = {tool.name: tool for tool in result.root.tools}

    for name in ("add", "boom", "get_more_tools"):
        assert "llm_model" in tools[name].inputSchema["properties"]
        assert "llm_model" not in tools[name].inputSchema.get("required", [])


async def test_virtual_tool_uses_conversation_id():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(report_missing=True, enable_conversation_id=True),
    )

    listed = await _list_tools(server)
    tool = next(t for t in listed.root.tools if t.name == "get_more_tools")
    assert "conversation_id" in tool.inputSchema["properties"]

    result = await server._tool_manager.call_tool("get_more_tools", {"context": "csv"})
    await _flush()

    handle = _events(client, "$mcp_missing_capability")[0]["properties"][
        "$mcp_conversation_id"
    ]
    assert any(handle in item.text for item in result)


# --- tools/call --------------------------------------------------------------


async def test_tool_call_captures_intent_and_strips_context():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    # Prime the listing metadata used by the shared call lifecycle.
    await _list_tools(server)

    received = {}
    original_add = server._tool_manager.get_tool("add").fn

    def spy_add(a: int, b: int) -> int:
        received["args"] = {"a": a, "b": b}
        return original_add(a, b)

    server._tool_manager.get_tool("add").fn = spy_add

    await server._tool_manager.call_tool(
        "add", {"a": 2, "b": 3, "context": "summing two numbers for the user's report"}
    )
    await _flush()

    # the injected `context` never reached the tool implementation
    assert received["args"] == {"a": 2, "b": 3}

    calls = _events(client, "$mcp_tool_call")
    assert len(calls) == 1
    props = calls[0]["properties"]
    assert props["$mcp_tool_name"] == "add"
    assert props["$mcp_tool_description"] == "Add two numbers."
    assert props["$mcp_intent"] == "summing two numbers for the user's report"
    assert props["$mcp_intent_source"] == "context_parameter"
    assert props["$mcp_is_error"] is False
    assert "$mcp_duration_ms" in props
    # context is stripped from captured parameters too
    assert "context" not in props["$mcp_parameters"]["request"]["params"]["arguments"]


async def test_tool_call_captures_client_model_without_prior_listing():
    server = make_server()
    client = FakeClient()
    instrument(server, client, MCPAnalyticsOptions(capture_model=True))

    context = SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"x-codex-turn-metadata": {"model": "gpt-5.6-sol"}}
        )
    )
    result = await server._tool_manager.call_tool(
        "add",
        {"a": 2, "b": 3, "llm_model": "claude-opus-4-8"},
        context=context,
    )
    await _flush()

    assert result == 5
    props = _events(client, "$mcp_tool_call")[0]["properties"]
    assert props["$mcp_llm_model"] == "gpt-5.6-sol"
    assert props["$mcp_llm_model_source"] == "client_metadata"
    assert "llm_model" not in props["$mcp_parameters"]["request"]["params"]["arguments"]


async def test_analytics_flush_sends_its_captures():
    async def slow_before_send(event):
        await asyncio.sleep(0.05)
        return event

    server = make_server()
    client = FakeClient()
    analytics = instrument(
        server, client, MCPAnalyticsOptions(before_send=slow_before_send)
    )

    await server._tool_manager.call_tool(
        "add", {"a": 2, "b": 3, "context": "summing two numbers"}
    )
    assert _events(client, "$mcp_tool_call") == []

    await analytics.flush()

    assert len(_events(client, "$mcp_tool_call")) == 1
    assert client.event_counts_at_flush == [len(client.events)]


class _FailingFlushClient(FakeClient):
    def flush(self, timeout_seconds=10):
        raise RuntimeError("network down")


def _fail_thread_start(self):
    raise RuntimeError("can't start new thread")


@pytest.mark.parametrize(
    "client_class, thread_start, expected_log",
    [
        (_FailingFlushClient, None, "network down"),
        (FakeClient, _fail_thread_start, "can't start new thread"),
    ],
    ids=["client flush raises", "thread start raises"],
)
async def test_analytics_flush_logs_instead_of_raising(
    monkeypatch, client_class, thread_start, expected_log
):
    if thread_start is not None:
        monkeypatch.setattr(threading.Thread, "start", thread_start)
    lines = []
    try:
        analytics = instrument(
            make_server(), client_class(), MCPAnalyticsOptions(logger=lines.append)
        )
        await analytics.flush()
    finally:
        set_logger(None)

    assert any(expected_log in line for line in lines)


async def test_cancelled_analytics_flush_leaves_the_worker_quiet(monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    thread_errors = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)

    class StalledFlushClient(FakeClient):
        def flush(self, timeout_seconds=10):
            release.wait(timeout=5)
            finished.set()

    analytics = instrument(make_server(), StalledFlushClient())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(analytics.flush(), 0.05)
    release.set()
    assert finished.wait(timeout=2)
    await asyncio.sleep(0.05)

    assert thread_errors == []


async def test_analytics_flush_leaves_the_default_executor_free():
    release = threading.Event()

    class StalledFlushClient(FakeClient):
        def flush(self, timeout_seconds=10):
            release.wait(timeout=5)

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    analytics = instrument(make_server(), StalledFlushClient())
    flushing = asyncio.ensure_future(analytics.flush())
    await asyncio.sleep(0.05)
    try:
        assert await asyncio.wait_for(loop.run_in_executor(None, lambda: 1), 1) == 1
    finally:
        release.set()
        await flushing


async def test_analytics_flush_does_not_queue_behind_stalled_flushes():
    release = threading.Event()

    class StalledFlushClient(FakeClient):
        def flush(self, timeout_seconds=10):
            release.wait(timeout=5)

    stalled = [instrument(make_server(), StalledFlushClient()) for _ in range(64)]
    healthy_client = FakeClient()
    healthy = instrument(make_server(), healthy_client)
    flushing = [asyncio.ensure_future(analytics.flush()) for analytics in stalled]
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(healthy.flush(), 1)
        assert healthy_client.event_counts_at_flush == [0]
    finally:
        release.set()
        await asyncio.gather(*flushing)


async def test_overlapping_analytics_flushes_run_one_client_flush_at_a_time():
    release = threading.Event()
    lock = threading.Lock()

    class StalledFlushClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.running = 0
            self.most_running = 0

        def flush(self, timeout_seconds=10):
            with lock:
                self.running += 1
                self.most_running = max(self.most_running, self.running)
            release.wait(timeout=5)
            with lock:
                self.running -= 1

    client = StalledFlushClient()
    handles = [instrument(make_server(), client) for _ in range(20)]
    flushing = [asyncio.ensure_future(analytics.flush()) for analytics in handles]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(asyncio.gather(*flushing), 2)

    assert client.most_running == 1


async def test_a_failed_analytics_flush_lets_the_client_be_collected():
    client = _FailingFlushClient()
    analytics = instrument(make_server(), client)
    await analytics.flush()
    client_ref = weakref.ref(client)
    for thread in threading.enumerate():
        if thread.name == "posthog-mcp-flush":
            thread.join(timeout=2)
    await asyncio.sleep(0)

    del client, analytics
    gc.collect()

    assert client_ref() is None


async def test_a_failed_earlier_flush_does_not_reach_the_loop_exception_handler():
    loop_errors = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    release = threading.Event()

    class StalledFailingClient(FakeClient):
        def flush(self, timeout_seconds=10):
            release.wait(timeout=5)
            raise RuntimeError("network down")

    client = StalledFailingClient()
    first = asyncio.ensure_future(instrument(make_server(), client).flush())
    await asyncio.sleep(0.05)
    second = asyncio.ensure_future(instrument(make_server(), client).flush())
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    gc.collect()
    await asyncio.sleep(0)

    assert loop_errors == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
async def test_analytics_flush_works_in_a_forked_child():
    analytics = instrument(make_server(), FakeClient())
    await analytics.flush()

    pid = os.fork()
    if pid == 0:
        try:
            asyncio.run(asyncio.wait_for(analytics.flush(), 2))
            os._exit(0)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


async def test_initialize_emitted_once_per_session():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    await server._tool_manager.call_tool(
        "add", {"a": 1, "b": 1, "context": "first call to warm up"}
    )
    await server._tool_manager.call_tool(
        "add", {"a": 2, "b": 2, "context": "second call for the total"}
    )
    await _flush()

    assert len(_events(client, "$mcp_initialize")) == 1
    assert len(_events(client, "$mcp_tool_call")) == 2


async def test_tool_call_error_is_captured_and_reraised():
    server = make_server()
    client = FakeClient()
    instrument(server, client)

    with pytest.raises(Exception):
        await server._tool_manager.call_tool(
            "boom", {"context": "attempting the risky operation"}
        )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls and calls[0]["properties"]["$mcp_is_error"] is True
    exceptions = _events(client, "$exception")
    assert exceptions
    # Canonical order: the caught/outermost exception (fastmcp's ToolError
    # wrapper) is first, the root cause last.
    exception_list = exceptions[0]["properties"]["$exception_list"]
    assert exception_list[0]["value"] == "Error executing tool boom: explode"
    assert exception_list[-1]["value"] == "explode"


async def test_identify_sets_distinct_id_and_groups():
    server = make_server()
    client = FakeClient()
    instrument(
        server,
        client,
        MCPAnalyticsOptions(
            identify=lambda request, extra: UserIdentity(
                distinct_id="user_42",
                properties={"plan": "pro"},
                groups={"organization": "org_7"},
            )
        ),
    )

    await server._tool_manager.call_tool(
        "add", {"a": 1, "b": 2, "context": "checking identity flows through"}
    )
    await _flush()

    calls = _events(client, "$mcp_tool_call")
    assert calls[0]["distinct_id"] == "user_42"
    assert calls[0]["properties"]["$groups"] == {"organization": "org_7"}
    assert calls[0]["options"] == {}
    assert not _events(client, "$identify")


async def test_instrument_is_idempotent():
    server = make_server()
    client = FakeClient()
    instrument(server, client)
    wrapped_call = server._tool_manager.call_tool
    instrument(server, client)
    assert server._tool_manager.call_tool is wrapped_call  # not double-wrapped


async def test_unsupported_server_returns_noop_handle():
    handle = instrument(object(), FakeClient())
    # graceful no-op: capture and flush do nothing and do not raise
    await handle.capture("anything")
    await handle.flush()


async def test_callbacks_can_read_headers_through_the_helper():
    """Same callback body as the v2 lane's equivalent test: `extra["ctx"]` is the
    SDK's own per-request context on both majors, read via `get_request_headers`."""
    from types import SimpleNamespace

    from posthog.mcp import get_request_headers

    server = make_server()
    client = FakeClient()
    seen = {}

    def identify(request, extra):
        seen["headers"] = get_request_headers(extra)
        return None

    instrument(server, client, MCPAnalyticsOptions(identify=identify))

    # FastMCP hands the tool a Context whose .request_context carries the request.
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"Authorization": "Bearer t0ken"}),
            session=SimpleNamespace(client_params=None),
        )
    )
    await server._tool_manager.call_tool(
        "add", {"a": 1, "b": 1, "context": "header read"}, context=context
    )
    await _flush()

    assert seen["headers"] == {"authorization": "Bearer t0ken"}


async def test_public_call_tool_entrypoint_still_works_outside_a_request():
    """`FastMCP.call_tool()` is public API for in-process invocation, where there
    is no request context. `Context.request_context` *raises* there, so reading
    it unguarded would push an analytics error into the customer's tool path."""
    server = make_server()
    instrument(server, FakeClient())

    result = await server.call_tool("add", {"a": 2, "b": 3, "context": "no request"})
    await _flush()

    text_blocks = [c.text for c in result[0] if getattr(c, "type", None) == "text"]
    assert "5" in text_blocks


async def test_a_failed_registry_lookup_delegates_instead_of_swallowing():
    # A lookup that raises means "could not answer", not "the name is free".
    # Answering False there swallows the host's own tool and returns PostHog's
    # canned reply as a success. Same contract as the low-level adapter.
    server = FastMCP("flaky-registry")

    @server.tool()
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

    original_get_tool = server._tool_manager.get_tool
    failed = []

    def flaky_get_tool(name, *args, **kwargs):
        if name == "get_more_tools" and not failed:
            failed.append(name)
            raise ConnectionError("registry unreachable")
        return original_get_tool(name, *args, **kwargs)

    server._tool_manager.get_tool = flaky_get_tool

    out = await server._tool_manager.call_tool(
        "get_more_tools", {"context": "need csv export"}
    )
    await _flush()

    assert "real tool ran" in str(out)
    assert _events(client, "$mcp_missing_capability") == []
    assert any("delegating the call to your server" in m for m in messages)
