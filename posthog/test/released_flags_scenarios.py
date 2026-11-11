"""Flag scenarios whose results are pinned to a released SDK.

``fixtures/released_flags_7_82_1.json`` holds the results of these scenarios as
posthog 7.82.1 produced them from the ``v2`` response shape, the shape that release
requests. ``test_flags_released_behaviour.py`` runs the same scenarios on this SDK
from the ``v2`` and the ``v4`` shape and compares them with that file.

This module uses only the public client API, so it also runs on 7.82.1. To
regenerate the file, install ``posthog==7.82.1`` in a separate environment, load this
module by its path from a directory outside the checkout, so that the installed
package is imported, and write ``{"description": DESCRIPTION, "results": run_all("v2")}``
to the file as JSON with sorted keys and an indent of 2.
"""

import copy
import dataclasses
import json
import warnings
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from posthog import Client
from posthog.request import APIError, QuotaLimitError

DESCRIPTION = (
    "Results of the scenarios in posthog/test/released_flags_scenarios.py, recorded"
    " with posthog 7.82.1 from the v2 response shape. See that module to regenerate."
)
API_KEY = "phc_released_flags_scenarios"
DISTINCT_ID = "user-1"
REQUEST_ID = "60000000-0000-4000-8000-000000000000"
EVALUATED_AT = 1800000000000
MATCHED = {
    "code": "condition_match",
    "condition_index": 0,
    "description": "Matched condition set 1",
}


def _flag(key: str, value: Any, **fields: Any) -> Dict[str, Any]:
    """One flag outcome, rendered into a response by ``build_response``."""
    flag: Dict[str, Any] = {
        "key": key,
        "value": value,
        "config_version": 1,
        "payload": None,
        "metadata": {"id": 700, "version": 3, "description": None},
        "has_experiment": False,
        "reason": MATCHED,
        "failed": False,
        "set": {},
    }
    flag.update(fields)
    return flag


# Flags read by the getter scenario. A config version 2 flag carries a typed value;
# the v2 response renders it the way the server does for older SDKs.
GETTER_FLAGS = [
    _flag("bool-on", True),
    _flag("bool-off", False, reason={"code": "no_condition_match"}),
    _flag(
        "v1-variant",
        "control",
        payload='{"columns": 2}',
        has_experiment=True,
        metadata={"id": 701, "version": 4, "description": "A variant flag"},
    ),
    _flag("v1-bool-payload", True, payload='{"cta": "Try it", "n": 1}'),
    _flag("v1-string-payload", True, payload='"plain"'),
    _flag("v2-string", "compact", config_version=2),
    _flag("v2-null", None, config_version=2),
    _flag("v2-number", 12.5, config_version=2),
    _flag("v2-object", {"layout": "compact", "n": [1, True]}, config_version=2),
    _flag("sparse", "compact", metadata={}, has_experiment=None, reason=None),
    # A failed record that still carries a payload.
    _flag("server-failed", "compact", failed=True, payload='{"columns": 2}'),
]


def build_response(
    shape: str, flags: List[Dict[str, Any]], **top_level: Any
) -> Dict[str, Any]:
    """The ``/flags`` response of the given shape (``"v2"`` or ``"v4"``) for flags."""
    records: Dict[str, Any] = {}
    for flag in flags:
        value = None if flag["failed"] else flag["value"]
        metadata = None if flag["metadata"] is None else dict(flag["metadata"])
        if metadata is not None and flag["has_experiment"] is not None:
            metadata["has_experiment"] = flag["has_experiment"]
        record: Dict[str, Any] = {"key": flag["key"]}
        if shape == "v2":
            typed = flag["config_version"] != 1 and not isinstance(
                value, (bool, str, type(None))
            )
            record["enabled"] = value is not None and value is not False
            record["variant"] = value if isinstance(value, str) else None
            if metadata is not None:
                metadata["payload"] = (
                    json.dumps(value, separators=(",", ":"))
                    if typed
                    else flag["payload"]
                )
        else:
            record["value"] = value
            if metadata is not None:
                metadata["config_version"] = flag["config_version"]
                if flag["config_version"] == 1:
                    metadata["payload"] = flag["payload"]
                if isinstance(value, str):
                    metadata["variant_key"] = value
        if flag["failed"]:
            record["failed"] = True
        if flag["reason"] is not None:
            record["reason"] = flag["reason"]
        if metadata is not None:
            record["metadata"] = metadata
        record.update(copy.deepcopy(flag["set"]))
        records[flag["key"]] = record
    response: Dict[str, Any] = {
        "flags": records,
        "errorsWhileComputingFlags": False,
        "requestId": REQUEST_ID,
        "evaluatedAt": EVALUATED_AT,
    }
    response.update(top_level)
    return response


