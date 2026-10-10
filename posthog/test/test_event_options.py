import inspect
import logging

import pytest

from posthog import AsyncPosthog, new_context, set_context_option, tag
from posthog.ai.utils import _capture_ai_event
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
            result = call(client)
            if inspect.isawaitable(result):
                result = await result
            assert result is not None
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
    "null_event_option_is_filled": ({PP: True}, {}, {PP: None}, "u", {PP: True}),
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


DEFAULTS_CONFIG = {
    "super_properties": {"shared": "super", "only_super": 1},
    "super_options": {"cookieless_mode": True},
}


def _recording_hook(seen):
    def hook(msg):
        seen.append((dict(msg["properties"]), dict(msg["options"])))
        removed = {"only_super", "$is_server"}
        properties = {
            **{k: v for k, v in msg["properties"].items() if k not in removed},
            "shared": "hook",
        }
        return {**msg, "properties": properties, "options": {"cookieless_mode": False}}

    return hook


def _set_context_values():
    tag("from_context", "context")
    set_context_option("product_tour_id", "context-tour")


def _context_capture(method):
    def call(client):
        if method == "capture":
            with new_context(fresh=True):
                _set_context_values()
                return client.capture("e", distinct_id="u")

        async def immediate():
            with new_context(fresh=True):
                _set_context_values()
                return await client.capture_immediate("e", distinct_id="u")

        return immediate()

    return call


def _assert_hook_sees_defaults_and_has_final_say(seen, events):
    hook_properties, hook_options = seen[0]
    assert (
        hook_properties["shared"],
        hook_properties["only_super"],
        hook_properties["from_context"],
        hook_properties["$is_server"],
        hook_properties["$geoip_disable"],
    ) == ("super", 1, "context", True, True)
    assert "$os" in hook_properties
    assert hook_options == {"cookieless_mode": True, "product_tour_id": "context-tour"}
    properties = events[0]["properties"]
    assert (properties["shared"], properties["from_context"]) == ("hook", "context")
    assert not {"only_super", "$is_server"} & properties.keys()
    assert events[0]["options"] == {"cookieless_mode": False}


def test_sync_defaults_fill_before_before_send():
    seen: list = []
    events = _sync_wire_events(
        _context_capture("capture"),
        before_send=_recording_hook(seen),
        **DEFAULTS_CONFIG,
    )
    _assert_hook_sees_defaults_and_has_final_say(seen, events)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["capture", "capture_immediate"])
async def test_async_defaults_fill_before_before_send(method):
    seen: list = []
    events = await _async_wire_events(
        _context_capture(method), before_send=_recording_hook(seen), **DEFAULTS_CONFIG
    )
    _assert_hook_sees_defaults_and_has_final_say(seen, events)


NESTED_SUPER_PROPERTIES = {
    "$set": {"plan": "free", "source": "super"},
    "$groups": {"company": "acme", "team": "core", "region": "us"},
    "$unset": ["stale"],
}


def _nested_capture(client):
    return client.capture(
        "e",
        distinct_id="u",
        properties={
            "$set": {"plan": "pro"},
            "$unset": ["other"],
            "$groups": {"company": "event", "team": "event"},
        },
        groups={"company": "posthog"},
    )


def _assert_nested_properties(events):
    properties = events[0]["properties"]
    assert properties["$set"] == {"plan": "pro", "source": "super"}
    assert properties["$groups"] == {
        "company": "posthog",
        "team": "event",
        "region": "us",
    }
    assert properties["$unset"] == ["other"]


def test_nested_properties_fill_one_level_deep():
    events = _sync_wire_events(
        _nested_capture, super_properties=NESTED_SUPER_PROPERTIES
    )
    _assert_nested_properties(events)


@pytest.mark.asyncio
async def test_async_nested_properties_fill_one_level_deep():
    events = await _async_wire_events(
        _nested_capture, super_properties=NESTED_SUPER_PROPERTIES
    )
    _assert_nested_properties(events)


