"""
Redis-based distributed cache for PostHog feature flag definitions.

This example demonstrates how to implement a FlagDefinitionCacheProvider
using Redis for multi-instance deployments (leader election pattern).

Usage:
    import redis
    from posthog import Posthog

    redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
    cache = RedisFlagCache(redis_client, service_key="my-service")

    posthog = Posthog(
        "<project_api_key>",
        personal_api_key="<personal_api_key>",
        flag_definition_cache_provider=cache,
    )

Requirements:
    pip install redis
"""

import json
import uuid

from posthog import FlagDefinitionCacheData, FlagDefinitionCacheProvider
from redis import Redis
from typing import Optional


class RedisFlagCache(FlagDefinitionCacheProvider):
    """
    A distributed cache for PostHog feature flag definitions using Redis.

    In a multi-instance deployment (e.g., multiple serverless functions or containers),
    we want only ONE instance to poll PostHog for flag updates, while all instances
    share the cached results. This prevents N instances from making N redundant API calls.

    The implementation uses leader election:
    - One instance "wins" and becomes responsible for fetching
    - Other instances read from the shared cache
    - If the leader dies, the lock expires (TTL) and another instance takes over

    Uses Lua scripts for atomic operations, following Redis distributed lock best practices:
    https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/
    """

    LOCK_TTL_MS = 60 * 1000  # 60 seconds, should be longer than the flags poll interval
    CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours

    # Lua script: acquire lock if free, or extend if we own it
    _LUA_TRY_LEAD = """
        local current = redis.call('GET', KEYS[1])
        if current == false then
            redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
            return 1
        elseif current == ARGV[1] then
            redis.call('PEXPIRE', KEYS[1], ARGV[2])
            return 1
        end
        return 0
    """

    # Lua script: release lock only if we own it
    _LUA_STOP_LEAD = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
    """

    def __init__(self, redis: Redis, service_key: str):
        """
        Initialize the Redis flag cache.

        Args:
            redis: A redis-py client instance. Must be configured with
                   decode_responses=True for correct string handling.
            service_key: A unique identifier for this service/environment.
                         Used to scope Redis keys, allowing multiple services
                         or environments to share the same Redis instance.
                         Examples: "my-api-prod", "checkout-service", "staging".

        Redis Keys Created:
            - posthog:flags:{service_key} - Cached flag definitions (JSON)
            - posthog:flags:{service_key}:lock - Leader election lock

        Example:
            redis_client = redis.Redis(
                host='localhost',
                port=6379,
                decode_responses=True
            )
            cache = RedisFlagCache(redis_client, service_key="my-api-prod")
        """
        self._redis = redis
        self._cache_key = f"posthog:flags:{service_key}"
        self._lock_key = f"posthog:flags:{service_key}:lock"
        self._instance_id = str(uuid.uuid4())
        self._try_lead = self._redis.register_script(self._LUA_TRY_LEAD)
        self._stop_lead = self._redis.register_script(self._LUA_STOP_LEAD)

    def get_flag_definitions(self) -> Optional[FlagDefinitionCacheData]:
        """
        Retrieve cached flag definitions from Redis.

        Returns:
            Cached flag definitions if available, None otherwise.
        """
        cached = self._redis.get(self._cache_key)
        return json.loads(cached) if cached else None

    def should_fetch_flag_definitions(self) -> bool:
        """
        Determines if this instance should fetch flag definitions from PostHog.

        Atomically either:
        - Acquires the lock if no one holds it, OR
        - Extends the lock TTL if we already hold it

        Returns:
            True if this instance is the leader and should fetch, False otherwise.
        """
        result = self._try_lead(
            keys=[self._lock_key],
            args=[self._instance_id, self.LOCK_TTL_MS],
        )
        return result == 1

    def on_flag_definitions_received(self, data: FlagDefinitionCacheData) -> None:
        """
        Store fetched flag definitions in Redis.

        Args:
            data: The flag definitions to cache.
        """
        self._redis.set(self._cache_key, json.dumps(data), ex=self.CACHE_TTL_SECONDS)

    def shutdown(self) -> None:
        """
        Release leadership if we hold it. Safe to call even if not the leader.
        """
        self._stop_lead(keys=[self._lock_key], args=[self._instance_id])
