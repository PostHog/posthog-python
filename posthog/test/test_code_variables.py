import collections
import functools
import json
import os
import subprocess
import sys
import types
from dataclasses import dataclass
from textwrap import dedent

import pytest

from posthog.exception_utils import (
    DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS,
    DEFAULT_CODE_VARIABLES_MASK_PATTERNS,
    VariableSizeLimiter,
    _MAX_COLLECTION_ITEMS_TO_SCAN,
    _MAX_MASK_DEPTH,
    _MAX_VALUE_LENGTH_FOR_PATTERN_MATCH,
    _MaskingConfig,
    _compile_patterns,
    _encode_variable,
    _is_high_entropy_secret,
    _looks_like_secret,
    _mask_value,
    _pattern_matches,
    _redact_url_credentials,
    _safe_repr,
    _serialize_frame_variables,
    attach_code_variables_to_frames,
    iter_stacks,
)
from posthog.exception_utils import (
    CODE_VARIABLES_REDACTED_VALUE as REDACTED,
)
from posthog.exception_utils import (
    CODE_VARIABLES_TOO_LONG_VALUE as TOO_LONG,
)

# --- shared helpers ------------------------------------------------------------------


def make_config(
    *, patterns=DEFAULT_CODE_VARIABLES_MASK_PATTERNS, ignore=(), mask_urls=True
):
    """Build a masking config, defaulting to the patterns the SDK ships with."""
    return _MaskingConfig.build(
        list(patterns), list(ignore), mask_url_credentials=mask_urls
    )


def mask(value, **kwargs):
    """Run one value through the recursive masker and return the masked result."""
    return _mask_value(value, make_config(**kwargs))


def encode(value, *, limiter=None, **kwargs):
    """Run one top-level variable through the full wire encoder (mask + format)."""
    return _encode_variable(
        value, make_config(**kwargs), limiter or VariableSizeLimiter()
    )


def extract(
    *,
    mask_patterns=DEFAULT_CODE_VARIABLES_MASK_PATTERNS,
    ignore_patterns=DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS,
    **local_vars,
):
    """Serialize a frame's locals. Pass the locals you want as keyword arguments."""
    config = _MaskingConfig.build(list(mask_patterns), list(ignore_patterns), True)
    frame = types.SimpleNamespace(f_locals=local_vars)
    return _serialize_frame_variables(frame, VariableSizeLimiter(), config)


_APP_HEADER = """\
import os
import posthog
from posthog import Posthog


def make_client(**options):
    return Posthog(
        "phc_x",
        host="https://eu.i.posthog.com",
        debug=True,
        enable_exception_autocapture=True,
        project_root=os.path.dirname(os.path.abspath(__file__)),
        **options,
    )
"""


def run_app(tmpdir, body, *, env=None):
    """Write and run a tiny PostHog app that raises an uncaught exception.

    The app already has ``make_client(**options)`` and ``posthog`` imported; the body
    creates the client, defines the failing code, and triggers it. In debug mode the
    autocaptured payload (including ``code_variables``) is printed, which is what the
    end-to-end tests assert against. Returns the combined stdout/stderr.
    """
    app = tmpdir.join("app.py")
    app.write(_APP_HEADER + "\n" + dedent(body))
    run_env = {**os.environ, **(env or {})}
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.check_output(
            [sys.executable, str(app)], stderr=subprocess.STDOUT, env=run_env
        )
    return excinfo.value.output.decode("utf-8")


# --- 1. pattern compilation ----------------------------------------------------------


class TestPatternCompilation:
    """Patterns compile into a (substrings, regexes) pair; simple ones take a fast path."""

    def test_simple_case_insensitive_pattern_becomes_a_substring(self):
        # "(?i)password" is just a case-insensitive contains-check -> plain substring
        substrings, regexes = _compile_patterns([r"(?i)password"])
        assert substrings == ["password"]
        assert regexes == []

    def test_complex_pattern_stays_a_regex(self):
        # "^sk_live_" needs anchoring -> kept as a compiled regex
        substrings, regexes = _compile_patterns([r"^sk_live_"])
        assert substrings == []
        assert len(regexes) == 1

    def test_patterns_can_mix_substrings_and_regexes(self):
        substrings, regexes = _compile_patterns([r"(?i)secret", r"^__.*"])
        assert substrings == ["secret"]
        assert len(regexes) == 1

    def test_no_patterns_compiles_to_none(self):
        assert _compile_patterns([]) is None

    def test_substring_match_is_case_insensitive(self):
        patterns = _compile_patterns([r"(?i)password"])
        assert _pattern_matches("MY_PASSWORD", patterns) is True
        assert _pattern_matches("safe_name", patterns) is False

    def test_regex_match_is_anchored_where_the_pattern_says(self):
        patterns = _compile_patterns([r"^__"])
        assert _pattern_matches("__private", patterns) is True  # starts with __
        assert _pattern_matches("trailing__", patterns) is False  # __ not at start


# --- 2. URL credential scrubbing -----------------------------------------------------


