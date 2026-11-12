import inspect
import logging

import pytest
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, use_span

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
from posthog.version import VERSION

OPTIONS = {"cookieless_mode": True, "future_option": "kept"}
PP = "process_person_profile"
LEGACY_PP = "$process_person_profile"

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
ASYNC_LIBRARY_CALLS = {**ASYNC_CAPTURE_CALLS, "capture_ai": CAPTURE_CALLS["capture_ai"]}
# These methods send identify-style events, which default person processing on.
IDENTIFY_STYLE_METHODS = {"set", "set_once", "group_identify", "alias"}


def _expected_options(method, options):
    return {**options, PP: True} if method in IDENTIFY_STYLE_METHODS else options


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
    assert [e["options"] for e in events] == [_expected_options(method, OPTIONS)]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(ASYNC_CAPTURE_CALLS))
async def test_async_options_reach_the_wire(method):
    events = await _async_wire_events(lambda c: ASYNC_CAPTURE_CALLS[method](c, OPTIONS))
    assert [e["options"] for e in events] == [_expected_options(method, OPTIONS)]


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
    assert [e["options"] for e in events] == [_expected_options(method, OPTIONS)]


@pytest.mark.parametrize("method", ["capture", "set", "set_once"])
def test_sync_context_options_reach_user_paths(method):
    def call(client):
        with new_context(fresh=True):
            set_context_option("cookieless_mode", True)
            return CAPTURE_CALLS[method](client, None)

    events = _sync_wire_events(call)
    assert [e["options"] for e in events] == [
        _expected_options(method, {"cookieless_mode": True})
    ]


@pytest.mark.parametrize(
    "event, expected", [("e", False), ("$identify", True)], ids=["other", "identify"]
)
def test_personless_option_against_legacy_property(event, expected):
    # An identify-style event takes the call's legacy property before the
    # personless default fills.
    events = _sync_wire_events(lambda c: c.capture(event, properties={LEGACY_PP: True}))
    assert events[0]["options"] == {PP: expected}
    assert LEGACY_PP not in events[0]["properties"]


@pytest.mark.parametrize(
    "event, expected", [("e", False), ("$identify", True)], ids=["other", "identify"]
)
def test_legacy_super_property_fills_option_when_unset(event, expected):
    events = _sync_wire_events(
        lambda c: c.capture(event, distinct_id="u"),
        super_properties={LEGACY_PP: False},
    )
    assert events[0]["options"] == {PP: expected}


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


@pytest.mark.asyncio
async def test_async_queued_event_keeps_capture_time_defaults():
    def capture_then_change_defaults(client):
        result = client.capture("e", distinct_id="d")
        client.super_properties["plan"] = "pro"
        client.super_options["cookieless_mode"] = True
        return result

    events = await _async_wire_events(
        capture_then_change_defaults,
        super_properties={"plan": "free"},
        super_options={"cookieless_mode": False},
    )
    assert events[0]["properties"]["plan"] == "free"
    assert events[0]["options"]["cookieless_mode"] is False


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


CALLER_SDK_VALUES = {
    "$is_server": False,
    "$geoip_disable": False,
    "$os": "caller-os",
    "$lib": "caller-lib",
    "$lib_version": "caller-version",
}
SDK_FLAGS = {"$is_server": False, "$geoip_disable": False}
SDK_LIBRARY = {"$lib": "posthog-python", "$lib_version": VERSION}
CALLER_LIBRARY = {"$lib": "caller-lib", "$lib_version": "caller-version"}


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


class _FlagSnapshot:
    def _get_event_properties(self):
        return {"$feature/beta": "snapshot", "$active_feature_flags": ["beta"]}


def _capture_with_flags_and_caller_value(source):
    def call(client):
        with new_context(fresh=True):
            if source == "context":
                tag("$feature/beta", "caller")
            properties = {"$feature/beta": "caller"} if source == "event" else None
            return client.capture(
                "e", distinct_id="u", properties=properties, flags=_FlagSnapshot()
            )

    return call