def plain(value: Any) -> Any:
    """A JSON-compatible copy of a result, with each dataclass's public fields."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            field.name: plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        return {"__class__": type(value).__name__, **fields}
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def _flag_event_properties(message: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: item
        for key, item in message["properties"].items()
        if key.startswith("$feature")
        or key in ("$active_feature_flags", "locally_evaluated")
    }


def _with_client(
    responses: List[Any], body: Callable[[Client, Dict[str, Any]], None], **options: Any
) -> Dict[str, Any]:
    """Run ``body`` on a client whose ``/flags`` requests return ``responses`` in turn."""
    client = Client(API_KEY, send=False, **options)
    outputs: Dict[str, Any] = {}
    events: List[Any] = []
    pending = list(responses)

    def flags(*args: Any, **kwargs: Any) -> Any:
        response = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    # Read the built event in before_send: v1 layers flag properties inside _enqueue.
    def record(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        events.append([message["event"], _flag_event_properties(message)])
        return None

    try:
        with (
            mock.patch("posthog.client.flags", side_effect=flags),
            mock.patch.object(client, "before_send", record),
            warnings.catch_warnings(),
        ):
            warnings.simplefilter("ignore")
            body(client, outputs)
    finally:
        client.shutdown()
    outputs["events"] = events
    return plain(outputs)


def _read_every_getter(
    client: Client, outputs: Dict[str, Any], keys: List[str]
) -> None:
    outputs["get_flags_decision"] = client.get_flags_decision(DISTINCT_ID)
    outputs["get_feature_variants"] = client.get_feature_variants(DISTINCT_ID)
    outputs["get_feature_payloads"] = client.get_feature_payloads(DISTINCT_ID)
    outputs["get_feature_flags_and_payloads"] = client.get_feature_flags_and_payloads(
        DISTINCT_ID
    )
    outputs["get_all_flags"] = client.get_all_flags(DISTINCT_ID)
    outputs["get_all_flags_and_payloads"] = client.get_all_flags_and_payloads(
        DISTINCT_ID
    )
    for key in keys + ["missing"]:
        result = client.get_feature_flag_result(key, DISTINCT_ID)
        outputs[f"get_feature_flag_result:{key}"] = (
            None
            if result is None
            else [result.enabled, result.variant, result.payload, result.reason]
        )
        outputs[f"get_feature_flag:{key}"] = client.get_feature_flag(key, "user-2")
        outputs[f"feature_enabled:{key}"] = client.feature_enabled(key, "user-3")
        outputs[f"get_feature_flag_payload:{key}"] = client.get_feature_flag_payload(
            key, DISTINCT_ID
        )
    snapshot = client.evaluate_flags("snapshot-user")
    outputs["snapshot.keys"] = sorted(snapshot.keys)
    for key in keys + ["missing"]:
        outputs[f"snapshot.is_enabled:{key}"] = snapshot.is_enabled(key)
        outputs[f"snapshot.get_flag:{key}"] = snapshot.get_flag(key)
        outputs[f"snapshot.get_flag_payload:{key}"] = snapshot.get_flag_payload(key)
    client.capture("snapshot-event", distinct_id="snapshot-user", flags=snapshot)
    client.capture("fetched-event", distinct_id="capture-user", send_feature_flags=True)


def getters(shape: str, flags: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Every legacy getter, snapshot read and flag event for one response."""
    keys = [flag["key"] for flag in flags]
    return _with_client(
        [build_response(shape, flags)],
        lambda client, outputs: _read_every_getter(client, outputs, keys),
    )


def _good(shape: str, **fields: Any) -> Dict[str, Any]:
    return build_response(
        shape, [_flag("cached", "blue", payload='{"theme": "dark"}', **fields)]
    )


# Single-flag histories with the memory fallback cache: the responses, in order, to
# the same flag read for the same user.
def cache_histories(shape: str) -> Dict[str, List[Any]]:
    return {
        "good, unreadable record, timeout": [
            _good(shape),
            _good(shape, set={"reason": "oops"}),
            RequestsTimeout("timed out"),
        ],
        "good, each request failure": [
            _good(shape),
            RequestsTimeout("timed out"),
            RequestsConnectionError("refused"),
            APIError(500, "server error"),
            QuotaLimitError(429, "quota limited"),
            ValueError("unexpected"),
        ],
        "good, disabled, timeout": [
            _good(shape),
            build_response(shape, [_flag("cached", False)]),
            RequestsTimeout("timed out"),
        ],
        "good, server failed, timeout": [
            _good(shape),
            _good(shape, failed=True),
            RequestsTimeout("timed out"),
        ],
        "good, missing with errors, timeout": [
            _good(shape),
            build_response(shape, [], errorsWhileComputingFlags=True),
            RequestsTimeout("timed out"),
        ],
        "good, other variant, timeout": [
            _good(shape),
            build_response(shape, [_flag("cached", "green")]),
            RequestsTimeout("timed out"),
        ],
    }


def cache_history(responses: List[Any]) -> Dict[str, Any]:
    def body(client: Client, outputs: Dict[str, Any]) -> None:
        results = []
        for _ in responses:
            # Forget sent events so that every read sends one, with its error.
            client.distinct_ids_feature_flags_reported.clear()
            result = client.get_feature_flag_result("cached", DISTINCT_ID)
            results.append(
                None
                if result is None
                else [result.enabled, result.variant, result.payload, result.reason]
            )
        outputs["results"] = results

    return _with_client(
        responses + [RequestsTimeout("unused")],
        body,
        flag_fallback_cache_url="memory://local/?ttl=300&size=100",
    )


UNREADABLE = _flag("unreadable", "compact", set={"reason": "oops"})
# What a record that cannot be read reads as: disabled, without reason or metadata.
UNREADABLE_AS_DISABLED = _flag("unreadable", False, metadata=None, reason=None)


def run_all(shape: str) -> Dict[str, Any]:
    """Every scenario's results for the given response shape."""
    return {
        "getters": getters(shape, GETTER_FLAGS),
        "getters with an unreadable record": getters(
            shape, [GETTER_FLAGS[2], UNREADABLE]
        ),
        "getters with the unreadable record read as disabled": getters(
            shape, [GETTER_FLAGS[2], UNREADABLE_AS_DISABLED]
        ),
        "cache histories": {
            name: cache_history(responses)
            for name, responses in cache_histories(shape).items()
        },
    }