class TestUrlCredentialScrubbing:
    """`scheme://user:pass@host` credentials are stripped from any string."""

    @pytest.mark.parametrize(
        "url, secret",
        [
            ("postgresql://warehouse:topsecret@db:26257/x", "topsecret"),
            ("redis://:p4ss@cache:6379", "p4ss"),  # password-only userinfo
            ("mongodb://admin:hush@mongo:27017", "hush"),
            ("https://admin:hush@api.example.com/v1", "hush"),
        ],
    )
    def test_embedded_credentials_are_removed(self, url, secret):
        # scheme://user:<secret>@host  ->  scheme://<redacted>@host
        result = _redact_url_credentials(url)
        assert secret not in result
        assert REDACTED in result

    def test_every_url_in_the_string_is_scrubbed(self):
        # two DSNs in one string -> both credentials gone
        result = _redact_url_credentials("a=postgres://u:p1@h1 b=redis://u:p2@h2")
        assert "p1" not in result and "p2" not in result

    def test_ipv6_host_is_preserved(self):
        # redis://user:<secret>@[::1]:6379  ->  host kept, credential gone
        result = _redact_url_credentials("redis://user:secret@[::1]:6379")
        assert result == "redis://" + REDACTED + "@[::1]:6379"

    @pytest.mark.parametrize(
        "value",
        [
            "ssh://gituser@github.com/repo",  # bare username, no password slot
            "https://api.example.com:8080/v1",  # port but no credentials
            "just a plain string",  # not a URL at all
        ],
    )
    def test_strings_without_credentials_are_left_untouched(self, value):
        assert _redact_url_credentials(value) == value


# --- 3. scalar masking ---------------------------------------------------------------


