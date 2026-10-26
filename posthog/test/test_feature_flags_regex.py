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
    (r"^(a+)+$", "aa\n"),
    (r"^(a+)+$", "aa"),
    (r"^(?:ab*)+$", "ab"),
    (r"^(a{1,2}){2}$", "aa"),
    (r"^(a?b)*$", "ab"),
    (r"^(a+|b)+$", "a"),
    (r"^((a+))+$", "aa"),
    (r"^((a+)b)+$", "ab"),
    (r"^((a+)*b)+$", "aab"),
    (r"^(a+)+?$", "aa"),
    (r"(a+)+", "aa"),
    (r"^\s$", "\x1d"),
    (r"^\S$", "\x1e"),
    (r"^\s+$", "\u2003\x1c"),
    (r"^\w+$", "a\u0301"),
    ("^a.$", "a\ud800"),
    (r"^(a|aa)+$", "aa\n"),
    (r"^a+$", "aa\n"),
    (r"^a*$", "a\n"),
    (r"^a?$", "a\n"),
    (r"^a{2}$", "aa\n"),
    (r"^(?:ab|cd){2,3}$", "abcd\n"),
    (r"^(?:foo|bar)$", "foo\n"),
    (r"foo|bar$", "foo\n"),
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
    (r"^([+*?])+$", "+*?", True),
    (r"^(\++)$", "++", True),
    (r"^(\+)+$", "++", True),
    (r"^\(+a+\)+$", "(aa)", True),
    (r"^(a+)(b+)$", "aabb", True),
    (r"^((ab)){2}$", "abab", True),
    (r"^([()]+)$", "()", True),
    (r"^(a)+(b)+$", "aabb", True),
    (r"^((a)+b)$", "aab", True),
    (r"^(a+)(?:b)+$", "aabb", True),
    ("", "\u0301", True),
    ("^$", "", True),
    (r"^[+*?{}|]$", "+\n", False),
    (r"^\+$", "+\n", False),
    (r"^\|$", "|\n", False),
    (r"^a\{$", "a{\n", False),
    (r"^a+\$", "aa$\n", True),
    (r"^foo\$$", "foo$\n", False),
    (r"^a+", "aa\n", True),
    (r"^(?:a|aa)+$", "aa", True),
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
@pytest.mark.parametrize("pattern", [r"*foo$", r"(a|b$"])
def test_match_property_regex_invalid_precedes_final_newline_policy(pattern, operator):
    assert (
        match_property(property_filter(pattern, operator), {"value": "foo\n"}) is False
    )


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
def test_match_property_regex_compile_resource_failure_is_inconclusive(operator):
    with pytest.raises(InconclusiveMatchError):
        match_property(
            property_filter("(" * 1000 + "a" + ")" * 1000, operator), {"value": "a"}
        )


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize("negation", [False, True])
@pytest.mark.parametrize("pattern", [r"\p{L}+", r"(é+)+"])
def test_match_property_regex_cohort_does_not_negate_inconclusive(
    operator, negation, pattern
):
    prop = {**property_filter(pattern, operator), "negation": negation}
    with pytest.raises(InconclusiveMatchError):
        match_property_group({"type": "AND", "values": [prop]}, {"value": "é"}, {})


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize(
    "pattern,value",
    [(r"\p{L}+", "é"), (r".*\bbot\b.*", "a" * 140), (r"^(a+)+$", "aa\n")],
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


@pytest.mark.parametrize("operator", ["regex", "not_regex"])
@pytest.mark.parametrize(
    "pattern,cases",
    [
        (r"^\w+$", [("abc", True), ("a\u0301", "defer"), ("!", False), ("é١", True)]),
        (
            r"^\s+$",
            [
                (" ", True),
                ("\x1c", "defer"),
                ("\u2003", True),
                ("\u2003\x1c", "defer"),
                ("\ufeff", False),
            ],
        ),
        (r"^a+$", [("aa", True), ("aa\n", "defer"), ("b", False)]),
        (r"^.$", [("😀", True), ("\ud800", "defer"), ("\n", False), ("\r", True)]),
        (r"^é+$", [("é", True), ("é\n", "defer"), ("foo", False)]),
        ("[", [("value", "invalid"), ("value\n", "invalid"), ("\ud800", "invalid")]),
        (r"\p{L}", [("abc", "defer"), ("é", "defer"), ("\n", "defer")]),
    ],
)
def test_match_property_regex_same_pattern_different_values(pattern, cases, operator):
    for sequence in (cases, list(reversed(cases))):
        for value, expected in sequence:
            prop = property_filter(pattern, operator)
            if expected == "defer":
                with pytest.raises(InconclusiveMatchError):
                    match_property(prop, {"value": value})
            elif expected == "invalid":
                assert match_property(prop, {"value": value}) is False
            else:
                assert match_property(prop, {"value": value}) is (
                    expected if operator == "regex" else not expected
                )


@pytest.fixture
def regex_pattern_cache():
    from posthog.feature_flags import _compile_regex

    _compile_regex.cache_clear()
    yield _compile_regex
    _compile_regex.cache_clear()


def test_regex_pattern_cache_reuses_metadata_not_match_results(regex_pattern_cache):
    prop = property_filter("^cached$", "regex")
    assert match_property(prop, {"value": "cached"}) is True
    assert match_property(prop, {"value": "other"}) is False
    assert (
        match_property(property_filter("^cached$", "not_regex"), {"value": "other"})
        is True
    )
    info = regex_pattern_cache.cache_info()
    assert (info.misses, info.hits, info.currsize) == (1, 2, 1)
    metadata = regex_pattern_cache("^cached$")
    assert regex_pattern_cache("^cached$") is metadata
    with pytest.raises(AttributeError):
        metadata.uses_word_class = True


def test_regex_pattern_cache_is_bounded_and_evicts_least_recently_used(
    regex_pattern_cache,
):
    first = regex_pattern_cache("^cache-first$")
    second = regex_pattern_cache("^cache-second$")
    limit = regex_pattern_cache.cache_info().maxsize
    assert limit == 256
    for index in range(limit - 2):
        regex_pattern_cache(f"^cache-{index}$")
    assert regex_pattern_cache("^cache-first$") is first
    regex_pattern_cache("^cache-overflow$")
    info = regex_pattern_cache.cache_info()
    assert info.currsize == limit
    assert regex_pattern_cache("^cache-first$") is first
    assert regex_pattern_cache.cache_info().misses == info.misses
    assert regex_pattern_cache("^cache-second$") is not second
    assert regex_pattern_cache.cache_info().misses == info.misses + 1
    assert regex_pattern_cache.cache_info().currsize == limit


def test_regex_pattern_cache_reuses_invalid_classification(regex_pattern_cache):
    assert regex_pattern_cache("[") is None
    for operator in ("regex", "not_regex"):
        assert match_property(property_filter("[", operator), {"value": "x\n"}) is False
    info = regex_pattern_cache.cache_info()
    assert (info.misses, info.hits, info.currsize) == (1, 2, 1)


def test_regex_pattern_cache_does_not_retain_exceptions(regex_pattern_cache):
    errors = []
    for _ in range(2):
        with pytest.raises(InconclusiveMatchError) as raised:
            regex_pattern_cache(r"\p{L}")
        errors.append(raised.value)
    assert errors[0] is not errors[1]
    info = regex_pattern_cache.cache_info()
    assert (info.misses, info.hits, info.currsize) == (2, 0, 0)


def test_regex_empty_pattern_bypasses_pattern_cache(regex_pattern_cache):
    for operator in ("regex", "not_regex"):
        for value in ("", "é", "\n"):
            assert match_property(property_filter("", operator), {"value": value}) is (
                operator == "regex"
            )
    info = regex_pattern_cache.cache_info()
    assert (info.misses, info.hits, info.currsize) == (0, 0, 0)


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
    cases += [
        ("[", {"value": "x"}, False),
        (r"*foo$", {"value": "foo\n"}, False),
        (r"(a|b$", {"value": "foo\n"}, False),
        ("safe", {}, None),
    ]
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
