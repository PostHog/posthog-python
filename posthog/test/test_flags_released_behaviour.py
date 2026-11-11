"""Legacy flag results pinned to the results posthog 7.82.1 recorded.

``fixtures/released_flags_7_82_1.json`` was recorded with posthog 7.82.1 from the
``v2`` response shape; ``released_flags_scenarios.py`` describes the scenarios and how
to regenerate the file. Each scenario runs here from the ``v2`` shape and from the
``v4`` shape of the same flags, so a change shared by both readers is caught too.
Where the expected result differs from 7.82.1, the test says which decision makes
it differ.
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from posthog.test import released_flags_scenarios as scenarios

RELEASED = json.loads(
    (Path(__file__).with_name("fixtures") / "released_flags_7_82_1.json").read_text(
        encoding="utf-8"
    )
)["results"]
SHAPES = ["v2", "v4"]


@pytest.fixture(scope="module", params=SHAPES)
def results(request: Any) -> Dict[str, Any]:
    return scenarios.run_all(request.param)


def test_every_legacy_getter_and_event_matches_7_82_1(results):
    expected = copy.deepcopy(RELEASED["getters"])
    # A record without a flag ID or version reads them as absent (None), where 7.82.1
    # read 0. Events already left out both, so only the parsed record differs.
    sparse = expected["get_flags_decision"]["flags"]["sparse"]["metadata"]
    assert (sparse["id"], sparse["version"]) == (0, 0)
    sparse["id"] = sparse["version"] = None

    # A failed record reads as disabled, so it has no payload, even when the record
    # carries one. 7.82.1 returned that payload from the payload reads and sent it on
    # the flag's $feature_flag_called events.
    failed = expected["get_flags_decision"]["flags"]["server-failed"]["metadata"]
    assert failed["payload"] == '{"columns": 2}'
    failed["payload"] = None
    for read in ["get_feature_flag_payload", "snapshot.get_flag_payload"]:
        assert expected[f"{read}:server-failed"] == {"columns": 2}
        expected[f"{read}:server-failed"] = None
    assert expected["get_feature_flag_result:server-failed"][2] == {"columns": 2}
    expected["get_feature_flag_result:server-failed"][2] = None
    failed_events = [
        properties
        for _, properties in expected["events"]
        if properties.get("$feature_flag") == "server-failed"
    ]
    assert failed_events
    for properties in failed_events:
        assert properties.pop("$feature_flag_payload") == {"columns": 2}

    assert results["getters"] == expected


def test_an_unreadable_record_fails_only_itself(results):
    # 7.82.1 failed the whole response when one record had a field of the wrong
    # type. That record now fails alone and reads as a disabled flag without reason
    # or metadata, and its siblings read as before.
    assert RELEASED["getters with an unreadable record"]["get_all_flags"] == {}

    as_disabled = RELEASED["getters with the unreadable record read as disabled"]
    assert results["getters with the unreadable record read as disabled"] == as_disabled

    # A single-flag read of the unreadable record still reports what 7.82.1
    # reported when the whole response failed: an unknown_error and no request
    # details. Only its value differs, disabled instead of None.
    expected = copy.deepcopy(as_disabled)
    released_events = RELEASED["getters with an unreadable record"]["events"]
    for event, released in zip(expected["events"], released_events):
        if (
            released[1].get("$feature_flag") == "unreadable"
            and released[1].get("$feature_flag_error") == "unknown_error"
        ):
            event[1].pop("$feature_flag_request_id")
            event[1].pop("$feature_flag_evaluated_at")
            event[1]["$feature_flag_error"] = "unknown_error"
    assert results["getters with an unreadable record"] == expected


@pytest.mark.parametrize("name", sorted(RELEASED["cache histories"]))
def test_fallback_cache_histories_match_7_82_1(results, name):
    # An unreadable record now fails only itself, but a single-flag read of it
    # still reports what 7.82.1 reported when the whole response failed: an
    # unknown_error, no request details, and the cached value.
    expected = copy.deepcopy(RELEASED["cache histories"][name])
    if name == "good, server failed, timeout":
        # A failed record has no payload, even when the record carries one, so
        # neither its read nor the cached result read after the timeout returns
        # one, and their events leave it out. 7.82.1 returned it from both.
        for result in expected["results"][1:]:
            assert result[2] == {"theme": "dark"}
            result[2] = None
        for _, properties in expected["events"][1:]:
            assert properties.pop("$feature_flag_payload") == {"theme": "dark"}
    assert results["cache histories"][name] == expected
