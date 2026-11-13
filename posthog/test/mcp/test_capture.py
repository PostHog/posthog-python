"""Custom event handles must use the same privacy and identity pipeline as auto capture."""

import pytest
from mcp.server.lowlevel import Server

from posthog.mcp import McpAnalytics, instrument
from posthog.mcp._internal import get_server_tracking_data
from posthog.mcp.types import UserIdentity
from posthog.test.mcp._helpers import FakeClient


async def test_custom_capture_is_awaited_and_preserves_session_identity():
    server = Server("custom-capture")
    client = FakeClient()
    handle = instrument(server, client)
    data = get_server_tracking_data(server)
    data.identified_sessions.set(
        data.session_id,
        UserIdentity(
            distinct_id="user-7", properties={"plan": "pro"}, groups={"org": "acme"}
        ),
    )

    await handle.capture("report_exported", {"format": "csv"})

    assert len(client.events) == 1
    event = client.events[0]
    assert event["event"] == "report_exported"
    assert event["distinct_id"] == "user-7"
    assert event["properties"]["$session_id"] == data.session_id
    assert event["properties"]["$set"] == {"plan": "pro"}
    assert event["properties"]["$groups"] == {"org": "acme"}
    assert event["properties"]["format"] == "csv"
    assert "$process_person_profile" not in event["properties"]


@pytest.mark.parametrize("name", [None, "", 3])
async def test_custom_capture_rejects_invalid_event_names(name):
    server = Server("custom-capture")
    client = FakeClient()
    handle = instrument(server, client)

    with pytest.raises(ValueError, match="requires an event name"):
        await handle.capture(name)

    assert client.events == []


async def test_custom_capture_after_tracking_removed_is_a_noop():
    server = Server("custom-capture")
    client = FakeClient()
    instrument(server, client)
    # A handle without tracking state is inert, not a fallback to another server.
    handle = McpAnalytics(Server("untracked"))

    await handle.capture("report_exported")

    assert client.events == []


async def test_custom_capture_with_no_sink_is_a_noop():
    server = Server("custom-capture")
    client = FakeClient()
    handle = instrument(server, client)
    get_server_tracking_data(server).sink = None

    await handle.capture("report_exported")

    assert client.events == []
