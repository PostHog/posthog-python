import pytest
from requests.exceptions import ConnectionError

from posthog.client import Client
from posthog.types import FeatureFlagResult
from posthog.utils import FlagCache


class _EvictDuringRead(dict):
    """Model a writer removing a key between a read's lookup operations."""

    def __contains__(self, key):
        present = super().__contains__(key)
        self.pop(key, None)
        return present

    def get(self, key, default=None):
        self.pop(key, None)
        return super().get(key, default)


@pytest.mark.parametrize("read_stale", [False, True])
@pytest.mark.parametrize("evict_user", [False, True])
def test_evicted_cache_entry_is_a_miss(read_stale, evict_user):
    cache = FlagCache()
    result = FeatureFlagResult.from_value_and_payload("flag", True, None)
    cache.set_cached_flag("user", "flag", result, 1)
    if evict_user:
        cache.cache = _EvictDuringRead(cache.cache)
    else:
        cache.cache["user"] = _EvictDuringRead(cache.cache["user"])

    if read_stale:
        actual = cache.get_stale_cached_flag("user", "flag")
    else:
        actual = cache.get_cached_flag("user", "flag", 1)

    assert actual is None
    assert not cache.cache if evict_user else not cache.cache["user"]


@pytest.mark.parametrize("evict_user", [False, True])
def test_public_flag_lookup_survives_eviction_during_outage(monkeypatch, evict_user):
    client = Client("test-key", send=False, enable_local_evaluation=False)
    cache = FlagCache()
    cache.set_cached_flag(
        "user", "flag", FeatureFlagResult.from_value_and_payload("flag", True, None), 1
    )
    if evict_user:
        cache.cache = _EvictDuringRead(cache.cache)
    else:
        cache.cache["user"] = _EvictDuringRead(cache.cache["user"])
    client.flag_cache = cache

    def unavailable(*args, **kwargs):
        raise ConnectionError("offline test")

    monkeypatch.setattr(client, "_get_feature_flag_details_from_server", unavailable)
    try:
        result = client.get_feature_flag_result(
            "flag", "user", send_feature_flag_events=False
        )
        assert result is None
    finally:
        client.shutdown()