class TestScalarMasking:
    """Simple scalars pass through untouched; non-finite floats become strings."""

    @pytest.mark.parametrize("value", [None, True, False, 42, -7, 3.14, "plain text"])
    def test_safe_scalars_pass_through_unchanged(self, value):
        assert mask(value) == value

    @pytest.mark.parametrize(
        "value, expected",
        [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")],
    )
    def test_non_finite_floats_become_strings(self, value, expected):
        # NaN/Infinity are invalid JSON, so they are rendered as strings instead
        assert mask(value) == expected

    def test_non_finite_floats_are_converted_even_when_nested(self):
        # [inf, 2.0]  ->  ["inf", 2.0]   (so json.dumps never sees a NaN/Infinity token)
        assert mask([float("inf"), 2.0]) == ["inf", 2.0]


# --- 4. string masking ---------------------------------------------------------------


class TestStringMasking:
    """A string is redacted by content, capped by length, and scrubbed of URL creds."""

    def test_plain_string_passes_through(self):
        assert mask("hello world") == "hello world"

    def test_string_matching_a_pattern_is_redacted(self):
        # the value itself contains "password" -> redact the whole string
        assert mask("contains_password_here") == REDACTED

    def test_overly_long_string_is_replaced(self):
        # too long to scan within budget -> placeholder, not the raw value
        assert mask("x" * (_MAX_VALUE_LENGTH_FOR_PATTERN_MATCH + 1)) == TOO_LONG

    def test_url_credentials_are_scrubbed_even_with_no_mask_patterns(self):
        # name masking off, URL scrubbing on (they are independent toggles)
        result = mask("postgresql://user:p4ss@host/db", patterns=[])
        assert "p4ss" not in result
        assert REDACTED in result

    def test_url_scrubbing_can_be_turned_off(self):
        result = mask("postgresql://user:p4ss@host/db", patterns=[], mask_urls=False)
        assert result == "postgresql://user:p4ss@host/db"


# --- 5. collection masking -----------------------------------------------------------


class TestCollectionMasking:
    """Dicts, lists and tuples are walked; size, depth and cycles are all bounded."""

    def test_safe_dict_is_unchanged(self):
        # {"name": "test", "value": 123}  ->  unchanged
        assert mask({"name": "test", "value": 123}) == {"name": "test", "value": 123}

    def test_dict_key_matching_a_pattern_redacts_its_value(self):
        # {"password": ...}  ->  value redacted on the strength of the key name alone
        assert mask({"password": "anything"}) == {"password": REDACTED}

    def test_dict_value_matching_a_pattern_is_redacted(self):
        # {"note": "...password..."}  ->  value redacted because the value matches
        assert mask({"note": "contains_password_here"}) == {"note": REDACTED}

    def test_nested_dict_is_masked_at_every_depth(self):
        # {"l1": {"l2": {"api_key": <secret>, "safe": "ok"}}}
        out = mask({"l1": {"l2": {"api_key": "xyz", "safe": "ok"}}})
        assert out["l1"]["l2"]["api_key"] == REDACTED
        assert out["l1"]["l2"]["safe"] == "ok"

    def test_list_items_are_masked_individually(self):
        # ["safe", "...password...", "safe2"]  ->  only the middle item is redacted
        assert mask(["safe", "contains_password_here", "safe2"]) == [
            "safe",
            REDACTED,
            "safe2",
        ]

    def test_tuple_stays_a_tuple(self):
        # ("a", "secret_token", "b")  ->  still a tuple, middle redacted by value
        out = mask(("a", "secret_token", "b"))
        assert isinstance(out, tuple)
        assert out == ("a", REDACTED, "b")

    def test_list_of_dicts_is_masked(self):
        # [{"id": 1, "password": <secret>}, {"id": 2, "value": "ok"}]
        out = mask([{"id": 1, "password": "x"}, {"id": 2, "value": "ok"}])
        assert out == [{"id": 1, "password": REDACTED}, {"id": 2, "value": "ok"}]

    def test_overly_long_dict_key_replaces_only_that_entry(self):
        # {"short": "ok", <very long key>: ..., "password": ...}
        long_key = "k" * 20000
        out = mask({"short": "ok", long_key: "v", "password": "x"})
        assert out["short"] == "ok"
        assert out[long_key] == TOO_LONG
        assert out["password"] == REDACTED

    @pytest.mark.parametrize(
        "build",
        [
            lambda n: {f"key{i}": i for i in range(n)},
            lambda n: list(range(n)),
            lambda n: tuple(range(n)),
        ],
        ids=["dict", "list", "tuple"],
    )
    def test_collection_with_too_many_items_is_replaced(self, build):
        # over the item cap -> placeholder; comfortably under it -> masked normally
        assert mask(build(_MAX_COLLECTION_ITEMS_TO_SCAN + 1)) == TOO_LONG
        assert mask(build(2)) != TOO_LONG

    def test_circular_dict_reference_is_detected(self):
        # d = {"key": "value", "self": d}
        d = {"key": "value"}
        d["self"] = d
        out = mask(d)
        assert out["key"] == "value"
        assert out["self"] == "<circular ref>"

    def test_circular_list_reference_is_detected(self):
        # items = ["item", items]
        items = ["item"]
        items.append(items)
        out = mask(items)
        assert out[0] == "item"
        assert out[1] == "<circular ref>"

    def test_non_string_dict_key_is_coerced_to_stay_serializable(self):
        # {(1, 2): "ok"}  ->  key stringified so the masked dict is always JSON-safe
        assert mask({(1, 2): "ok"}) == {"(1, 2)": "ok"}

    def test_non_string_dict_key_still_redacts_on_its_name(self):
        # a key whose text matches a pattern redacts its value, just like a string key
        assert mask({("db", "password"): "x"}) == {"('db', 'password')": REDACTED}

    def test_non_string_dict_key_does_not_defeat_value_masking(self):
        # a tuple key used to break json.dumps and fall back to a repr of the *original*
        # dict, leaking everything - the value must still be masked structurally
        class Conn:
            def __init__(self):
                self.password = "hunter2"

            def __repr__(
                self,
            ):  # repr hides the field name, so only name-masking saves it
                return f"Conn({self.password})"

        out = mask({(1, 2): Conn()})
        assert out["(1, 2)"]["password"] == REDACTED
        assert "hunter2" not in str(out)

    def test_sequence_subclass_that_cannot_be_rebuilt_falls_back_to_a_list(self):
        # a subclass whose __new__ won't take a single iterable must not raise out of
        # masking (which would repr the original); its items are already masked
        class Pair(tuple):
            def __new__(cls, a, b):
                return super().__new__(cls, (a, b))

        assert mask(Pair("ok", "contains_password_here")) == ["ok", REDACTED]

    def test_total_node_budget_caps_runaway_structures(self):
        # depth and per-collection caps are per-level; a moderately wide+deep tree still
        # blows past the *total* node budget -> truncated, so masking stays cheap
        def tree(width, depth):
            if depth == 0:
                return {}
            return {f"k{i}": tree(width, depth - 1) for i in range(width)}

        assert TOO_LONG in json.dumps(mask(tree(8, 3)))  # ~580 nodes, over the budget
        assert TOO_LONG not in json.dumps(mask(tree(4, 2)))  # ~20 nodes, well under


# --- 6. object traversal -------------------------------------------------------------


class TestObjectTraversal:
    """Custom objects are decomposed into their real fields, so a `password` attribute
    is caught by name instead of leaking through repr()."""

    def test_dataclass_is_decomposed_into_its_fields(self):
        # Config(host="db", user="wh", password=<secret>)  ->  dict; password redacted
        @dataclass
        class Config:
            host: str
            user: str
            password: str

        out = mask(Config("db.example.com", "warehouse", "uHjH9secret"))
        assert out["host"] == "db.example.com"
        assert out["user"] == "warehouse"
        assert out["password"] == REDACTED
        assert "Config" in out["__class__"]
        assert "uHjH9secret" not in str(out)

    def test_plain_object_is_decomposed_via_its_dict(self):
        # obj.username="alice", obj.api_key=<secret>
        class Credentials:
            def __init__(self):
                self.username = "alice"
                self.api_key = "sk_live_abc123"

        out = mask(Credentials())
        assert out["username"] == "alice"
        assert out["api_key"] == REDACTED
        assert "sk_live_abc123" not in str(out)

    def test_object_nested_in_a_tuple_is_still_masked(self):
        # (Config(password=<secret>), Inputs(schema_name="traffic"))
        @dataclass
        class Config:
            host: str
            password: str

        @dataclass
        class Inputs:
            schema_name: str

        out = mask((Config("db", "topsecret"), Inputs("traffic")))
        assert out[0]["host"] == "db"
        assert out[0]["password"] == REDACTED
        assert out[1]["schema_name"] == "traffic"
        assert "topsecret" not in str(out)

    def test_object_with_too_many_attributes_is_replaced(self):
        class Wide:
            def __init__(self, n):
                for i in range(n):
                    setattr(self, f"attr{i}", i)

        assert mask(Wide(_MAX_COLLECTION_ITEMS_TO_SCAN + 1)) == TOO_LONG
        assert isinstance(mask(Wide(2)), dict)

    def test_shallow_object_secret_is_redacted_by_name(self):
        # Box(password=<secret>)  ->  field redacted, secret absent
        @dataclass
        class Box:
            password: str

        out = mask(Box("hunter2"))
        assert out["password"] == REDACTED
        assert "hunter2" not in str(out)

    @pytest.mark.parametrize("depth", [_MAX_MASK_DEPTH, _MAX_MASK_DEPTH + 5])
    def test_secret_past_the_depth_limit_does_not_leak(self, depth):
        # Node(Node(...Node(Box(password=<secret>))))  nested past the depth cap.
        # Box.__repr__ hides the field name, so only name-based traversal could catch
        # it - past the cap we must fail closed and never emit the repr.
        @dataclass
        class Box:
            password: str

            def __repr__(self):
                return f"Box({self.password})"

        class Node:
            def __init__(self, child):
                self.child = child

        value = Box("hunter2")
        for _ in range(depth):
            value = Node(value)
        assert "hunter2" not in str(mask(value))

    def test_structure_past_the_depth_limit_degrades_to_a_placeholder(self):
        # [[[ ... ["leaf"] ... ]]] nested past the cap  ->  placeholder, leaf dropped
        value = "leaf"
        for _ in range(_MAX_MASK_DEPTH + 1):
            value = [value]
        out = mask(value)
        assert TOO_LONG in str(out)
        assert "leaf" not in str(out)

    def test_namedtuple_is_decomposed_by_field_name(self):
        # Creds(user="alice", password=<secret>)  ->  dict; password redacted by name
        Creds = collections.namedtuple("Creds", ["user", "password"])
        out = mask(Creds("alice", "uHjH9secret"))
        assert out["user"] == "alice"
        assert out["password"] == REDACTED
        assert "Creds" in out["__class__"]
        assert "uHjH9secret" not in str(out)

    def test_namedtuple_field_is_caught_by_name_not_repr(self):
        # a custom __repr__ that hides the field names can't relabel a sensitive field
        # out of the mask: we traverse the real fields, not the repr
        class Token(collections.namedtuple("Token", ["label", "secret"])):
            def __repr__(self):
                return f"<{self.label}>"

        out = mask(Token("prod", "sk_live_xyz"))
        assert out["secret"] == REDACTED
        assert "sk_live_xyz" not in str(out)

    def test_sensitively_named_property_is_not_leaked_through_repr(self):
        # a @property lives on the class, not in __dict__, so attribute traversal never
        # sees it - but a custom __repr__ can expose it. Catch it by name, don't repr.
        class Config:
            @property
            def password(self):
                return "hunter2"

            def __repr__(self):  # hides the field name, so the repr scan can't catch it
                return f"Config({self.password})"

        out = mask(Config())
        assert out["password"] == REDACTED
        assert "Config" in out["__class__"]
        assert "hunter2" not in str(out)

    def test_sensitively_named_cached_property_is_not_leaked(self):
        # functools.cached_property is a class descriptor until first access, so an
        # un-accessed one isn't in __dict__ either - catch it by name like @property
        class Client:
            @functools.cached_property
            def api_key(self):
                return "sk_live_xyz"

            def __repr__(self):
                return f"Client({self.api_key})"

        out = mask(Client())
        assert out["api_key"] == REDACTED
        assert "sk_live_xyz" not in str(out)

    def test_sensitively_named_slot_is_redacted_by_name(self):
        # a __slots__ value lives outside __dict__; a custom __repr__ that hides the slot
        # name would otherwise leak it, so catch the slot by name
        class Session:
            __slots__ = ("token",)

            def __init__(self, token):
                self.token = token

            def __repr__(self):  # hides the slot name
                return f"Session({self.token})"

        out = mask(Session("sk_live_xyz"))
        assert out["token"] == REDACTED
        assert "sk_live_xyz" not in str(out)

    def test_sensitively_named_class_attribute_is_redacted_by_name(self):
        # a class-level data attribute also lives outside instance __dict__; redact it by
        # name so a custom __repr__ can't leak it
        class Config:
            password = "hunter2"

            def __repr__(self):
                return f"Config({self.password})"

        out = mask(Config())
        assert out["password"] == REDACTED
        assert "hunter2" not in str(out)

    def test_non_sensitive_members_are_left_to_the_normal_repr(self):
        # only members whose *name* matches the mask are redacted; a plain property/slot
        # must not trip redaction (and no getter is ever called)
        class Box:
            __slots__ = ("region",)

            def __init__(self):
                self.region = "us-east-1"

            @property
            def host(self):
                return "db.example.com"

            def __repr__(self):
                return f"Box({self.host}/{self.region})"

        assert mask(Box()) == "Box(db.example.com/us-east-1)"


# --- 7. opaque repr fallback ---------------------------------------------------------


class TestOpaqueReprFallback:
    """Slotted objects have no __dict__ to walk, so they fall back to a fail-closed
    repr: keep it only if nothing about it looks sensitive."""

    def test_repr_is_kept_when_nothing_looks_sensitive(self):
        class Point:
            __slots__ = ("x", "y")

            def __repr__(self):
                return "Point(x=1, y=2)"

        assert _safe_repr(Point(), make_config()) == "Point(x=1, y=2)"

    def test_whole_value_is_redacted_when_repr_mentions_a_secret(self):
        # repr embeds the word "password" -> redact the entire repr, don't emit it.
        # No maskable member here (empty slots), so this exercises the repr fallback.
        class Creds:
            __slots__ = ()

            def __repr__(self):
                return "Creds(password=s3cr3t)"

        assert _safe_repr(Creds(), make_config()) == REDACTED
        assert mask(Creds()) == REDACTED  # same outcome through the masker

    def test_broken_repr_yields_a_type_placeholder(self):
        # a __repr__ that raises must neither crash capture nor leak its fields
        class Boom:
            __slots__ = ("secret",)

            def __init__(self):
                self.secret = "leak123"

            def __repr__(self):
                raise RuntimeError("boom")

        result = _safe_repr(Boom(), make_config())
        assert "leak123" not in result
        assert result.startswith("<") and result.endswith(">")
        assert "Boom" in result  # a type-name placeholder, nothing from the instance

    def test_overly_long_repr_is_redacted(self):
        # a repr too long to scan can't be vouched for -> redact it whole
        class Huge:
            __slots__ = ("payload",)

            def __init__(self, payload):
                self.payload = payload

            def __repr__(self):
                return self.payload

        payload = "topsecret" + "x" * (_MAX_VALUE_LENGTH_FOR_PATTERN_MATCH + 1)
        assert _safe_repr(Huge(payload), make_config()) == REDACTED

    def test_url_credentials_in_repr_are_scrubbed(self):
        # repr embeds a connection string -> credential scrubbed, rest of repr kept
        class Conn:
            __slots__ = ()

            def __repr__(self):
                return "Conn(url=postgresql://user:leakpw@db/app)"

        assert "leakpw" not in _safe_repr(Conn(), make_config())
        assert "leakpw" in _safe_repr(Conn(), make_config(mask_urls=False))


# --- 8. variable encoding ------------------------------------------------------------


class TestVariableEncoding:
    """The top-level encoder decides the wire format: numbers stay raw, everything else
    becomes a string, and the size budget is enforced."""

    @pytest.mark.parametrize("value", [0, 42, -7, 3.14, 1.0])
    def test_numbers_stay_raw_json_numbers(self, value):
        assert encode(value) == value

    def test_none_and_booleans_become_strings(self):
        assert encode(None) == "None"
        assert encode(True) == "True"
        assert encode(False) == "False"

    @pytest.mark.parametrize(
        "value, expected",
        [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")],
    )
    def test_non_finite_floats_become_strings(self, value, expected):
        assert encode(value) == expected

    def test_string_is_emitted_unchanged(self):
        assert encode("hello world") == "hello world"

    def test_dict_becomes_a_json_string(self):
        # {"name": "test", "value": 123}  ->  '{"name": "test", "value": 123}'
        assert (
            encode({"name": "test", "value": 123}) == '{"name": "test", "value": 123}'
        )

    def test_nested_non_finite_floats_keep_the_json_strict_valid(self):
        # {"ratio": inf, "ok": 1.5}  ->  inf becomes "inf", output parses as strict JSON
        out = encode({"ratio": float("inf"), "ok": 1.5})
        assert "NaN" not in out and "Infinity" not in out
        assert json.loads(out) == {"ratio": "inf", "ok": 1.5}

    def test_value_is_truncated_to_the_length_budget(self):
        out = encode("a" * 2000)
        assert len(out) == 1024
        assert out.endswith("...")

    def test_value_is_dropped_when_the_shared_budget_is_exhausted(self):
        limiter = VariableSizeLimiter(max_size=4)
        assert encode("ok", limiter=limiter) == "ok"  # 2 bytes fit
        assert encode("overflow", limiter=limiter) is None  # budget already spent

    def test_dict_with_a_non_string_key_encodes_to_valid_json(self):
        # the tuple key used to break json.dumps and leak a repr of the original dict;
        # now it stays strict JSON with the sensitive value masked
        out = encode({(1, 2): {"password": "hunter2"}})
        assert json.loads(out) == {"(1, 2)": {"password": REDACTED}}
        assert "hunter2" not in out

    def test_namedtuple_encodes_to_valid_json(self):
        # Creds(user, password)  ->  a JSON object keyed by field name, password masked
        Creds = collections.namedtuple("Creds", ["user", "password"])
        out = encode(Creds("alice", "hunter2"))
        assert json.loads(out)["password"] == REDACTED
        assert "hunter2" not in out


# --- 9. frame variable extraction ----------------------------------------------------


class TestFrameVariableExtraction:
    """Reading a frame's locals: ignored names dropped, masked names redacted, scalars
    ordered ahead of complex values."""

    def test_simple_and_complex_locals_are_serialized(self):
        # locals: count=3 (scalar), data={"a": 1} (complex)
        out = extract(count=3, data={"a": 1})
        assert out == {"count": 3, "data": '{"a": 1}'}

    def test_ignored_names_are_skipped(self):
        # locals: visible=1, __hidden=2  ->  __hidden dropped by the ignore patterns
        assert extract(visible=1, __hidden=2) == {"visible": 1}

    def test_variable_whose_name_matches_the_mask_is_redacted_whole(self):
        # a local named "password" is redacted without inspecting its value at all
        assert extract(password="anything") == {"password": REDACTED}

    def test_scalars_come_first_then_complex_values_each_group_sorted(self):
        # scalars z, a  +  complex data, m  ->  a, z, then data, m
        out = extract(z=1, a=2, m=[1], data={"k": 1})
        assert list(out) == ["a", "z", "data", "m"]

    def test_patterns_are_compiled_once_per_capture(self, monkeypatch):
        # A multi-frame in-app stack must compile the mask/ignore patterns ONCE for the
        # whole capture, not once per frame (the regression this refactor fixes).
        from posthog import exception_utils

        # Each frame carries a local so it has something to serialize.
        def deepest(note):
            raise ValueError(note)

        def middle(note):
            deepest(note)

        def outer(note):
            middle(note)

        try:
            outer("boom")
        except ValueError:
            exc_info = sys.exc_info()

        tb_frames = list(iter_stacks(exc_info[2]))
        assert len(tb_frames) >= 3  # several in-app frames to process
        frames = [{"in_app": True} for _ in tb_frames]
        all_exceptions = [{"stacktrace": {"frames": frames}}]

        compile_calls = []
        real_compile = exception_utils._compile_patterns
        monkeypatch.setattr(
            exception_utils,
            "_compile_patterns",
            lambda patterns: compile_calls.append(patterns) or real_compile(patterns),
        )

        attach_code_variables_to_frames(
            all_exceptions,
            exc_info,
            list(DEFAULT_CODE_VARIABLES_MASK_PATTERNS),
            list(DEFAULT_CODE_VARIABLES_IGNORE_PATTERNS),
        )

        # exactly two compiles (mask + ignore) regardless of how many frames there are
        assert len(compile_calls) == 2
        # and multiple frames really were processed
        assert sum("code_variables" in frame for frame in frames) >= 2


# --- 10. end to end ------------------------------------------------------------------


class TestEndToEnd:
    """The whole code-variables pipeline through a real subprocess: an uncaught
    exception is autocaptured and its serialized payload (with code_variables) is
    printed in debug mode."""

    def test_code_variables_are_captured_and_masked(self, tmpdir):
        # the failing frame's locals: one safe dict, two secrets, one ignored dunder
        #   greeting="hello world"  count=42  data={"name": "test", ...}
        #   password=<secret-by-name>   note=<secret-by-value>   __hidden=<ignored>
        output = run_app(
            tmpdir,
            """
            make_client(capture_exception_code_variables=True)

            def trigger_error():
                greeting = "hello world"
                count = 42
                data = {"name": "test", "value": 123}
                password = "secret123"           # name matches -> redacted
                note = "contains_password_here"  # value matches -> redacted
                __hidden = "ignored"             # dunder -> skipped
                1 / 0

            trigger_error()
            """,
        )
        assert "ZeroDivisionError" in output
        assert "'code_variables':" in output  # the key form, not the temp-path word
        # scalars show up in the debug log in their repr form
        assert "'greeting': 'hello world'" in output
        assert "'count': 42" in output
        # a dict variable is double-encoded as a JSON string (locks the wire format)
        assert '"data": "{\\"name\\": \\"test\\", \\"value\\": 123}"' in output
        # both secrets are redacted, and the ignored name never appears
        assert "'password': '%s'" % REDACTED in output
        assert "'note': '%s'" % REDACTED in output
        assert "'__hidden'" not in output

    def test_code_variables_are_not_captured_when_disabled(self, tmpdir):
        output = run_app(
            tmpdir,
            """
            make_client(capture_exception_code_variables=False)

            def trigger_error():
                value = "hello world"
                1 / 0

            trigger_error()
            """,
        )
        assert "ZeroDivisionError" in output
        # check the key forms (repr + JSON), not the bare word - the temp path that
        # gets serialized into the payload happens to contain "code_variables" too.
        assert "'code_variables':" not in output
        assert '"code_variables":' not in output

    def test_a_context_can_enable_and_customize_masking(self, tmpdir):
        # client has capture OFF; the context turns it ON with a custom "bank" pattern
        output = run_app(
            tmpdir,
            """
            client = make_client(capture_exception_code_variables=False)

            def process_data():
                bank = "should_be_masked"  # matched by the custom context pattern
                account = "visible"
                1 / 0

            with posthog.new_context(client=client):
                posthog.set_capture_exception_code_variables_context(True)
                posthog.set_code_variables_mask_patterns_context([r"(?i).*bank.*"])
                posthog.set_code_variables_ignore_patterns_context([])
                process_data()
            """,
        )
        assert "code_variables" in output
        assert "'bank': '%s'" % REDACTED in output
        assert "'account': 'visible'" in output

    def test_object_secret_is_never_emitted(self, tmpdir):
        # args = (PostgresConfig(host=..., password=<env secret>), Inputs(schema=...))
        # The secret is read from the environment so it is never a source literal.
        output = run_app(
            tmpdir,
            """
            from dataclasses import dataclass

            @dataclass
            class PostgresConfig:
                host: str
                password: str

            @dataclass
            class Inputs:
                schema_name: str

            make_client(capture_exception_code_variables=True)

            def trigger_error():
                secret = os.environ["TEST_DB_PASSWORD"]
                args = (
                    PostgresConfig(host="db.example.com", password=secret),
                    Inputs(schema_name="traffic_stats"),
                )
                1 / 0

            trigger_error()
            """,
            env={"TEST_DB_PASSWORD": "uHjH9WJuEV0VT2NKoP7zpQ"},
        )
        assert "code_variables" in output
        assert "uHjH9WJuEV0VT2NKoP7zpQ" not in output  # the secret never leaks
        assert REDACTED in output
        assert "PostgresConfig" in output  # surrounding context is kept
        assert "traffic_stats" in output

    def test_url_credential_masking_can_be_disabled(self, tmpdir):
        # db_uri = "postgresql://user:<env secret>@host/db" with a neutral name and no
        # masked keyword - so only the URL heuristic could scrub it, and it is off here.
        output = run_app(
            tmpdir,
            """
            make_client(
                capture_exception_code_variables=True,
                code_variables_mask_url_credentials=False,
            )

            def trigger_error():
                db_uri = "postgresql://user:" + os.environ["TEST_DB_PASSWORD"] + "@host/db"
                1 / 0

            trigger_error()
            """,
            env={"TEST_DB_PASSWORD": "p4ssRUNTIME"},
        )
        assert "code_variables" in output
        assert "p4ssRUNTIME" in output  # URL masking disabled -> credential retained


# --- entropy-based secret detection (last resort) ------------------------------------


# Synthetic, format-correct fakes (no real credentials). Vendor keys are assembled from
# prefix + body so no complete secret literal lives in source (which trips secret scanners).
def _key(prefix, body):
    return prefix + body


KNOWN_FORMAT_SECRETS = [
    _key("sk-proj-", "T3BlbkFJabcd1234efgh5678ijkl9012mnop3456qrst7890wxyz"),  # OpenAI
    _key(
        "sk-", "Hf8sJd72hsKbNd83jdH5sQp2T3BlbkFJabcdEFGH1234ijklMNOPqrst"
    ),  # OpenAI legacy
    _key(
        "sk-ant-", "api03-aBcDeFgHiJkLmNoPqRsTuVwX0123456789-AbCdEf_gHiJkLmQQ"
    ),  # Anthropic
    _key("AKIA", "IOSFODNN7EXAMPLE"),  # AWS access key id (AWS's own doc example)
    _key("sk_live_", "4eC39HqLyjWDarjtT1zdp7dc"),  # Stripe secret key
    _key("pk_live_", "TYooMQauvdEDq54NiTphI7jx"),  # Stripe publishable key
    _key("ghp_", "16C7e42F292c6912E7710c838347Ae178B4a"),  # GitHub PAT
    _key(
        "github_pat_", "11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789abcdef"
    ),  # GitHub
    _key("glpat-", "aB1cD2eF3gH4iJ5kL6mN"),  # GitLab PAT
    _key(
        "xoxb-", "1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
    ),  # Slack bot token
    _key("AIza", "SyD-1a2B3c4D5e6F7g8H9i0JkLmNoPqRsTuVw"),  # Google API key
    _key(
        "eyJ", "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    ),  # JWT
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234",  # PEM private key
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkt",  # OpenSSH private key
]

HIGH_ENTROPY_SECRETS = [
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # AWS secret key (no prefix, base64)
    "xK9#mP2$vL5nQ8w!",  # strong password with symbols
    "P@ssw0rd!2024#Secure$Key",  # strong password
    "xK9mP2vL5nQ8wRtZ",  # strong password, no symbols
    "n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH",  # random mixed-case+digit token
    "dGhpc2lzYVNlY3JldFRva2VuMTIzNA==",  # base64 blob
]

NON_SECRETS = [
    # structured high-entropy ids/hashes/encodings -- must never be flagged
    "550e8400-e29b-41d4-a716-446655440000",  # UUID v4 (lowercase)
    "F47AC10B-58CC-4372-A567-0E02B2C3D479",  # UUID (uppercase)
    "507f1f77bcf86cd799439011",  # Mongo ObjectId
    "e83c5163316f89bfbde7d9ab23ca2e25604af290",  # git SHA-1
    "d41d8cd98f00b204e9800998ecf8427e",  # md5 hex digest
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",  # sha256 hex
    "a7F2c9E1b4D8a3C6e0F5b2D9c7A1e4F8",  # pure mixed-case hex (treated as an id)
    # object reprs / structured strings -- common in code variables, high-entropy but
    # must stay readable (regression: a real prod event flagged the first one)
    "CheckActivityInput(proxy_record_id=UUID('019df333-e9e2-0000-fa8e-ba3dd4217c09'))",
    "<posthog.temporal.common._ActivityInterceptor object at 0xffff77a7a850>",
    "ExecuteActivityInput(fn=check_status,args=[Input(id=42,name=widget)])",
    # ordinary code values -- must stay readable for debugging
    "user_authentication_handler",  # snake_case identifier
    "getUserByIdAndOrganization",  # camelCase identifier
    "getUserById2024",  # camelCase with digits
    "ApplicationConfigurationManager",  # PascalCase class name
    "PENDING_APPROVAL",  # SCREAMING_CASE enum
    "created-at-descending",  # dashed slug
    "application/json",  # mime type
    "alice.smith@example.com",  # email
    "the quick brown fox jumps over",  # prose (has spaces)
    "/usr/local/lib/python3.13/site-packages/posthog/client.py",  # unix path
    "C:\\Users\\admin\\app\\config.yaml",  # windows path
    "https://api.example.com/v2/users/12345/orders",  # url
    "1234567890123456",  # long number
    "3.141592653589793",  # float
    "2026-06-23T11:11:00.000Z",  # ISO timestamp
    "v1.2.3-beta.4",  # version string
    "active",  # short word
]


class TestSecretDetection:
    """The entropy-based last-resort detector: catches vendor keys and strong
    random secrets, while leaving ids/hashes/identifiers/paths readable."""

    @pytest.mark.parametrize("value", KNOWN_FORMAT_SECRETS)
    def test_known_vendor_formats_are_detected(self, value):
        assert _looks_like_secret(value) is True

    @pytest.mark.parametrize("value", HIGH_ENTROPY_SECRETS)
    def test_high_entropy_secrets_are_detected(self, value):
        assert _looks_like_secret(value) is True

    @pytest.mark.parametrize("value", NON_SECRETS)
    def test_non_secrets_are_not_flagged(self, value):
        assert _looks_like_secret(value) is False

    def test_uuids_and_object_ids_are_never_flagged(self):
        assert _looks_like_secret("550e8400-e29b-41d4-a716-446655440000") is False
        assert _looks_like_secret("507f1f77bcf86cd799439011") is False

    def test_short_strings_bail_cheaply(self):
        assert _looks_like_secret("xK9#mP2$") is False
        assert _is_high_entropy_secret("xK9#mP2$") is False

    def test_pure_hex_is_treated_as_an_id_not_a_secret(self):
        assert _is_high_entropy_secret("d41d8cd98f00b204e9800998ecf8427e") is False

    # -- integration with the masking pipeline --------------------------------------

    def test_high_entropy_value_in_a_neutral_variable_is_redacted(self):
        result = extract(api_response="n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH")
        assert result == {"api_response": REDACTED}

    def test_secret_inside_a_nested_structure_is_redacted(self):
        out = mask(
            {"entries": [_key("sk_live_", "4eC39HqLyjWDarjtT1zdp7dc"), "plain-value"]}
        )
        assert out == {"entries": [REDACTED, "plain-value"]}

    def test_object_id_value_survives(self):
        result = extract(order_id="507f1f77bcf86cd799439011")
        assert result == {"order_id": "507f1f77bcf86cd799439011"}

    def test_detection_can_be_disabled(self):
        config = _MaskingConfig.build(
            list(DEFAULT_CODE_VARIABLES_MASK_PATTERNS), [], detect_secrets=False
        )
        secret = "n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH"
        assert _mask_value(secret, config) == secret

    def test_detection_is_independent_of_url_scrubbing(self):
        config = _MaskingConfig.build(
            [], [], mask_url_credentials=False, detect_secrets=True
        )
        assert _mask_value("n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH", config) == REDACTED

    def test_end_to_end_neutral_named_secret_is_redacted(self, tmpdir):
        # neutral local, no keyword, no known prefix -> only the entropy gate can catch it
        output = run_app(
            tmpdir,
            """
            make_client(capture_exception_code_variables=True)

            def trigger_error():
                handle = os.environ["TEST_TOKEN"]
                1 / 0

            trigger_error()
            """,
            env={"TEST_TOKEN": "n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH"},
        )
        assert "code_variables" in output
        assert "n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH" not in output
        assert REDACTED in output

    def test_end_to_end_detection_disabled_via_client_option(self, tmpdir):
        # detection off on the client -> value captured verbatim (full public plumbing)
        output = run_app(
            tmpdir,
            """
            make_client(
                capture_exception_code_variables=True,
                code_variables_detect_secrets=False,
            )

            def trigger_error():
                handle = os.environ["TEST_TOKEN"]
                1 / 0

            trigger_error()
            """,
            env={"TEST_TOKEN": "n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH"},
        )
        assert "code_variables" in output
        assert "n8fK2pQ9vX7mL4wR8tY3uZ6bC1dE5gH" in output  # detection off -> retained
