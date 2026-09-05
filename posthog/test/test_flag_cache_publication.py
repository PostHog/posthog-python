"""Result cache I/O must not block definition publication or local snapshots."""

import threading
from unittest import mock

import pytest

from posthog.client import Client
from posthog.request import APIError, GetResponse
from posthog.test.test_property_matching_version import definitions, evaluate
from posthog.test.test_utils import FAKE_TEST_API_KEY, FakeRedis
from posthog.utils import FlagCache, FlagCacheEntry, RedisFlagCache


@pytest.fixture(params=["memory", "redis"])
def client(request):
    client = Client(
        FAKE_TEST_API_KEY,
        secret_key="test-secret",
        send=False,
        enable_local_evaluation=False,
    )
    client.flag_cache = (
        FlagCache() if request.param == "memory" else RedisFlagCache(FakeRedis())
    )
    client._update_flag_state(definitions(1))
    yield client
    client.shutdown()


def test_bulk_and_refresh_do_not_wait_for_result_write(client):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []
    cache = client.flag_cache
    if isinstance(cache, RedisFlagCache):
        target, attribute, original = cache.redis, "setex", cache.redis.setex
    else:
        from posthog import utils

        target, attribute, original = utils, "FlagCacheEntry", FlagCacheEntry

    def pause_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def write_old_result():
        try:
            assert evaluate(client) is True
        except BaseException as error:
            errors.append(error)

    def refresh_and_evaluate():
        try:
            # Neither bulk evaluation nor version-only publication may wait on
            # the optional result cache, even while its write lock is held.
            result = client.evaluate_flags(
                "other-user",
                person_properties={"value": "banana"},
                only_evaluate_locally=True,
            )
            assert result.get_flag("person") is True
            with mock.patch(
                "posthog.client.get",
                return_value=GetResponse(
                    data=definitions(2), etag="v2", not_modified=False
                ),
            ):
                client._load_feature_flags()
            result = client.evaluate_flags(
                "other-user",
                person_properties={"value": "banana"},
                only_evaluate_locally=True,
            )
            assert result.get_flag("person") is False
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    writer = threading.Thread(target=write_old_result)
    reader = threading.Thread(target=refresh_and_evaluate)
    with mock.patch.object(target, attribute, side_effect=pause_write):
        try:
            writer.start()
            assert entered.wait(2)
            reader.start()
            assert finished.wait(1), "local snapshot/refresh blocked by cache write"
        finally:
            release.set()
            writer.join(5)
            if reader.ident is not None:
                reader.join(5)
    assert not writer.is_alive() and not reader.is_alive()
    assert not errors
    # The old write completed AFTER refresh. Neither ordinary cache lookup nor
    # API-outage fallback may expose it, even when requesting its old version.
    assert cache.get_cached_flag("user", "person", 0) is None
    assert cache.get_stale_cached_flag("user", "person") is None
    with mock.patch.object(
        client,
        "_get_feature_flag_details_from_server",
        side_effect=APIError(503, "offline"),
    ):
        assert (
            client.get_feature_flag_result(
                "person", "user", send_feature_flag_events=False
            )
            is None
        )
    assert evaluate(client) is False
    with mock.patch.object(
        client,
        "_get_feature_flag_details_from_server",
        side_effect=APIError(503, "offline"),
    ):
        result = client.get_feature_flag_result(
            "person", "user", send_feature_flag_events=False
        )
    assert result is not None and result.get_value() is False


def test_new_generation_write_finishes_after_paused_old_write(client):
    cache = client.flag_cache
    entered = threading.Event()
    release = threading.Event()
    new_started = threading.Event()
    errors = []
    if isinstance(cache, RedisFlagCache):
        target, attribute, original = cache.redis, "setex", cache.redis.setex
    else:
        from posthog import utils

        target, attribute, original = utils, "FlagCacheEntry", FlagCacheEntry

    def pause_first_write(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    def write(value, version):
        try:
            if value == "new":
                new_started.set()
            cache.set_cached_flag("user", "person", value, version)
        except BaseException as error:
            errors.append(error)

    old = threading.Thread(target=write, args=("old", 0))
    new = threading.Thread(target=write, args=("new", 1))
    with mock.patch.object(target, attribute, side_effect=pause_first_write):
        try:
            old.start()
            assert entered.wait(2)
            cache._advance_generation(1)
            new.start()
            assert new_started.wait(2)
        finally:
            release.set()
            old.join(5)
            if new.ident is not None:
                new.join(5)
    assert not old.is_alive() and not new.is_alive()
    assert not errors
    assert cache.get_cached_flag("user", "person", 1) == "new"
    assert cache.get_stale_cached_flag("user", "person") == "new"


def test_queued_old_write_cannot_replace_new_generation(client):
    cache = client.flag_cache
    old_version = client.flag_definition_version
    client._update_flag_state(definitions(2), client.feature_flags_by_key)
    assert evaluate(client) is False
    cache.set_cached_flag("user", "person", "old result", old_version)
    assert cache.get_stale_cached_flag("user", "person").get_value() is False


@pytest.mark.parametrize("status", [401, 402])
def test_reset_does_not_use_external_cache_cleanup(client, status):
    assert evaluate(client) is True
    with (
        mock.patch.object(
            client.flag_cache, "clear", side_effect=AssertionError("cache I/O")
        ),
        mock.patch.object(
            client.flag_cache,
            "invalidate_version",
            side_effect=AssertionError("cache I/O"),
        ),
        mock.patch("posthog.client.get", side_effect=APIError(status, "reset")),
    ):
        client._load_feature_flags()
    assert not client.feature_flags
    assert client.flag_cache.get_stale_cached_flag("user", "person") is None
    client._update_flag_state(definitions(1))
    assert evaluate(client) is True


def test_memory_generation_churn_prunes_removed_flags():
    cache = FlagCache()
    for version in range(100):
        cache._advance_generation(version)
        cache.set_cached_flag("reused-user", f"flag-{version}", True, version)
    assert list(cache.cache["reused-user"]) == ["flag-99"]


def test_standalone_cache_can_reuse_invalidated_version(client):
    cache = client.flag_cache
    cache.set_cached_flag("user", "flag", True, 1)
    cache.invalidate_version(1)
    assert cache.get_stale_cached_flag("user", "flag") is None
    cache.set_cached_flag("user", "flag", False, 1)
    assert cache.get_stale_cached_flag("user", "flag") is False
    cache.clear()
    cache.set_cached_flag("user", "flag", True, 0)
    assert cache.get_stale_cached_flag("user", "flag") is True


def test_fork_replaces_held_cache_write_lock_and_preserves_fence(client):
    cache = client.flag_cache
    client._update_flag_state(definitions(2), client.feature_flags_by_key)
    lock = cache._write_lock
    lock.acquire()
    try:
        with mock.patch.object(
            client, "_initialize_flag_cache", return_value=RedisFlagCache(FakeRedis())
        ):
            client._reinit_after_fork()
        assert client.flag_cache._write_lock is not lock
        assert client.flag_cache._minimum_version == client.flag_definition_version
        assert client.flag_cache._write_lock.acquire(blocking=False)
        client.flag_cache._write_lock.release()
    finally:
        lock.release()
    assert evaluate(client) is False
