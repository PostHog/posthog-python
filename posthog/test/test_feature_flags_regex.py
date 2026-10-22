"""Regex compatibility with the flags service's Rust matcher, not just Python re."""

from unittest import mock

import pytest

from posthog.client import Client
from posthog.feature_flags import (
    InconclusiveMatchError,
    match_cohort,
    match_property,
    match_property_group,
)
from posthog.test.test_utils import FAKE_TEST_API_KEY


pytestmark = pytest.mark.filterwarnings(
    r"ignore:`get_feature_flag` is deprecated:DeprecationWarning"
)


# These constructs either have different native semantics or require service syntax/budgets.
DEFERRED = [
    (r"^\p{L}+$", "é"),
    (r"^\p{L}+$", None),
    (r"^\P{L}+$", "123"),
    (r"(?<=a+)b", "aab"),
    (r"a(?=b)", "ab"),
    (r"(a)\1", "aa"),
    (r"(?i)^foo$", "FOO"),
    (r"(?m)^foo$", "foo\nbar"),
    (r"\Afoo\z", "foo"),
    (r"^[a&&b]$", "a"),
    (r"^[a-z&&[^aeiou]]+$", "bcdf"),
    (r"^[a-z--b]$", "b"),
    (r"^[a~~b]$", "~"),
    (r"^[[:alpha:]]+$", "foo"),
    (r"[]", "a"),
    (r"[^]", "a"),
    (r"\141", "a"),
    (r"\q", "q"),
    (r"^\x{41}$", "A"),
    (r"a++", "aa"),
    (r"a{,2}", "aa"),
    (r"a*{2}", "aa"),
    (r"{foo}", "{foo}"),
    (r"[\s-9]", "-"),
    (r"[a-\d]", "-"),
    (r"^?https?://", "https://a"),
    (r"$?", "x"),
    (r"\b+", "x"),
    (r"{2}", "x"),
    (r"a|{2}", "x"),
    (r"({2})", "x"),
    (r"(?:{2})", "x"),
    (r"(?:)*", "x"),
    (r"()+", "x"),
    (r"(?:){2}", "x"),
    (r"a{0,4294967296}", "x"),
    (r"a{4294967296,}", "x"),
    (r".*\bbot\b.*", "a" * 140),
    (r".*\Bbot\B.*", "a" * 140),
    (r"\<foo\>", "foo"),
    (r"[\@]", "@"),
    (r"[\B]", "B"),
    (r"^\w+$", "\u0301"),  # Rust includes combining marks; Python does not.
    (r"^\W+$", "²"),  # Python includes non-decimal numbers; Rust does not.
    (r"^\s$", "\x1c"),  # Python's extra ASCII whitespace is not Rust whitespace.
    (r"^\S$", "\x1f"),
    ("^.$", "\ud800"),
    ("\ud800", "x"),
]

LOCAL = [
    ("pro", "a pro plan", True),
    ("^pro$", "a pro plan", False),
    ("^pro$", "PRO", False),
    (r"^\d+$", "123", True),
    (r"^\d+$", "١", True),
    (r"^\D+$", "é", True),
    (r"^\w+$", "abc_123", True),
    (r"^\w+$", "é١", True),
    (r"^\W+$", "é١", False),
    (r"^\s$", "\u2003", True),
    (r"^\s$", "\ufeff", False),
    ("^.$", "😀", True),
    ("^é+$", "éé", True),
    ("^.$", "\r", True),
    (r"^[\w.+-]+@example\.com$", "a.b+tag@example.com", True),
    (r"^(?:ab|cd){2,3}$", "abcdab", True),
    (r"^\\p\{L\}$", r"\p{L}", True),
    (r"^\(\?i\)$", "(?i)", True),
    (r"^\x41+$", "AA", True),
    (r"^nu", None, True),
    (r"^\++$", "++", True),
    (r"[\b]", "\b", True),
    (r"^a\-b$", "a-b", True),
    (r"\@example\.com$", "a@example.com", True),
    (r"^a\:b$", "a:b", True),
    (r"^a\_b$", "a_b", True),
    (r"^a\=b$", "a=b", True),
    (r"^a\ b$", "a b", True),
    (r"^\^?$", "", True),
    (r"^\${2}$", "$$", True),
    (r"^\({2}$", "((", True),
    (r"^\|{2}$", "||", True),
    (r"^[\^$?]+$", "$?^", True),
    (r"^[\w-]+$", "a-b", True),
    ("", "", True),
    ("", "é", True),
    ("", None, True),
    ("", "\r", True),
    ("^foo$", "foo\n", False),
    ("^foo$", "foo\nbar", False),
    ("foo$", "foo\r\n", False),
]


