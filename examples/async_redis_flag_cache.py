"""
Async Redis-based distributed cache for PostHog feature flag definitions.

This example demonstrates how to implement a FlagDefinitionCacheProvider with
redis.asyncio for async-first applications. The PostHog SDK accepts async cache
provider methods and runs their awaitables to completion before continuing.

Usage:
    import redis.asyncio as redis
    from posthog import Posthog

    # Use a Redis client dedicated to this cache provider. The SDK runs async
    # provider methods on its own background event loop.
    redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)
    cache = AsyncRedisFlagCache(redis_client, service_key="my-service")

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
from typing import Optional

from posthog import FlagDefinitionCacheData, FlagDefinitionCacheProvider
from redis.asyncio import Redis


class AsyncRedisFlagCache(FlagDefinitionCacheProvider):
    """
    A distributed cache for PostHog feature flag definitions using redis.asyncio.

    In a multi-instance deployment, only one instance should poll PostHog for
    flag updates while all instances share the cached results. This prevents N
    instances from making N redundant API calls.

    The implementation uses leader election:
    - One instance "wins" and becomes responsible for fetching
    - Other instances read from the shared cache
    - If the leader dies, the lock expires and another instance takes over

    Uses Lua scripts for atomic operations, following Redis distributed lock best
    practices: https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/
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
        Initialize the async Redis flag cache.

        Args:
            redis: A redis.asyncio client instance dedicated to this cache provider.
                   The SDK runs async provider methods on its own background event
                   loop, so avoid sharing the same asyncio Redis client with a
                   different application event loop. Configure decode_responses=True
                   for string responses, or bytes responses will be decoded here.
            service_key: A unique identifier for this service/environment.
                         Used to scope Redis keys, allowing multiple services
                         or environments to share the same Redis instance.
        """
        self._redis = redis
        self._cache_key = f"posthog:flags:{service_key}"
        self._lock_key = f"posthog:flags:{service_key}:lock"
        self._instance_id = str(uuid.uuid4())

    async def get_flag_definitions(self) -> Optional[FlagDefinitionCacheData]:
        """
        Retrieve cached flag definitions from Redis.

        Returns:
            Cached flag definitions if available, None otherwise.
        """
        cached = await self._redis.get(self._cache_key)
        if not cached:
            return None
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        return json.loads(cached)

    async def should_fetch_flag_definitions(self) -> bool:
        """
        Determines if this instance should fetch flag definitions from PostHog.

        Atomically either:
        - Acquires the lock if no one holds it, OR
        - Extends the lock TTL if we already hold it

        Returns:
            True if this instance is the leader and should fetch, False otherwise.
        """
        result = await self._redis.eval(
            self._LUA_TRY_LEAD,
            1,
            self._lock_key,
            self._instance_id,
            self.LOCK_TTL_MS,
        )
        return result == 1

    async def on_flag_definitions_received(self, data: FlagDefinitionCacheData) -> None:
        """
        Store fetched flag definitions in Redis.

        Args:
            data: The flag definitions to cache.
        """
        await self._redis.set(
            self._cache_key, json.dumps(data), ex=self.CACHE_TTL_SECONDS
        )

    async def shutdown(self) -> None:
        """
        Release leadership if we hold it. Safe to call even if not the leader.
        """
        await self._redis.eval(
            self._LUA_STOP_LEAD,
            1,
            self._lock_key,
            self._instance_id,
        )
