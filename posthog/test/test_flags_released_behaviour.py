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

    assert results["getters"] == expected


def test_an_unreadable_record_fails_only_itself(results):
    # 7.82.1 failed the whole response when one record had a field of the wrong
    # type. That record now fails alone and reads as a disabled flag without reason
    # or metadata, and its siblings read as before.
    assert RELEASED["getters with an unreadable record"]["get_all_flags"] == {}

    expected = RELEASED["getters with the unreadable record read as disabled"]
    assert results["getters with an unreadable record"] == expected
    assert results["getters with the unreadable record read as disabled"] == expected


@pytest.mark.parametrize("name", sorted(RELEASED["cache histories"]))
def test_fallback_cache_histories_match_7_82_1(results, name):
    expected = copy.deepcopy(RELEASED["cache histories"][name])
    if name == "good, unreadable record, timeout":
        # 7.82.1 failed the whole response on the unreadable record, so its event
        # had an unknown_error and no request details. The request now succeeds and
        # only the record fails, so the event carries the request details and no
        # error, while the value still falls back to the cached one.
        unreadable_read = expected["events"][1][1]
        assert unreadable_read.pop("$feature_flag_error") == "unknown_error"
        unreadable_read["$feature_flag_request_id"] = scenarios.REQUEST_ID
        unreadable_read["$feature_flag_evaluated_at"] = scenarios.EVALUATED_AT

    assert results["cache histories"][name] == expected