def _flag_layer_config(source):
    return (
        {"super_properties": {"$feature/beta": "caller"}} if source == "super" else {}
    )


def _assert_flag_layers(events, source):
    properties = events[0]["properties"]
    expected = "snapshot" if source is None else "caller"
    assert properties["$feature/beta"] == expected
    assert properties["$active_feature_flags"] == ["beta"]


@pytest.mark.parametrize("source", ["event", "context", "super", None])
def test_sync_caller_layers_beat_flag_properties(source):
    events = _sync_wire_events(
        _capture_with_flags_and_caller_value(source), **_flag_layer_config(source)
    )
    _assert_flag_layers(events, source)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["event", "context", "super", None])
async def test_async_caller_layers_beat_flag_properties(source):
    events = await _async_wire_events(
        _capture_with_flags_and_caller_value(source), **_flag_layer_config(source)
    )
    _assert_flag_layers(events, source)


SPAN_IDS = {
    "$trace_id": "00000000000000000000000000000123",
    "$span_id": "0000000000000456",
}

# Each caller layer sets one key of the pair, so the SDK adds neither span id.
TRACE_ID_CASES = [
    pytest.param(None, SPAN_IDS, id="no_layer"),
    pytest.param("event", {"$trace_id": "caller"}, id="event_trace_id"),
    pytest.param("context", {"$trace_id": "caller"}, id="context_trace_id"),
    pytest.param("super", {"$span_id": "caller"}, id="super_span_id"),
]

TRACE_CALLS = {
    "capture": lambda c, p: c.capture("e", distinct_id="u", properties=p),
    "capture_ai": lambda c, p: c.capture_ai(
        "$ai_generation", distinct_id="u", properties=p
    ),
    "capture_exception": lambda c, p: c.capture_exception(
        ValueError("boom"), distinct_id="u", properties=p
    ),
}


def _capture_in_span(method, source=None):
    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x123,
            span_id=0x456,
            is_remote=False,
            trace_flags=TraceFlags.SAMPLED,
        )
    )

    def call(client):
        with new_context(fresh=True), use_span(span):
            if source == "context":
                tag("$trace_id", "caller")
            properties = {"$trace_id": "caller"} if source == "event" else None
            return TRACE_CALLS[method](client, properties)

    return call


def _trace_config(source, capture_trace_context=True):
    config: dict = {"capture_trace_context": capture_trace_context}
    if source == "super":
        config["super_properties"] = {"$span_id": "caller"}
    return config


def _span_ids(properties):
    return {k: properties[k] for k in SPAN_IDS if k in properties}


def _trace_context_setting(method):
    # capture_exception adds the span ids even with capture_trace_context off.
    return method != "capture_exception"


