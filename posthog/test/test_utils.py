import json
import sys
import time
import unittest
from unittest import mock
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Optional
from uuid import UUID

from parameterized import parameterized
from pydantic import BaseModel
from pydantic.v1 import BaseModel as BaseModelV1

from posthog import utils
from posthog.types import FeatureFlagResult

TEST_API_KEY = "kOOlRy2QlMY9jHZQv0bKz0FZyazBUoY8Arj0lFVNjs4"
FAKE_TEST_API_KEY = "random_key"


class FakeRedis:
    def __init__(self, fail=False):
        self.store = {}
        self.fail = fail
        self.setex_calls = []
        self.scan_calls = []
        self._last_scan_keys = []

    def _key(self, key):
        return key.decode() if isinstance(key, bytes) else key

    def get(self, key):
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.store.get(self._key(key))

    def setex(self, key, ttl, value):
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.setex_calls.append((self._key(key), ttl, value))
        self.store[self._key(key)] = value

    def set(self, key, value):
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.store[self._key(key)] = value

    def scan(self, cursor, match=None, count=None):
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.scan_calls.append((cursor, match, count))
        prefix = match[:-1] if match and match.endswith("*") else match
        if cursor == 0:
            self._last_scan_keys = [
                key.encode()
                for key in sorted(self.store)
                if prefix is None or key.startswith(prefix)
            ]
        keys = self._last_scan_keys
        midpoint = max(1, len(keys) // 2)
        if cursor == 0 and len(keys) > 1:
            return 1, keys[:midpoint]
        return 0, keys[midpoint:] if cursor == 1 else keys

    def delete(self, *keys):
        if self.fail:
            raise RuntimeError("redis unavailable")
        for key in keys:
            self.store.pop(self._key(key), None)


class TestUtils(unittest.TestCase):
    @parameterized.expand(
        [
            ("naive datetime should be naive", True),
            ("timezone-aware datetime should not be naive", False),
        ]
    )
    def test_is_naive(self, _name: str, expected_naive: bool):
        if expected_naive:
            dt = datetime.now()  # naive datetime
        else:
            dt = datetime.now(tz=timezone.utc)  # timezone-aware datetime

        assert utils.is_naive(dt) is expected_naive

    def test_timezone_utils(self):
        now = datetime.now()
        utcnow = datetime.now(tz=timezone.utc)

        fixed = utils.guess_timezone(now)
        assert utils.is_naive(fixed) is False

        shouldnt_be_edited = utils.guess_timezone(utcnow)
        assert utcnow == shouldnt_be_edited

        old_naive = datetime(2000, 1, 1)
        fixed_old = utils.guess_timezone(old_naive)
        assert fixed_old == old_naive.replace(tzinfo=timezone.utc)

    def test_total_seconds(self):
        delta = timedelta(days=2, seconds=3, microseconds=4)
        assert utils.total_seconds(delta) == 172803.000004

    def test_is_naive_when_tzinfo_has_no_offset(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                if dt is None:
                    return timedelta(hours=1)
                return None

        assert utils.is_naive(datetime(2024, 1, 1, tzinfo=NoOffset())) is True

    def test_clean(self):
        simple = {
            "decimal": Decimal("0.142857"),
            "unicode": "woo",
            "date": datetime.now(),
            "long": 200000000,
            "integer": 1,
            "float": 2.0,
            "bool": True,
            "str": "woo",
            "none": None,
        }

        complicated = {
            "exception": Exception("This should show up"),
            "timedelta": timedelta(microseconds=20),
            "list": [1, 2, 3],
        }

        combined = dict(simple.items())
        combined.update(complicated.items())

        pre_clean_keys = combined.keys()

        utils.clean(combined)
        assert combined.keys() == pre_clean_keys

        # test UUID separately, as the UUID object doesn't equal its string representation according to Python
        assert (
            utils.clean(UUID("12345678123456781234567812345678"))
            == "12345678-1234-5678-1234-567812345678"
        )

    def test_clean_with_dates(self):
        dict_with_dates = {
            "birthdate": date(1980, 1, 1),
            "registration": datetime.now(tz=timezone.utc),
        }
        assert dict_with_dates == utils.clean(dict_with_dates)

    def test_bytes(self):
        item = bytes(10)
        utils.clean(item)
        assert utils.clean(item) == "\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"

    def test_clean_fn(self):
        cleaned = utils.clean({"fn": lambda x: x, "number": 4})
        assert cleaned == {"fn": None, "number": 4}

    @parameterized.expand(
        [
            ("http://posthog.io/", "http://posthog.io"),
            ("http://posthog.io", "http://posthog.io"),
            ("https://example.com/path/", "https://example.com/path"),
            ("https://example.com/path", "https://example.com/path"),
        ]
    )
    def test_remove_slash(self, input_url, expected_url):
        assert expected_url == utils.remove_trailing_slash(input_url)

    def test_clean_pydantic(self):
        class ModelV2(BaseModel):
            foo: str
            bar: int
            baz: Optional[str] = None

        class ModelV1(BaseModelV1):
            foo: int
            bar: str

        class NestedModel(BaseModel):
            foo: ModelV2

        class ModelDumpOnly:
            def model_dump(self):
                return {"foo": "model_dump"}

        assert utils.clean(ModelDumpOnly()) == {"foo": "model_dump"}
        assert utils.clean(ModelV2(foo="1", bar=2)) == {
            "foo": "1",
            "bar": 2,
            "baz": None,
        }
        # Pydantic V1 is not compatible with Python 3.14+
        if sys.version_info < (3, 14):
            assert utils.clean(ModelV1(foo=1, bar="2")) == {"foo": 1, "bar": "2"}
        assert utils.clean(NestedModel(foo=ModelV2(foo="1", bar=2, baz="3"))) == {
            "foo": {"foo": "1", "bar": 2, "baz": "3"}
        }

    def test_clean_pydantic_like_class(self) -> None:
        class Dummy:
            def model_dump(self, required_param: str) -> dict:
                return {}

        # previously python 2 code would cause an error while cleaning,
        # and this entire object would be None, and we would log an error
        # let's allow ourselves to clean `Dummy` as None,
        # without blatting the `test` key
        with mock.patch.object(utils.log, "debug") as debug:
            assert utils.clean({"test": Dummy()}) == {"test": None}
        debug.assert_called_once()
        assert debug.call_args.args[0].startswith(
            "Could not serialize Pydantic-like model:"
        )

    def test_clean_containers_and_invalid_dict_values(self):
        assert utils.clean(
            (Decimal("1.5"), UUID("12345678123456781234567812345678"))
        ) == [
            1.5,
            "12345678-1234-5678-1234-567812345678",
        ]

        bad_value = object()

        def clean_or_raise(value):
            if value is bad_value:
                raise TypeError("unsupported")
            return value

        with (
            mock.patch("posthog.utils.clean", side_effect=clean_or_raise),
            mock.patch.object(utils.log, "warning") as warning,
        ):
            assert utils._clean_dict({"ok": 1, "bad": bad_value}) == {"ok": 1}

        warning.assert_called_once()
        assert warning.call_args.args[0] == (
            'Dictionary values must be serializeable to JSON "%s" value %s of type %s is unsupported.'
        )
        assert warning.call_args.args[1:] == ("bad", bad_value, type(bad_value))

    def test_coerce_unicode(self):
        assert utils._coerce_unicode("already unicode") == "already unicode"
        assert utils._coerce_unicode(b"bytes") == "bytes"
        assert utils._coerce_unicode(123) is None

        with mock.patch.object(utils.log, "warning") as warning:
            assert utils._coerce_unicode(b"\xff") is None
        warning.assert_called_once()
        assert warning.call_args.args[0] == "Error decoding: %s"
        assert "invalid start byte" in warning.call_args.args[1]

        class UndecodableBytes(bytes):
            def decode(self, *args, **kwargs):
                raise Exception("left", "right")

        with mock.patch.object(utils.log, "warning") as warning:
            assert utils._coerce_unicode(UndecodableBytes(b"broken")) is None
        assert warning.call_args.args[1] == "left:right"

    def test_regex_datetime_and_case_helpers(self):
        assert utils.is_valid_regex("^posthog.*") is True
        assert utils.is_valid_regex("[") is False

        naive = datetime(2024, 1, 1)
        aware = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert utils.convert_to_datetime_aware(naive) == aware
        assert utils.convert_to_datetime_aware(aware) is aware

        assert utils.str_icontains("Hello World", "WORLD") is True
        assert utils.str_icontains("Hello World", "python") is False
        assert utils.str_iequals("Hello World", "hello world") is True
        assert utils.str_iequals("Hello World", "hello") is False

    def test_get_os_info_branches(self):
        with (
            mock.patch.object(utils.sys, "platform", "win32"),
            mock.patch.object(
                utils.platform, "win32_ver", return_value=("11", "", "", "")
            ),
        ):
            assert utils.get_os_info() == {"$os": "Windows", "$os_version": "11"}

        with (
            mock.patch.object(utils.sys, "platform", "win32"),
            mock.patch.object(
                utils.platform, "win32_ver", return_value=("", "", "", "")
            ),
        ):
            assert utils.get_os_info() == {"$os": "Windows", "$os_version": ""}

        with (
            mock.patch.object(utils.sys, "platform", "darwin"),
            mock.patch.object(
                utils.platform, "mac_ver", return_value=("14.4", ("", "", ""), "")
            ),
        ):
            assert utils.get_os_info() == {"$os": "Mac OS X", "$os_version": "14.4"}

        with (
            mock.patch.object(utils.sys, "platform", "linux"),
            mock.patch.object(utils.distro, "info", return_value={"version": "24.04"}),
            mock.patch.object(utils.distro, "name", return_value="Ubuntu"),
        ):
            assert utils.get_os_info() == {
                "$os": "Linux",
                "$os_version": "24.04",
                "$os_distro": "Ubuntu",
            }

        with (
            mock.patch.object(utils.sys, "platform", "freebsd13"),
            mock.patch.object(utils.platform, "release", return_value="13.2"),
        ):
            assert utils.get_os_info() == {"$os": "FreeBSD", "$os_version": "13.2"}

        with (
            mock.patch.object(utils.sys, "platform", "sunos"),
            mock.patch.object(utils.platform, "release", return_value="5.11"),
        ):
            assert utils.get_os_info() == {"$os": "sunos", "$os_version": "5.11"}

    def test_system_context(self):
        with (
            mock.patch.object(
                utils.platform, "python_implementation", return_value="CPython"
            ),
            mock.patch.object(
                utils, "get_os_info", return_value={"$os": "TestOS", "$os_version": "1"}
            ),
        ):
            context = utils.system_context()

        assert context == {
            "$python_runtime": "CPython",
            "$python_version": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
            "$os": "TestOS",
            "$os_version": "1",
        }

    def test_clean_dataclass(self):
        @dataclass
        class InnerDataClass:
            inner_foo: str
            inner_bar: int
            inner_uuid: UUID
            inner_date: datetime
            inner_optional: Optional[str] = None

        @dataclass
        class TestDataClass:
            foo: str
            bar: int
            nested: InnerDataClass

        assert utils.clean(
            TestDataClass(
                foo="1",
                bar=2,
                nested=InnerDataClass(
                    inner_foo="3",
                    inner_bar=4,
                    inner_uuid=UUID("12345678123456781234567812345678"),
                    inner_date=datetime(2025, 1, 1),
                ),
            )
        ) == {
            "foo": "1",
            "bar": 2,
            "nested": {
                "inner_foo": "3",
                "inner_bar": 4,
                "inner_uuid": "12345678-1234-5678-1234-567812345678",
                "inner_date": datetime(2025, 1, 1),
                "inner_optional": None,
            },
        }


class TestFlagCache(unittest.TestCase):
    def setUp(self):
        self.cache = utils.FlagCache(max_size=3, default_ttl=1)
        self.flag_result = FeatureFlagResult.from_value_and_payload(
            "test-flag", True, None
        )

    def test_default_cache_settings(self):
        cache = utils.FlagCache()
        assert cache.max_size == 10000
        assert cache.default_ttl == 300

    def test_cache_entry_validity(self):
        entry = utils.FlagCacheEntry(
            self.flag_result, flag_definition_version=1, timestamp=100
        )

        assert entry.is_valid(current_time=109, ttl=10, current_flag_version=1) is True
        assert entry.is_valid(current_time=110, ttl=10, current_flag_version=1) is False
        assert entry.is_valid(current_time=111, ttl=10, current_flag_version=1) is False
        assert entry.is_valid(current_time=109, ttl=10, current_flag_version=2) is False
        assert entry.is_stale_but_usable(current_time=109, max_stale_age=10) is True
        assert entry.is_stale_but_usable(current_time=110, max_stale_age=10) is False
        assert entry.is_stale_but_usable(current_time=3700) is False
        assert entry.is_stale_but_usable(current_time=3700.5) is False

    def test_cache_basic_operations(self):
        distinct_id = "user123"
        flag_key = "test-flag"
        flag_version = 1

        # Test cache miss
        result = self.cache.get_cached_flag(distinct_id, flag_key, flag_version)
        assert result is None

        # Test cache set and hit
        self.cache.set_cached_flag(
            distinct_id, flag_key, self.flag_result, flag_version
        )
        result = self.cache.get_cached_flag(distinct_id, flag_key, flag_version)
        assert result is not None
        assert result.get_value()

    def test_cache_ttl_expiration(self):
        distinct_id = "user123"
        flag_key = "test-flag"
        flag_version = 1

        # Set flag in cache
        self.cache.set_cached_flag(
            distinct_id, flag_key, self.flag_result, flag_version
        )

        # Should be available immediately
        result = self.cache.get_cached_flag(distinct_id, flag_key, flag_version)
        assert result is not None

        # Wait for TTL to expire (1 second + buffer)
        time.sleep(1.1)

        # Should be expired
        result = self.cache.get_cached_flag(distinct_id, flag_key, flag_version)
        assert result is None

    def test_cache_version_invalidation(self):
        distinct_id = "user123"
        flag_key = "test-flag"
        old_version = 1
        new_version = 2

        # Set flag with old version
        self.cache.set_cached_flag(distinct_id, flag_key, self.flag_result, old_version)

        # Should hit with old version
        result = self.cache.get_cached_flag(distinct_id, flag_key, old_version)
        assert result is not None
        assert self.cache.cache[distinct_id][flag_key].timestamp <= time.time()

        # Should miss with new version
        result = self.cache.get_cached_flag(distinct_id, flag_key, new_version)
        assert result is None

        # Invalidate old version
        self.cache.invalidate_version(old_version)

        # Should miss even with old version after invalidation
        result = self.cache.get_cached_flag(distinct_id, flag_key, old_version)
        assert result is None
        assert distinct_id not in self.cache.access_times

    def test_cache_version_invalidation_keeps_users_with_other_flags(self):
        self.cache.set_cached_flag("user123", "old-flag", self.flag_result, 1)
        self.cache.set_cached_flag("user123", "new-flag", self.flag_result, 2)

        self.cache.invalidate_version(1)

        assert "old-flag" not in self.cache.cache["user123"]
        assert "new-flag" in self.cache.cache["user123"]
        assert "user123" in self.cache.access_times

        old_empty_user = "old-empty-user"
        self.cache.set_cached_flag(old_empty_user, "old-flag", self.flag_result, 1)
        self.cache.invalidate_version(1)
        assert old_empty_user not in self.cache.cache
        assert old_empty_user not in self.cache.access_times

    def test_stale_cache_misses(self):
        assert self.cache.get_stale_cached_flag("missing-user", "test-flag") is None

        self.cache.cache["user123"] = {}
        assert self.cache.get_stale_cached_flag("user123", "missing-flag") is None

    def test_stale_cache_passes_current_time_and_max_age(self):
        class StrictEntry:
            flag_result = "stale-result"

            def is_stale_but_usable(self, current_time, max_stale_age=3600):
                assert current_time == 1234
                assert max_stale_age == 99
                return True

        self.cache.cache["user123"] = {"test-flag": StrictEntry()}
        with mock.patch.object(utils.time, "time", return_value=1234):
            assert (
                self.cache.get_stale_cached_flag("user123", "test-flag", 99)
                == "stale-result"
            )

    def test_stale_cache_functionality(self):
        distinct_id = "user123"
        flag_key = "test-flag"
        flag_version = 1

        # Set flag in cache
        self.cache.set_cached_flag(
            distinct_id, flag_key, self.flag_result, flag_version
        )

        # Wait for TTL to expire
        time.sleep(1.1)

        # Should not get fresh cache
        result = self.cache.get_cached_flag(distinct_id, flag_key, flag_version)
        assert result is None

        # Should get stale cache (within 1 hour default)
        stale_result = self.cache.get_stale_cached_flag(distinct_id, flag_key)
        assert stale_result is not None
        assert stale_result.get_value()

    def test_lru_eviction(self):
        # Cache has max_size=3, so adding 4 users should evict the LRU one
        flag_version = 1

        # Add 3 users
        for i in range(3):
            user_id = f"user{i}"
            self.cache.set_cached_flag(
                user_id, "test-flag", self.flag_result, flag_version
            )

        # Access user0 to make it recently used
        self.cache.get_cached_flag("user0", "test-flag", flag_version)

        # Add 4th user, should evict user1 (least recently used)
        self.cache.set_cached_flag("user3", "test-flag", self.flag_result, flag_version)

        # user0 should still be there (was recently accessed)
        result = self.cache.get_cached_flag("user0", "test-flag", flag_version)
        assert result is not None

        # user2 should still be there (was recently added)
        result = self.cache.get_cached_flag("user2", "test-flag", flag_version)
        assert result is not None

        # user3 should be there (just added)
        result = self.cache.get_cached_flag("user3", "test-flag", flag_version)
        assert result is not None

    def test_lru_eviction_removes_twenty_percent(self):
        cache = utils.FlagCache(max_size=10, default_ttl=60)
        for i in range(10):
            cache.set_cached_flag(f"user{i}", "test-flag", self.flag_result, 1)
            cache.access_times[f"user{i}"] = i

        cache.set_cached_flag("user10", "test-flag", self.flag_result, 1)

        assert "user0" not in cache.cache
        assert "user1" not in cache.cache
        assert "user0" not in cache.access_times
        assert "user1" not in cache.access_times
        assert len(cache.cache) == 9

    def test_empty_lru_eviction_and_clear(self):
        self.cache._evict_lru()
        assert self.cache.cache == {}
        assert self.cache.access_times == {}

        self.cache.set_cached_flag("user123", "test-flag", self.flag_result, 1)
        self.cache.clear()
        assert self.cache.cache == {}
        assert self.cache.access_times == {}


class TestRedisFlagCache(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.cache = utils.RedisFlagCache(
            self.redis, default_ttl=10, stale_ttl=60, key_prefix="test:flags:"
        )

    def test_default_cache_settings(self):
        default_cache = utils.RedisFlagCache(self.redis)
        assert default_cache.default_ttl == 300
        assert default_cache.stale_ttl == 3600
        assert default_cache.key_prefix == "posthog:flags:"
        assert default_cache.version_key == "posthog:flags:version"

    def test_cache_key_and_serialization(self):
        assert self.cache._get_cache_key("user123", "beta") == "test:flags:user123:beta"

        generated_timestamp = json.loads(self.cache._serialize_entry(True, 3))[
            "timestamp"
        ]
        assert isinstance(generated_timestamp, float)

        serialized = self.cache._serialize_entry(
            {"enabled": True, "count": Decimal("1.5")}, 3, timestamp=123
        )
        assert json.loads(serialized) == {
            "flag_result": {"enabled": True, "count": 1.5},
            "flag_version": 3,
            "timestamp": 123,
        }

        entry = self.cache._deserialize_entry(serialized)
        assert entry.flag_result == {"enabled": True, "count": 1.5}
        assert entry.flag_definition_version == 3
        assert entry.timestamp == 123
        assert self.cache._deserialize_entry("not json") is None
        assert self.cache._deserialize_entry(json.dumps({"flag_result": True})) is None

    def test_get_set_and_stale_cached_flags(self):
        self.cache.set_cached_flag("user123", "beta", True, 7)

        assert self.cache.get_cached_flag("user123", "beta", 7) is True
        assert self.cache.get_cached_flag("user123", "beta", 8) is None
        assert self.redis.store["test:flags:version"] == 7
        assert self.redis.setex_calls[0][1] == 60

        stale_key = self.cache._get_cache_key("user123", "old-beta")
        self.redis.store[stale_key] = self.cache._serialize_entry(
            True, 7, timestamp=time.time() - 20
        )
        assert self.cache.get_cached_flag("user123", "old-beta", 7) is None
        assert (
            self.cache.get_stale_cached_flag("user123", "old-beta", max_stale_age=30)
            is True
        )
        assert (
            self.cache.get_stale_cached_flag("user123", "old-beta", max_stale_age=5)
            is None
        )

        default_stale_key = self.cache._get_cache_key("user123", "default-stale")
        self.redis.store[default_stale_key] = self.cache._serialize_entry(
            True, 7, timestamp=time.time() - 30
        )
        assert self.cache.get_stale_cached_flag("user123", "default-stale") is True

        boundary_key = self.cache._get_cache_key("user123", "boundary-stale")
        self.redis.store[boundary_key] = self.cache._serialize_entry(
            True, 7, timestamp=time.time() - 3600.5
        )
        assert self.cache.get_stale_cached_flag("user123", "boundary-stale") is None

    def test_redis_errors_fall_back_to_miss(self):
        failing_cache = utils.RedisFlagCache(FakeRedis(fail=True))

        assert failing_cache.get_cached_flag("user123", "beta", 1) is None
        assert failing_cache.get_stale_cached_flag("user123", "beta") is None
        failing_cache.set_cached_flag("user123", "beta", True, 1)
        failing_cache.invalidate_version(1)
        failing_cache.clear()

    def test_invalidate_version(self):
        old_key = self.cache._get_cache_key("user123", "old")
        new_key = self.cache._get_cache_key("user123", "new")
        invalid_key = self.cache._get_cache_key("user123", "invalid")
        self.redis.store[old_key] = self.cache._serialize_entry(True, 1, timestamp=100)
        self.redis.store[new_key] = self.cache._serialize_entry(True, 2, timestamp=100)
        self.redis.store[invalid_key] = "not json"
        self.redis.store[self.cache.version_key] = 2

        self.cache.invalidate_version(1)

        assert self.redis.scan_calls == [
            (0, "test:flags:*", 100),
            (1, "test:flags:*", 100),
        ]
        assert old_key not in self.redis.store
        assert invalid_key not in self.redis.store
        assert new_key in self.redis.store
        assert self.cache.version_key in self.redis.store

    def test_invalidate_version_continues_after_version_key_in_scan_batch(self):
        self.redis.store[self.cache.version_key] = 2
        old_key = self.cache._get_cache_key("zzz-user", "old-beta")
        newer_key = self.cache._get_cache_key("zzzz-user", "new-beta")
        self.redis.store[old_key] = self.cache._serialize_entry(False, 1)
        self.redis.store[newer_key] = self.cache._serialize_entry(True, 2)
        self.redis.store[self.cache._get_cache_key("zzzzz-user", "newer-beta")] = (
            self.cache._serialize_entry(True, 2)
        )

        self.cache.invalidate_version(1)

        assert old_key not in self.redis.store
        assert newer_key in self.redis.store

    def test_clear(self):
        self.redis.store[self.cache._get_cache_key("user123", "beta")] = "value"
        self.redis.store[self.cache.version_key] = 1
        self.redis.store["other:key"] = "value"

        self.cache.clear()

        assert self.redis.scan_calls == [
            (0, "test:flags:*", 100),
            (1, "test:flags:*", 100),
        ]
        assert self.redis.store == {"other:key": "value"}
