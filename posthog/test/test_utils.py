import sys
import time
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID

import six
from dateutil.tz import tzutc
from parameterized import parameterized
from pydantic import BaseModel
from pydantic.v1 import BaseModel as BaseModelV1

from posthog import utils
from posthog.types import FeatureFlagResult

TEST_API_KEY = "kOOlRy2QlMY9jHZQv0bKz0FZyazBUoY8Arj0lFVNjs4"
FAKE_TEST_API_KEY = "random_key"


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
            dt = datetime.now(tz=tzutc())  # timezone-aware datetime

        assert utils.is_naive(dt) is expected_naive

    def test_timezone_utils(self):
        now = datetime.now()
        utcnow = datetime.now(tz=tzutc())

        fixed = utils.guess_timezone(now)
        assert utils.is_naive(fixed) is False

        shouldnt_be_edited = utils.guess_timezone(utcnow)
        assert utcnow == shouldnt_be_edited

    def test_clean(self):
        simple = {
            "decimal": Decimal("0.142857"),
            "unicode": six.u("woo"),
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
            "registration": datetime.now(tz=tzutc()),
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
        assert utils.clean({"test": Dummy()}) == {"test": None}

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

        # Should miss with new version
        result = self.cache.get_cached_flag(distinct_id, flag_key, new_version)
        assert result is None

        # Invalidate old version
        self.cache.invalidate_version(old_version)

        # Should miss even with old version after invalidation
        result = self.cache.get_cached_flag(distinct_id, flag_key, old_version)
        assert result is None

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
