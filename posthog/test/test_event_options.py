import logging

import pytest

from posthog import AsyncPosthog, new_context, set_context_option, tag
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


def _sync_wire_events(call, before_send=None, **config) -> list[dict]:
    with patch_capture_send("client") as send:
        client = Client(
            FAKE_TEST_API_KEY, sync_mode=True, before_send=before_send, **config
        )
        assert call(client) is not None
    return [_to_v1_event(msg) for msg in sent_events(send)]


async def _async_wire_events(call, before_send=None, **config) -> list[dict]:
    batches: list[list[dict]] = []

    async def send_batch(api_key, host, batch, **kwargs):
        batches.append(batch)

    with patch_async_capture_send(side_effect=send_batch):
        async with AsyncPosthog(
            "test-key", before_send=before_send, **config
        ) as client:
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


PP = "process_person_profile"

# Layers, lowest first: personless (no distinct_id), super, context, event.
LAYER_CASES = {
    "personless_alone": ({}, {}, {}, None, {PP: False}),
    "identified_sends_no_option": ({}, {}, {}, "u", {}),
    "super_beats_personless": ({PP: True}, {}, {}, None, {PP: True}),
    "context_beats_super": ({PP: True}, {PP: False}, {}, "u", {PP: False}),
    "event_beats_context": ({}, {PP: False}, {PP: True}, "u", {PP: True}),
    "event_beats_all": ({PP: False}, {PP: False}, {PP: True}, None, {PP: True}),
    "layers_merge_by_key": (
        {"cookieless_mode": True},
        {"product_tour_id": "t"},
        {},
        "u",
        {"cookieless_mode": True, "product_tour_id": "t"},
    ),
}


def _layered_capture(context_options, event_options, distinct_id):
    def call(client):
        with new_context(fresh=True):
            for key, value in context_options.items():
                set_context_option(key, value)
            return client.capture("e", distinct_id=distinct_id, options=event_options)

    return call


@pytest.mark.parametrize("case", list(LAYER_CASES))
def test_sync_option_layers(case):
    super_options, context, event, distinct_id, expected = LAYER_CASES[case]
    events = _sync_wire_events(
        _layered_capture(context, event, distinct_id), super_options=super_options
    )
    assert events[0]["options"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(LAYER_CASES))
async def test_async_option_layers(case):
    super_options, context, event, distinct_id, expected = LAYER_CASES[case]
    events = await _async_wire_events(
        _layered_capture(context, event, distinct_id), super_options=super_options
    )
    assert events[0]["options"] == expected


@pytest.mark.parametrize(
    "method", ["capture", "set", "set_once", "group_identify", "alias"]
)
def test_sync_super_options_reach_every_path(method):
    events = _sync_wire_events(
        lambda c: CAPTURE_CALLS[method](c, None), super_options=OPTIONS
    )
    assert [e["options"] for e in events] == [OPTIONS]


@pytest.mark.parametrize("method", ["capture", "set", "set_once"])
def test_sync_context_options_reach_user_paths(method):
    def call(client):
        with new_context(fresh=True):
            set_context_option("cookieless_mode", True)
            return CAPTURE_CALLS[method](client, None)

    events = _sync_wire_events(call)
    assert [e["options"] for e in events] == [{"cookieless_mode": True}]


def test_personless_option_beats_legacy_property():
    events = _sync_wire_events(
        lambda c: c.capture("e", properties={"$process_person_profile": True})
    )
    assert events[0]["options"] == {PP: False}
    assert "$process_person_profile" not in events[0]["properties"]


def test_legacy_super_property_fills_option_when_unset():
    events = _sync_wire_events(
        lambda c: c.capture("e", distinct_id="u"),
        super_properties={"$process_person_profile": False},
    )
    assert events[0]["options"] == {PP: False}


def _layered_properties(client):
    with new_context(fresh=True):
        tag("from_context", "context")
        tag("shared", "context")
        return client.capture(
            "e", distinct_id="u", properties={"shared": "event", "only_event": 1}
        )


SUPER_PROPERTIES = {"shared": "super", "from_context": "super", "only_super": 1}


def test_sync_event_and_context_properties_beat_super_properties():
    events = _sync_wire_events(_layered_properties, super_properties=SUPER_PROPERTIES)
    properties = events[0]["properties"]
    assert (
        properties["shared"],
        properties["from_context"],
        properties["only_super"],
        properties["only_event"],
    ) == ("event", "context", 1, 1)


@pytest.mark.asyncio
async def test_async_event_and_context_properties_beat_super_properties():
    events = await _async_wire_events(
        _layered_properties, super_properties=SUPER_PROPERTIES
    )
    properties = events[0]["properties"]
    assert (
        properties["shared"],
        properties["from_context"],
        properties["only_super"],
        properties["only_event"],
    ) == ("event", "context", 1, 1)
