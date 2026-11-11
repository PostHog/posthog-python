"""Standalone FastMCP turns a raised tool exception into an ``isError`` result
before the handler we wrap returns. The failure must still carry what was raised
(posthog-python#1223)."""

import asyncio
import gc
import logging
import weakref

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client, FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from posthog.mcp import MCPAnalyticsOptions, instrument  # noqa: E402
from posthog.test.mcp._helpers import FakeClient, events_named, flush_background  # noqa: E402


def _upstream():
    raise ConnectionError("upstream down")


def _chained():
    try:
        _upstream()
    except ConnectionError as exc:
        raise RuntimeError("lookup failed") from exc


def _tool_error():
    raise ToolError("bad input")


def _tool_error_worded_like_fastmcp():
    try:
        _upstream()
    except ConnectionError as exc:
        raise ToolError("Error calling tool: lookup failed") from exc


@pytest.mark.parametrize(
    "raise_in_tool, mask_error_details, error_type, message, chain",
    [
        (
            _chained,
            False,
            "RuntimeError",
            "lookup failed",
            ["RuntimeError", "ConnectionError"],
        ),
        (
            _chained,
            True,
            "RuntimeError",
            "lookup failed",
            ["RuntimeError", "ConnectionError"],
        ),
        (_tool_error, False, "ToolError", "bad input", ["ToolError"]),
        (
            _tool_error_worded_like_fastmcp,
            False,
            "ToolError",
            "Error calling tool: lookup failed",
            ["ToolError", "ConnectionError"],
        ),
    ],
)
async def test_failed_call_carries_the_raised_exception(
    raise_in_tool, mask_error_details, error_type, message, chain
):
    server = FastMCP("example-server", mask_error_details=mask_error_details)

    @server.tool
    def fail() -> str:
        raise_in_tool()
        return "unreachable"

    sink = FakeClient()
    instrument(server, sink, MCPAnalyticsOptions())

    async with Client(server) as client:
        result = await client.call_tool("fail", {}, raise_on_error=False)
    await flush_background()

    assert result.is_error
    [call] = events_named(sink, "$mcp_tool_call")
    assert call["properties"]["$mcp_is_error"] is True
    assert call["properties"]["$mcp_error_type"] == error_type
    assert call["properties"]["$mcp_error_message"] == message
    [exception] = events_named(sink, "$exception")
    exception_list = exception["properties"]["$exception_list"]
    raised = [e for e in exception_list if e["type"] in chain]
    assert [e["type"] for e in raised] == chain
    assert all(e["stacktrace"]["frames"] for e in raised)


@pytest.mark.parametrize(
    "failing_server, instrumented",
    [
        ("same", "wrapper"),
        ("other", "wrapper"),
        ("other", "low_level_only"),
    ],
)
async def test_exception_caught_by_the_tool_is_not_reported(
    failing_server, instrumented
):
    if not hasattr(FastMCP, "call_tool"):
        pytest.skip("fastmcp 2 has no public call_tool for a tool to call")
    server = FastMCP("example-server")
    other = FastMCP("other-server")
    instrument(other, FakeClient(), MCPAnalyticsOptions())
    backend = server if failing_server == "same" else other

    @backend.tool
    def inner() -> str:
        raise RuntimeError("inner failure")

    @server.tool
    async def outer() -> str:
        try:
            await backend.call_tool("inner", {})
        except ToolError:
            pass
        return "recovered"

    sink = FakeClient()
    target = server if instrumented == "wrapper" else server._mcp_server
    instrument(target, sink, MCPAnalyticsOptions())

    async with Client(server) as client:
        result = await client.call_tool("outer", {}, raise_on_error=False)
    await flush_background()

    assert not result.is_error
    [call] = events_named(sink, "$mcp_tool_call")
    assert call["properties"]["$mcp_is_error"] is False
    assert events_named(sink, "$exception") == []


async def test_secrets_in_frame_source_lines_are_redacted():
    server = FastMCP("example-server")

    @server.tool
    def fail() -> str:
        dsn = "postgres://admin:hunter2@db.example.com/app"
        raise RuntimeError(f"connect failed for {dsn.rsplit('@')[-1]}")

    sink = FakeClient()
    instrument(server, sink, MCPAnalyticsOptions())

    async with Client(server) as client:
        await client.call_tool("fail", {}, raise_on_error=False)
    await flush_background()

    [exception] = events_named(sink, "$exception")
    frames = [
        frame
        for entry in exception["properties"]["$exception_list"]
        for frame in entry["stacktrace"]["frames"]
    ]
    assert "db.example.com/app" in repr(frames)
    assert "hunter2" not in repr(frames)


@pytest.fixture
def logging_off():
    # pytest's log capture keeps fastmcp's "Error calling tool" records, and
    # their exc_info would keep the traceback alive on its own.
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


async def test_a_background_task_does_not_keep_the_failure_alive(logging_off):
    server = FastMCP("example-server")
    release = asyncio.Event()
    lingering = []
    payload_refs = []

    class Payload:
        pass

    @server.tool
    async def fail() -> str:
        payload = Payload()
        payload_refs.append(weakref.ref(payload))
        lingering.append(asyncio.get_running_loop().create_task(release.wait()))
        raise RuntimeError("failed")

    sink = FakeClient()
    instrument(server, sink, MCPAnalyticsOptions())

    async with Client(server) as client:
        await client.call_tool("fail", {}, raise_on_error=False)
    await flush_background()
    gc.collect()

    try:
        assert not lingering[0].done()
        assert payload_refs[0]() is None
    finally:
        release.set()
        await lingering[0]
