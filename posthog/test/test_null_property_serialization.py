"""Exercise final SDK bytes; no transport/serializer is replaced by a sanitizer."""

import copy
import gzip
import json
import socket
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from queue import Queue

import httpx
import pytest
import requests
from pydantic import BaseModel

from posthog import AsyncPosthog, Posthog
from posthog._async_consumer import _serialized_event_size
from posthog._async_request import _serialize_flags_body
from posthog.consumer import Consumer
from posthog.request import DatetimeSerializer, post
from posthog.utils import clean


@dataclass
class NullableDataclass:
    drop: None = None


class NullableModel(BaseModel):
    drop: None = None


def properties():
    return {
        "test": None,
        "nested": {"drop": None},
        "items": ["1", None, 2, {"drop": None}, [None]],
        "empty": "",
        "zero": 0,
        "enabled": False,
        "literal": "null",
        "literalUndefined": "undefined",
        "emptyArray": [],
        "emptyObject": {},
        "$set": {"drop": None},
        "$set_once": {"items": [None, {"drop": None}]},
        "$group_set": {"drop": None},
        "dataclass": NullableDataclass(),
        "model": NullableModel(),
        "date": date(2026, 1, 1),
        "decimal": Decimal("1.25"),
    }


EXPECTED = {
    "nested": {},
    "items": ["1", None, 2, {}, [None]],
    "empty": "",
    "zero": 0,
    "enabled": False,
    "literal": "null",
    "literalUndefined": "undefined",
    "emptyArray": [],
    "emptyObject": {},
    "$set": {},
    "$set_once": {"items": [None, {}]},
    "$group_set": {},
    "dataclass": {},
    "model": {},
    "date": "2026-01-01",
    "decimal": 1.25,
}


@pytest.fixture
def flag_response():
    return {}


