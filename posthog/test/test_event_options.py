import logging

import pytest

from posthog import AsyncPosthog
from posthog.capture_event import _to_v1_event
from posthog.client import Client
from posthog.test.capture_helpers import (
    patch_async_capture_send,
    patch_capture_send,
    sent_events,
)
from posthog.test.test_utils import FAKE_TEST_API_KEY

OPTIONS = {"cookieless_mode": True, "future_option": "kept"}

CAPTURE_CALLS = {
    "capture": lambda c, o: c.capture("e", distinct_id="u", options=o),
    "capture_ai": lambda c, o: c.capture_ai(
        "$ai_generation", distinct_id="u", options=o
    ),
    "set": lambda c, o: c.set(distinct_id="u", properties={"a": 1}, options=o),
    "set_once": lambda c, o: c.set_once(
        distinct_id="u", properties={"a": 1}, options=o
    ),
    "group_identify": lambda c, o: c.group_identify(
        "company", "acme", distinct_id="u", options=o
    ),
    "alias": lambda c, o: c.alias("previous", "u", options=o),
    "capture_exception": lambda c, o: c.capture_exception(
        ValueError("boom"), distinct_id="u", options=o
    ),
}
ASYNC_CAPTURE_CALLS = {k: v for k, v in CAPTURE_CALLS.items() if k != "capture_ai"}


def _sync_wire_events(call, before_send=None) -> list[dict]:
    with patch_capture_send("client") as send:
        client = Client(FAKE_TEST_API_KEY, sync_mode=True, before_send=before_send)
        assert call(client) is not None
    return [_to_v1_event(msg) for msg in sent_events(send)]


async def _async_wire_events(call, before_send=None) -> list[dict]:
    batches: list[list[dict]] = []

    async def send_batch(api_key, host, batch, **kwargs):
        batches.append(batch)

    with patch_async_capture_send(side_effect=send_batch):
        async with AsyncPosthog("test-key", before_send=before_send) as client:
            assert call(client) is not None
            await client.flush(timeout_seconds=1)
    return [_to_v1_event(msg) for batch in batches for msg in batch]


@pytest.mark.parametrize("method", list(CAPTURE_CALLS))
def test_sync_options_reach_the_wire(method):
    events = _sync_wire_events(lambda c: CAPTURE_CALLS[method](c, OPTIONS))
    assert [e["options"] for e in events] == [OPTIONS]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(ASYNC_CAPTURE_CALLS))
async def test_async_options_reach_the_wire(method):
    events = await _async_wire_events(lambda c: ASYNC_CAPTURE_CALLS[method](c, OPTIONS))
    assert [e["options"] for e in events] == [OPTIONS]


def _hook(msg):
    properties = {k: v for k, v in msg["properties"].items() if k != "$product_tour_id"}
    return {**msg, "options": {"cookieless_mode": False}, "properties": properties}


def _hooked_capture(client):
    return client.capture(
        "e",
        distinct_id="u",
        properties={"$product_tour_id": "tour-1"},
        options={"cookieless_mode": True},
    )


def test_sync_before_send_changes_options_and_legacy_properties():
    events = _sync_wire_events(_hooked_capture, before_send=_hook)
    assert events[0]["options"] == {"cookieless_mode": False}


@pytest.mark.asyncio
async def test_async_before_send_changes_options_and_legacy_properties():
    events = await _async_wire_events(_hooked_capture, before_send=_hook)
    assert events[0]["options"] == {"cookieless_mode": False}


def test_sync_non_dict_options_are_logged_and_event_is_sent(caplog):
    with caplog.at_level(logging.ERROR, logger="posthog"):
        events = _sync_wire_events(
            lambda c: c.capture("e", distinct_id="u", options=["cookieless_mode"])
        )
    assert events[0]["options"] == {}
    assert "options must be a dict" in caplog.text


@pytest.mark.asyncio
async def test_async_non_dict_options_are_logged_and_event_is_sent(caplog):
    with caplog.at_level(logging.ERROR, logger="posthog"):
        events = await _async_wire_events(
            lambda c: c.capture("e", distinct_id="u", options=["cookieless_mode"])
        )
    assert events[0]["options"] == {}
    assert "options must be a dict" in caplog.text