def property_filter(pattern, operator):
    return {"key": "value", "type": "person", "operator": operator, "value": pattern}


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize("pattern,value", DEFERRED)
def test_match_property_regex_defers_unsupported(pattern, value, operator):
    with pytest.raises(InconclusiveMatchError):
        match_property(property_filter(pattern, operator), {"value": value})


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize("pattern,value,expected", LOCAL)
def test_match_property_regex_keeps_portable_patterns_local(
    pattern, value, expected, operator
):
    assert match_property(property_filter(pattern, operator), {"value": value}) is (
        expected if operator == "regex" else not expected
    )


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize("pattern", ["[", "(", "*", "?*"])
@pytest.mark.parametrize("value", ["value", "é", "\r"])
def test_match_property_regex_invalid_is_false_for_both_operators(
    pattern, value, operator
):
    assert match_property(property_filter(pattern, operator), {"value": value}) is False


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
def test_match_property_regex_compile_resource_failure_is_inconclusive(operator):
    with pytest.raises(InconclusiveMatchError):
        match_property(
            property_filter("(" * 1000 + "a" + ")" * 1000, operator), {"value": "a"}
        )


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize("negation", [False, True])
def test_match_property_regex_cohort_does_not_negate_inconclusive(operator, negation):
    prop = {**property_filter(r"\p{L}+", operator), "negation": negation}
    with pytest.raises(InconclusiveMatchError):
        match_property_group({"type": "AND", "values": [prop]}, {"value": "é"}, {})


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize(
    "pattern,value", [(r"\p{L}+", "é"), (r".*\bbot\b.*", "a" * 140)]
)
def test_match_cohort_regex_not_in_does_not_negate_inconclusive(
    operator, pattern, value
):
    cohorts = {"1": {"type": "AND", "values": [property_filter(pattern, operator)]}}
    with pytest.raises(InconclusiveMatchError):
        match_cohort(
            {"value": 1, "operator": "not_in", "type": "cohort"},
            {"value": value},
            cohorts,
        )


@pytest.fixture
def client():
    instance = Client(FAKE_TEST_API_KEY, send=False)
    yield instance
    instance.shutdown()


@pytest.mark.parametrize("api", ["evaluate_flags", "get_feature_flag"])
@pytest.mark.parametrize("operator", ["regex", "not_regex"])
def test_regex_public_fallback_boundaries(client, api, operator):
    cases = [(p, {"value": v}, None) for p, v in DEFERRED]
    cases += [
        (p, {"value": v}, expected if operator == "regex" else not expected)
        for p, v, expected in LOCAL
    ]
    cases += [("[", {"value": "x"}, False), ("safe", {}, None)]
    with (
        mock.patch("posthog.client.flags") as remote,
        mock.patch.object(Client, "capture"),
    ):
        remote.return_value = {
            "flags": {"regex-flag": {"enabled": True, "variant": None}}
        }
        for pattern, properties, local_result in cases:
            client.feature_flags = [
                {
                    "id": 1,
                    "key": "regex-flag",
                    "active": True,
                    "filters": {
                        "groups": [
                            {
                                "properties": [property_filter(pattern, operator)],
                                "rollout_percentage": 100,
                            }
                        ]
                    },
                }
            ]
            for local_only in (False, True):
                remote.reset_mock()
                kwargs = {
                    "person_properties": properties,
                    "only_evaluate_locally": local_only,
                }
                if api == "evaluate_flags":
                    result = client.evaluate_flags("user-1", **kwargs).get_flag(
                        "regex-flag"
                    )
                else:
                    result = client.get_feature_flag(
                        "regex-flag", "user-1", send_feature_flag_events=False, **kwargs
                    )
                expected = (
                    (None if local_only else True)
                    if local_result is None
                    else local_result
                )
                assert result is expected, (
                    api,
                    operator,
                    pattern,
                    properties,
                    local_only,
                )
                assert remote.call_count == int(local_result is None and not local_only)