@pytest.fixture
def wire(monkeypatch, flag_response):
    captured = []

    def deny(*args, **kwargs):
        raise AssertionError("Unexpected SDK network access")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(requests.Session, "request", deny)
    monkeypatch.setattr(httpx.AsyncClient, "request", deny)

    def record(url, data, headers):
        if headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        body = json.loads(data)
        if "/flags/" in str(url):
            return flag_response
        captured.append((str(url), body))
        return {
            "results": {
                event["uuid"]: {"result": "ok"} for event in body.get("batch", [])
            }
        }

    def sync_post(self, url, data=None, headers=None, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(record(url, data, headers)).encode()
        return response

    async def async_post(self, url, content=None, headers=None, **kwargs):
        return httpx.Response(200, json=record(url, content, headers))

    monkeypatch.setattr(requests.Session, "post", sync_post)
    monkeypatch.setattr(httpx.AsyncClient, "post", async_post)
    return captured


def assert_wire(wire, mode, case, typed):
    assert len(wire) == 1
    url, body = wire[0]
    v1 = mode == "v1" and case != "ai"
    assert url.endswith(
        "/i/v1/analytics/events"
        if v1
        else "/i/v0/ai/batch/"
        if case == "ai"
        else "/batch/"
    )
    events = body["batch"]
    assert len(events) == 1
    props = events[0]["properties"]
    if not v1:
        assert props["$lib"] == "posthog-python"
    else:
        assert "options" in events[0]
        assert "uuid" in events[0]
    assert "test" not in props
    assert "missing" not in props
    assert "hookNull" not in props
    if case != "all_null":
        assert props["hookItems"] == [None, {}]
    assert props["super"] == {}
    if case not in ("all_null", "hook"):
        assert {key: props[key] for key in EXPECTED} == EXPECTED
    if case == "exception":
        assert props["$exception_list"] == typed[0]
        assert props["$exception_list"]


def hook(typed):
    def before_send(event):
        props = event["properties"]
        props.update(hookNull=None, hookItems=[None, {"drop": None}])
        if event["event"] == "$exception":
            typed.append(copy.deepcopy(props["$exception_list"]))
        return event

    return before_send


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("case", ["capture", "ai", "exception", "all_null", "hook"])
def test_client_null_properties_wire(wire, mode, sync, compress, case):
    typed = []
    client = Posthog(
        "test-token",
        host="http://127.0.0.1:1",
        sync_mode=sync,
        capture_mode=mode,
        gzip=compress,
        before_send=None if case == "all_null" else hook(typed),
        super_properties={"super": {"drop": None}},
    )
    source = (
        {} if case == "hook" else {"test": None} if case == "all_null" else properties()
    )
    original = copy.deepcopy(source)
    try:
        if case == "exception":
            result = client.capture_exception(
                ValueError("test"), distinct_id="user", properties=source
            )
        elif case == "ai":
            result = client.capture_ai(
                "$ai_generation", distinct_id="user", properties=source
            )
        else:
            result = client.capture("Nullable", distinct_id="user", properties=source)
        assert result is not None
        client.flush()
    finally:
        client.shutdown()
    assert source == original
    assert_wire(wire, mode, case, typed)


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("case", ["capture", "exception", "all_null", "hook"])
async def test_async_client_null_properties_wire(wire, mode, immediate, compress, case):
    if immediate and case == "exception":
        pytest.skip("AsyncPosthog has no immediate exception API")
    typed = []
    client = AsyncPosthog(
        "test-token",
        host="http://127.0.0.1:1",
        capture_mode=mode,
        gzip=compress,
        before_send=None if case == "all_null" else hook(typed),
        super_properties={"super": {"drop": None}},
    )
    source = (
        {} if case == "hook" else {"test": None} if case == "all_null" else properties()
    )
    original = copy.deepcopy(source)
    try:
        if case == "exception":
            result = client.capture_exception(
                ValueError("test"), distinct_id="user", properties=source
            )
        elif immediate:
            result = await client.capture_immediate(
                "Nullable", distinct_id="user", properties=source
            )
        else:
            result = client.capture("Nullable", distinct_id="user", properties=source)
        assert result is not None
        await client.flush()
    finally:
        await client.shutdown()
    assert source == original
    assert_wire(wire, mode, case, typed)


def test_non_event_requests_and_clean_keep_null(wire):
    source = {"properties": {"drop": None}, "model": NullableModel()}
    assert clean(source)["model"] == {"drop": None}
    assert clean(source)["properties"] == {"drop": None}
    post("test-token", "http://127.0.0.1:1", "/unrelated", properties={"drop": None})
    assert wire[0][1]["properties"] == {"drop": None}
    data, _ = _serialize_flags_body("test-token", {"person_properties": {"drop": None}})
    assert json.loads(data)["person_properties"] == {"drop": None}


async def test_event_size_uses_cleaned_properties(wire):
    event = {
        "event": "Nullable",
        "properties": {"x" * 1000: None, "items": [None, {"drop": None}]},
    }
    expected = {"event": "Nullable", "properties": {"items": [None, {}]}}
    assert await _serialized_event_size(event) == len(
        json.dumps(expected, cls=DatetimeSerializer).encode()
    )
    queue = Queue()
    queue.put(event)
    consumer = Consumer(queue, "test-token", flush_at=1, max_msg_size=200)
    assert consumer.next() == [event]


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("event_name", ["$exception", "ordinary"])
def test_exception_metadata_exemption_is_event_scoped(wire, mode, event_name):
    typed = [
        {"type": "Error", "value": None, "stacktrace": {"frames": [{"filename": None}]}}
    ]
    client = Posthog(
        "test-token", host="http://127.0.0.1:1", sync_mode=True, capture_mode=mode
    )
    try:
        client.capture(
            event_name,
            distinct_id="user",
            properties={"$exception_list": typed, "custom": {"drop": None}},
        )
    finally:
        client.shutdown()
    body = wire[0][1]
    props = body["batch"][0]["properties"]
    assert props["custom"] == {}
    assert props["$exception_list"] == (
        typed
        if event_name == "$exception"
        else [{"type": "Error", "stacktrace": {"frames": [{}]}}]
    )


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("method", ["set", "set_once"])
@pytest.mark.parametrize("native_async", [False, True])
async def test_person_properties_serialization(wire, mode, method, native_async):
    client_type = AsyncPosthog if native_async else Posthog
    client = client_type("test-token", host="http://127.0.0.1:1", capture_mode=mode)
    source = {"drop": None, "items": [None, {"drop": None}]}
    original = copy.deepcopy(source)
    try:
        assert getattr(client, method)(distinct_id="user", properties=source)
        if native_async:
            await client.flush()
        else:
            client.flush()
    finally:
        if native_async:
            await client.shutdown()
        else:
            client.shutdown()
    event = wire[0][1]["batch"][0]
    assert (event["properties"] if mode == "v1" else event)[f"${method}"] == {
        "items": [None, {}]
    }
    assert source == original


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("native_async", [False, True])
async def test_hook_drop_still_prevents_delivery(wire, mode, native_async):
    client_type = AsyncPosthog if native_async else Posthog
    client = client_type(
        "test-token",
        host="http://127.0.0.1:1",
        capture_mode=mode,
        before_send=lambda event: None,
    )
    try:
        client.capture("Dropped", distinct_id="user", properties={"drop": None})
        if native_async:
            await client.flush()
        else:
            client.flush()
    finally:
        if native_async:
            await client.shutdown()
        else:
            client.shutdown()
    assert wire == []


def flag_hook(event):
    event["properties"].update(
        custom={"drop": None, "items": [None, {"drop": None}]},
        **{"$feature/other": None, "$feature/other-object": {"drop": None}},
    )
    return event


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("delivery", ["sync", "threaded", "async"])
@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("case", ["missing", "error", "minimal"])
async def test_generated_flag_metadata_wire(
    wire, flag_response, mode, delivery, compress, case
):
    # A missing flag cannot be minimized: has_experiment is unknown. A known
    # non-experiment flag exercises the real server-controlled privacy allowlist.
    flag_response.update(
        flags={
            "flag": {
                "key": "flag",
                "enabled": False,
                "variant": None,
                "metadata": {"has_experiment": False},
            }
        }
        if case == "minimal"
        else {},
        errorsWhileComputingFlags=case == "error",
        minimalFlagCalledEvents=True,
    )
    native_async = delivery == "async"
    options = {} if native_async else {"sync_mode": delivery == "sync"}
    client = (AsyncPosthog if native_async else Posthog)(
        "test-token",
        host="http://127.0.0.1:1",
        capture_mode=mode,
        gzip=compress,
        before_send=flag_hook,
        super_properties={"private_super": "must not survive minimization"},
        **options,
    )
    try:
        # Distinct users avoid deduplication between the two actual producers.
        if not native_async:
            result = client.get_feature_flag_result("flag", "single-user")
            assert (result.get_value() if result else None) is (
                False if case == "minimal" else None
            )
        snapshot = (
            await client.evaluate_flags("snapshot-user")
            if native_async
            else client.evaluate_flags("snapshot-user")
        )
        assert snapshot.get_flag("flag") is (False if case == "minimal" else None)
        if native_async:
            await client.flush()
        else:
            client.flush()
    finally:
        if native_async:
            await client.shutdown()
        else:
            client.shutdown()
    events = [event for _, body in wire for event in body["batch"]]
    assert len(events) == (1 if native_async else 2)
    for event in events:
        assert event["event"] == "$feature_flag_called"
        props = event["properties"]
        assert props["$feature_flag"] == "flag"
        assert props["$feature_flag_response"] is (False if case == "minimal" else None)
        if case == "minimal":
            assert "$feature/flag" not in props
            assert "private_super" not in props
        else:
            assert props["$feature/flag"] is None
            assert props["$feature_flag_error"] == (
                "errors_while_computing_flags,flag_missing"
                if case == "error"
                else "flag_missing"
            )
        # before_send remains authoritative after privacy minimization.
        assert props["custom"] == {"items": [None, {}]}
        assert props["$feature/other-object"] == {}
        assert "$feature/other" not in props


@pytest.mark.parametrize("mode", ["v0", "v1"])
@pytest.mark.parametrize("event_name", ["ordinary", "$feature_flag_called"])
def test_flag_metadata_exemption_is_event_and_value_scoped(wire, mode, event_name):
    client = Posthog(
        "test-token", host="http://127.0.0.1:1", sync_mode=True, capture_mode=mode
    )
    try:
        client.capture(
            event_name,
            distinct_id="user",
            properties={
                "$feature_flag": "flag",
                "$feature_flag_response": {"drop": None},
                "$feature/flag": {"drop": None},
                "$feature/other": None,
            },
        )
        client.capture(
            "ordinary",
            distinct_id="user",
            properties={
                "$feature_flag": "flag",
                "$feature_flag_response": None,
                "$feature/flag": None,
            },
        )
    finally:
        client.shutdown()
    props = wire[0][1]["batch"][0]["properties"]
    assert props["$feature_flag_response"] == {}
    assert props["$feature/flag"] == {}
    assert "$feature/other" not in props
    ordinary = wire[1][1]["batch"][0]["properties"]
    assert "$feature_flag_response" not in ordinary
    assert "$feature/flag" not in ordinary