@pytest.mark.parametrize("method", list(TRACE_CALLS))
@pytest.mark.parametrize("source,expected", TRACE_ID_CASES)
def test_sync_span_ids_fill_as_a_pair_below_caller_layers(method, source, expected):
    events = _sync_wire_events(
        _capture_in_span(method, source),
        **_trace_config(source, _trace_context_setting(method)),
    )
    assert _span_ids(events[0]["properties"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(TRACE_CALLS))
@pytest.mark.parametrize("source,expected", TRACE_ID_CASES)
async def test_async_span_ids_fill_as_a_pair_below_caller_layers(
    method, source, expected
):
    events = await _async_wire_events(
        _capture_in_span(method, source),
        **_trace_config(source, _trace_context_setting(method)),
    )
    assert _span_ids(events[0]["properties"]) == expected


def _span_id_hook(seen):
    def hook(msg):
        seen.append(_span_ids(msg["properties"]))
        return {**msg, "properties": {**msg["properties"], "$span_id": "hook"}}

    return hook


@pytest.mark.parametrize("method", list(TRACE_CALLS))
def test_sync_before_send_sees_and_changes_span_ids(method):
    seen: list = []
    events = _sync_wire_events(
        _capture_in_span(method),
        before_send=_span_id_hook(seen),
        **_trace_config(None, _trace_context_setting(method)),
    )
    assert seen == [SPAN_IDS]
    assert _span_ids(events[0]["properties"]) == {**SPAN_IDS, "$span_id": "hook"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(TRACE_CALLS))
async def test_async_before_send_sees_and_changes_span_ids(method):
    seen: list = []
    events = await _async_wire_events(
        _capture_in_span(method),
        before_send=_span_id_hook(seen),
        **_trace_config(None, _trace_context_setting(method)),
    )
    assert seen == [SPAN_IDS]
    assert _span_ids(events[0]["properties"]) == {**SPAN_IDS, "$span_id": "hook"}


GEOIP_CALL_CASES = [
    pytest.param(True, "context", False, True, id="call_beats_context"),
    pytest.param(True, "super", False, True, id="call_beats_super"),
    pytest.param(False, "super", True, False, id="call_false_beats_super"),
    pytest.param(False, None, None, False, id="call_false_beats_client_setting"),
    pytest.param(True, "event", False, False, id="event_property_beats_call"),
]


def _capture_with_call_geoip(call_value, source, layer_value):
    def call(client):
        with new_context(fresh=True):
            if source == "context":
                tag("$geoip_disable", layer_value)
            properties = {"$geoip_disable": layer_value} if source == "event" else None
            return client.capture(
                "e", distinct_id="u", properties=properties, disable_geoip=call_value
            )

    return call


def _geoip_layer_config(source, layer_value):
    if source == "super":
        return {"super_properties": {"$geoip_disable": layer_value}}
    return {}


@pytest.mark.parametrize("call_value, source, layer_value, expected", GEOIP_CALL_CASES)
def test_sync_call_disable_geoip_is_an_event_value(
    call_value, source, layer_value, expected
):
    events = _sync_wire_events(
        _capture_with_call_geoip(call_value, source, layer_value),
        disable_geoip=True,
        **_geoip_layer_config(source, layer_value),
    )
    assert events[0]["properties"]["$geoip_disable"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("call_value, source, layer_value, expected", GEOIP_CALL_CASES)
async def test_async_call_disable_geoip_is_an_event_value(
    call_value, source, layer_value, expected
):
    events = await _async_wire_events(
        _capture_with_call_geoip(call_value, source, layer_value),
        disable_geoip=True,
        **_geoip_layer_config(source, layer_value),
    )
    assert events[0]["properties"]["$geoip_disable"] is expected


EVERY_PATH_CASES = [
    pytest.param(None, SDK_LIBRARY, id="sdk_library"),
    pytest.param(SDK_FLAGS, SDK_FLAGS, id="super_flags"),
    pytest.param(CALLER_LIBRARY, CALLER_LIBRARY, id="super_library"),
]


@pytest.mark.parametrize("method", list(CAPTURE_CALLS))
@pytest.mark.parametrize("super_properties, expected", EVERY_PATH_CASES)
def test_sync_super_properties_beat_sdk_values_on_every_path(
    method, super_properties, expected
):
    events = _sync_wire_events(
        lambda c: CAPTURE_CALLS[method](c, None), super_properties=super_properties
    )
    _assert_caller_values(events, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", list(ASYNC_LIBRARY_CALLS))
@pytest.mark.parametrize("super_properties, expected", EVERY_PATH_CASES)
async def test_async_super_properties_beat_sdk_values_on_every_path(
    method, super_properties, expected
):
    events = await _async_wire_events(
        lambda c: ASYNC_LIBRARY_CALLS[method](c, None),
        super_properties=super_properties,
    )
    _assert_caller_values(events, expected)


def _library(properties):
    return {k: properties[k] for k in SDK_LIBRARY if k in properties}


# Layers, lowest first: super, context, event. Only a non-empty string sets
# $lib or $lib_version, so a null, empty or non-string value counts as unset.
LIBRARY_CASES = [
    pytest.param({}, {}, {}, SDK_LIBRARY, id="no_layer"),
    pytest.param(
        {}, {}, {"$lib": "caller-lib"}, {"$lib": "caller-lib"}, id="caller_lib_alone"
    ),
    pytest.param(
        {},
        {},
        {"$lib": "caller-lib", "$lib_version": None},
        {"$lib": "caller-lib", "$lib_version": None},
        id="caller_lib_keeps_null_version",
    ),
    pytest.param({}, {}, {"$lib": "posthog-python"}, SDK_LIBRARY, id="sdk_lib_alone"),
    pytest.param(
        {},
        {},
        {"$lib": "posthog-python", "$lib_version": ""},
        SDK_LIBRARY,
        id="sdk_lib_empty_version",
    ),
    pytest.param(
        {},
        {},
        {"$lib": "posthog-python", "$lib_version": 5},
        SDK_LIBRARY,
        id="sdk_lib_number_version",
    ),
    pytest.param({}, {}, {"$lib": " "}, {"$lib": " "}, id="whitespace_lib_is_set"),
    pytest.param(
        {"$lib": 5, "$lib_version": "super-version"},
        {},
        {},
        SDK_LIBRARY,
        id="super_number_lib",
    ),
    pytest.param(
        {},
        {},
        {"$lib": "posthog-python", "$lib_version": "1.0.0"},
        {"$lib": "posthog-python", "$lib_version": "1.0.0"},
        id="sdk_lib_caller_version",
    ),
    *(
        pytest.param({}, {}, {"$lib": value}, SDK_LIBRARY, id=f"{name}_lib")
        for name, value in [
            ("null", None),
            ("empty", ""),
            ("number", 5),
            ("bool", True),
            ("dict", {}),
        ]
    ),
    pytest.param(
        {}, {}, {"$lib_version": "caller-version"}, SDK_LIBRARY, id="lone_version"
    ),
    pytest.param(
        {},
        {},
        {"$lib": None, "$lib_version": "caller-version"},
        SDK_LIBRARY,
        id="null_lib_with_version",
    ),
    pytest.param(
        {"$lib_version": "super-version"},
        {},
        {},
        SDK_LIBRARY,
        id="super_lone_version",
    ),
    pytest.param(
        CALLER_LIBRARY, {}, {"$lib": None}, CALLER_LIBRARY, id="super_fills_null_lib"
    ),
    pytest.param(
        {},
        {"$lib": "context-lib"},
        {"$lib": ""},
        {"$lib": "context-lib"},
        id="context_fills_empty_lib",
    ),
    pytest.param(
        CALLER_LIBRARY,
        {"$lib": "context-lib"},
        {},
        {"$lib": "context-lib", "$lib_version": "caller-version"},
        id="context_beats_super_by_key",
    ),
    pytest.param(
        {"$lib": "super-lib"},
        {},
        {"$lib": "posthog-python"},
        SDK_LIBRARY,
        id="event_sdk_lib_beats_super",
    ),
]


def _library_capture(context, event):
    def call(client):
        with new_context(fresh=True):
            for key, value in context.items():
                tag(key, value)
            return client.capture("e", distinct_id="u", properties=dict(event))

    return call


@pytest.mark.parametrize("super_properties, context, event, expected", LIBRARY_CASES)
def test_sync_sdk_library_fills_as_a_pair(super_properties, context, event, expected):
    events = _sync_wire_events(
        _library_capture(context, event), super_properties=super_properties
    )
    assert _library(events[0]["properties"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("super_properties, context, event, expected", LIBRARY_CASES)
async def test_async_sdk_library_fills_as_a_pair(
    super_properties, context, event, expected
):
    events = await _async_wire_events(
        _library_capture(context, event), super_properties=super_properties
    )
    assert _library(events[0]["properties"]) == expected


def _without_library(properties):
    return {k: v for k, v in properties.items() if k not in SDK_LIBRARY}


LIBRARY_HOOKS = {
    "change": (
        lambda p: {**_without_library(p), "$lib": "hook-lib"},
        {"$lib": "hook-lib"},
    ),
    "remove": (_without_library, {}),
    "null": (lambda p: {**p, "$lib": None}, {**SDK_LIBRARY, "$lib": None}),
}


def _library_hook(seen, change):
    def hook(msg):
        seen.append(_library(msg["properties"]))
        return {**msg, "properties": change(msg["properties"])}

    return hook


@pytest.mark.parametrize("hook", list(LIBRARY_HOOKS))
def test_sync_before_send_sees_and_changes_the_sdk_library(hook):
    change, expected = LIBRARY_HOOKS[hook]
    seen: list = []
    events = _sync_wire_events(
        lambda c: c.capture("e", distinct_id="u"),
        before_send=_library_hook(seen, change),
    )
    assert seen == [SDK_LIBRARY]
    assert _library(events[0]["properties"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", list(LIBRARY_HOOKS))
async def test_async_before_send_sees_and_changes_the_sdk_library(hook):
    change, expected = LIBRARY_HOOKS[hook]
    seen: list = []
    events = await _async_wire_events(
        lambda c: c.capture("e", distinct_id="u"),
        before_send=_library_hook(seen, change),
    )
    assert seen == [SDK_LIBRARY]
    assert _library(events[0]["properties"]) == expected


@pytest.mark.parametrize("method", ["set", "set_once"])
def test_set_call_wins_over_super_person_properties(method):
    key = f"${method}"
    events = _sync_wire_events(
        lambda c: getattr(c, method)(distinct_id="u", properties={"plan": "pro"}),
        super_properties={key: {"plan": "free", "source": "super"}},
    )
    assert events[0]["properties"][key] == {"plan": "pro", "source": "super"}


@pytest.mark.parametrize(
    "event, extra", [("e", {}), ("$identify", {PP: True})], ids=["other", "identify"]
)
def test_late_options_replace_and_remove_legacy_properties(event, extra):
    def call(client):
        with new_context(fresh=True):
            set_context_option("product_tour_id", "context-tour")
            return client.capture(
                event, distinct_id="u", properties={"$product_tour_id": "event-tour"}
            )

    events = _sync_wire_events(
        call,
        super_properties={"$cookieless_mode": False},
        super_options={"cookieless_mode": True},
    )
    assert events[0]["options"] == {
        "cookieless_mode": True,
        "product_tour_id": "context-tour",
        **extra,
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


MODES = ["sync", "async"]

# Listed here rather than imported, so the test pins the exact names.
IDENTIFY_STYLE_EVENTS = [
    "$identify",
    "$create_alias",
    "$merge_dangerously",
    "$groupidentify",
    "$set",
    "$set_once",
]


async def _wire_events(mode, call, before_send=None, **config) -> list[dict]:
    if mode == "sync":
        return _sync_wire_events(call, before_send=before_send, **config)
    return await _async_wire_events(call, before_send=before_send, **config)


def _capture_as(name, method="capture"):
    return lambda c, o, p: getattr(c, method)(
        name, distinct_id="u", properties=p, options=o
    )


IDENTIFY_STYLE_EMITTERS = {
    "set": lambda c, o, p: c.set(distinct_id="u", properties={"a": 1}, options=o),
    "set_once": lambda c, o, p: c.set_once(
        distinct_id="u", properties={"a": 1}, options=o
    ),
    "group_identify": lambda c, o, p: c.group_identify(
        "company", "acme", distinct_id="u", options=o
    ),
    "alias": lambda c, o, p: c.alias("previous", "u", options=o),
    **{f"capture:{name}": _capture_as(name) for name in IDENTIFY_STYLE_EVENTS},
    "capture_ai:$identify": _capture_as("$identify", "capture_ai"),
}
# Only the capture methods take event properties, so only they can set the
# legacy property on the call.
PROPERTY_EMITTERS = [k for k in IDENTIFY_STYLE_EMITTERS if k.startswith("capture")]


def _layers(value):
    """Each layer kind that can set person processing, keyed by kind."""
    return {
        "super_options": {"super_options": {PP: value}},
        "super_properties": {"super_properties": {LEGACY_PP: value}},
        "context_option": {"context_options": {PP: value}},
        "context_tag": {"tags": {LEGACY_PP: value}},
    }


def _all_layers(value):
    if value is None:
        return {}
    return {k: v for layer in _layers(value).values() for k, v in layer.items()}


def _layered(emit, layers, options=None, properties=None):
    def call(client):
        with new_context(fresh=True):
            for key, value in layers.get("context_options", {}).items():
                set_context_option(key, value)
            for key, value in layers.get("tags", {}).items():
                tag(key, value)
            return emit(client, options, properties)

    return call


def _client_config(layers):
    return {k: layers[k] for k in ("super_options", "super_properties") if k in layers}


def _assert_person_processing(events, expected):
    assert events[0]["options"][PP] is expected
    assert LEGACY_PP not in events[0]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("layer", ["no_layer", *_layers(False)])
@pytest.mark.parametrize("emitter", list(IDENTIFY_STYLE_EMITTERS))
async def test_identify_style_event_defaults_person_processing_on(mode, layer, emitter):
    layers = _layers(False).get(layer, {})
    events = await _wire_events(
        mode,
        _layered(IDENTIFY_STYLE_EMITTERS[emitter], layers),
        **_client_config(layers),
    )
    _assert_person_processing(events, True)


# (layer value, call options, expected)
CALL_OPTION_CASES = {
    "option_false_beats_true_layers": (True, {PP: False}, False),
    "option_true_beats_false_layers": (False, {PP: True}, True),
    "null_option_is_unset": (False, {PP: None}, True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", list(CALL_OPTION_CASES))
@pytest.mark.parametrize("emitter", list(IDENTIFY_STYLE_EMITTERS))
async def test_identify_style_event_keeps_the_call_option(mode, case, emitter):
    layer_value, options, expected = CALL_OPTION_CASES[case]
    layers = _all_layers(layer_value)
    events = await _wire_events(
        mode,
        _layered(IDENTIFY_STYLE_EMITTERS[emitter], layers, options=options),
        **_client_config(layers),
    )
    _assert_person_processing(events, expected)


# (layer value, call options, call properties, expected)
CALL_PROPERTY_CASES = {
    "legacy_false_moves_to_option": (None, None, {LEGACY_PP: False}, False),
    "legacy_false_beats_true_layers": (True, None, {LEGACY_PP: False}, False),
    "legacy_true_beats_false_layers": (False, None, {LEGACY_PP: True}, True),
    "option_beats_legacy": (None, {PP: False}, {LEGACY_PP: True}, False),
    "null_legacy_is_unset": (False, None, {LEGACY_PP: None}, True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", list(CALL_PROPERTY_CASES))
@pytest.mark.parametrize("emitter", PROPERTY_EMITTERS)
async def test_identify_style_event_takes_the_call_legacy_property(mode, case, emitter):
    layer_value, options, properties, expected = CALL_PROPERTY_CASES[case]
    layers = _all_layers(layer_value)
    events = await _wire_events(
        mode,
        _layered(
            IDENTIFY_STYLE_EMITTERS[emitter],
            layers,
            options=options,
            properties=properties,
        ),
        **_client_config(layers),
    )
    _assert_person_processing(events, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "properties, expected",
    [(None, False), ({LEGACY_PP: True}, True)],
    ids=["personless_default_stays", "call_legacy_wins"],
)
@pytest.mark.parametrize("name", IDENTIFY_STYLE_EVENTS)
async def test_identify_style_event_with_a_generated_distinct_id(
    mode, properties, expected, name
):
    events = await _wire_events(mode, lambda c: c.capture(name, properties=properties))
    _assert_person_processing(events, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_group_identify_fallback_distinct_id_processes_the_person(mode):
    events = await _wire_events(mode, lambda c: c.group_identify("company", "acme"))
    assert events[0]["distinct_id"] == "$company_acme"
    _assert_person_processing(events, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "emitter, properties, resolved",
    [("alias", None, True), ("capture:$identify", {LEGACY_PP: False}, False)],
    ids=["default", "hoisted_legacy"],
)
async def test_before_send_sees_the_resolved_option_and_has_final_say(
    mode, emitter, properties, resolved
):
    seen: list = []

    def hook(msg):
        seen.append((msg["options"].get(PP), LEGACY_PP in msg["properties"]))
        return {**msg, "options": {**msg["options"], PP: not resolved}}

    events = await _wire_events(
        mode,
        lambda c: IDENTIFY_STYLE_EMITTERS[emitter](c, None, properties),
        before_send=hook,
    )
    assert seen == [(resolved, False)]
    _assert_person_processing(events, not resolved)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", ["e", "identify", "$IDENTIFY", "$set_once_extra"])
async def test_other_events_keep_layered_person_processing(mode, name):
    layers = _all_layers(False)
    events = await _wire_events(
        mode, _layered(_capture_as(name), layers), **_client_config(layers)
    )
    _assert_person_processing(events, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_before_send_rename_to_identify_style_gets_no_default(mode):
    events = await _wire_events(
        mode,
        lambda c: c.capture("e", distinct_id="u"),
        before_send=lambda msg: {**msg, "event": "$identify"},
        super_options={PP: False},
    )
    assert events[0]["event"] == "$identify"
    _assert_person_processing(events, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "emitter, properties, expected",
    [("alias", None, True), ("capture:$identify", {LEGACY_PP: False}, False)],
    ids=["default", "hoisted_legacy"],
)
async def test_identify_style_default_leaves_the_call_dicts_unchanged(
    mode, emitter, properties, expected
):
    options = {"cookieless_mode": True}
    caller_properties = None if properties is None else dict(properties)
    events = await _wire_events(
        mode,
        lambda c: IDENTIFY_STYLE_EMITTERS[emitter](c, options, caller_properties),
    )
    assert options == {"cookieless_mode": True}
    assert caller_properties == properties
    assert events[0]["options"] == {"cookieless_mode": True, PP: expected}


LEGACY_FALSE_LAYERS = ["super_properties", "context_tag"]


def _record_person_processing(seen, drop_options=False):
    def hook(msg):
        seen.append((msg["options"].get(PP), msg["properties"].get(LEGACY_PP, "unset")))
        if drop_options:
            return {k: v for k, v in msg.items() if k != "options"}
        return msg

    return hook


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("layer", LEGACY_FALSE_LAYERS)
@pytest.mark.parametrize("emitter", list(IDENTIFY_STYLE_EMITTERS))
async def test_before_send_sees_no_layered_legacy_property_on_identify_style_events(
    mode, layer, emitter
):
    layers = _layers(False)[layer]
    seen: list = []
    await _wire_events(
        mode,
        _layered(IDENTIFY_STYLE_EMITTERS[emitter], layers),
        before_send=_record_person_processing(seen),
        **_client_config(layers),
    )
    assert seen == [(True, "unset")]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("layer", LEGACY_FALSE_LAYERS)
async def test_hook_that_drops_options_sends_no_layered_legacy_false(mode, layer):
    layers = _layers(False)[layer]
    events = await _wire_events(
        mode,
        _layered(IDENTIFY_STYLE_EMITTERS["alias"], layers),
        before_send=_record_person_processing([], drop_options=True),
        **_client_config(layers),
    )
    assert PP not in events[0]["options"]
    assert LEGACY_PP not in events[0]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("layer", LEGACY_FALSE_LAYERS)
async def test_before_send_still_sees_the_layered_legacy_property_on_other_events(
    mode, layer
):
    layers = {**_layers(False)[layer], "super_options": {PP: True}}
    seen: list = []
    events = await _wire_events(
        mode,
        _layered(_capture_as("e"), layers),
        before_send=_record_person_processing(seen),
        **_client_config(layers),
    )
    assert seen == [(True, False)]
    _assert_person_processing(events, True)