CALLER_SDK_VALUES = {"$is_server": False, "$geoip_disable": False, "$os": "caller-os"}
SDK_FLAGS = {"$is_server": False, "$geoip_disable": False}


def _capture_with_caller_sdk_values(source):
    def call(client):
        with new_context(fresh=True):
            if source == "context":
                for key, value in CALLER_SDK_VALUES.items():
                    tag(key, value)
            properties = CALLER_SDK_VALUES if source == "event" else None
            return client.capture("e", distinct_id="u", properties=properties)

    return call


def _sdk_value_config(source):
    return {"super_properties": CALLER_SDK_VALUES} if source == "super" else {}


def _assert_caller_values(events, expected):
    properties = events[0]["properties"]
    assert {key: properties.get(key) for key in expected} == expected


@pytest.mark.parametrize("source", ["event", "context", "super"])
def test_sync_caller_values_beat_sdk_values(source):
    events = _sync_wire_events(
        _capture_with_caller_sdk_values(source), **_sdk_value_config(source)
    )
    _assert_caller_values(events, CALLER_SDK_VALUES)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["event", "context", "super"])
async def test_async_caller_values_beat_sdk_values(source):
    events = await _async_wire_events(
        _capture_with_caller_sdk_values(source), **_sdk_value_config(source)
    )
    _assert_caller_values(events, CALLER_SDK_VALUES)


@pytest.mark.parametrize("method", list(CAPTURE_CALLS))
def test_sync_super_properties_beat_sdk_values_on_every_path(method):
    events = _sync_wire_events(
        lambda c: CAPTURE_CALLS[method](c, None), super_properties=SDK_FLAGS
    )
    _assert_caller_values(events, SDK_FLAGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(ASYNC_CAPTURE_CALLS))
async def test_async_super_properties_beat_sdk_values_on_every_path(method):
    events = await _async_wire_events(
        lambda c: ASYNC_CAPTURE_CALLS[method](c, None), super_properties=SDK_FLAGS
    )
    _assert_caller_values(events, SDK_FLAGS)


@pytest.mark.parametrize("method", ["set", "set_once"])
def test_set_call_wins_over_super_person_properties(method):
    key = f"${method}"
    events = _sync_wire_events(
        lambda c: getattr(c, method)(distinct_id="u", properties={"plan": "pro"}),
        super_properties={key: {"plan": "free", "source": "super"}},
    )
    assert events[0]["properties"][key] == {"plan": "pro", "source": "super"}


def test_late_options_replace_and_remove_legacy_properties():
    def call(client):
        with new_context(fresh=True):
            set_context_option("product_tour_id", "context-tour")
            return client.capture(
                "e", distinct_id="u", properties={"$product_tour_id": "event-tour"}
            )

    events = _sync_wire_events(
        call,
        super_properties={"$cookieless_mode": False},
        super_options={"cookieless_mode": True},
    )
    assert events[0]["options"] == {
        "cookieless_mode": True,
        "product_tour_id": "context-tour",
    }
    assert not {"$cookieless_mode", "$product_tour_id"} & events[0]["properties"].keys()


def _personless_ai_capture(client):
    with new_context(fresh=True):
        set_context_option(PP, True)
        return _capture_ai_event(
            client,
            "$ai_generation",
            distinct_id="trace-1",
            properties={"$process_person_profile": True},
            options={PP: True, "cookieless_mode": True},
            personless=True,
        )


@pytest.mark.parametrize(
    "before_send, expected",
    [
        (None, {PP: False, "cookieless_mode": True}),
        (lambda m: {**m, "options": {PP: True}}, {PP: True}),
    ],
    ids=["sdk_option_wins", "before_send_can_change_it"],
)
def test_personless_ai_event_overrides_every_user_layer(before_send, expected):
    events = _sync_wire_events(
        _personless_ai_capture, before_send=before_send, super_options={PP: True}
    )
    assert events[0]["options"] == expected
