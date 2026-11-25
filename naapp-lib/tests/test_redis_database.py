"""aweb_naapp_testing.redis_database: a test's Redis client removes exactly the
keys it wrote, from the Redis database other local runs share."""

from __future__ import annotations

import os
from uuid import uuid4

from aweb_naapp_testing.redis_database import KeyRecordingRedis
from redis.asyncio import Redis


async def _exists(*keys: str) -> list[bool]:
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
        return [bool(await redis.exists(key)) for key in keys]


async def test_a_client_forgets_every_key_it_wrote_and_nothing_else() -> None:
    run = uuid4().hex
    mine = [f"test:{run}:set", f"test:{run}:pipeline", f"test:{run}:script", f"test:{run}:hash"]
    generation = f"awid:cache_generation:id_status:test:{run}"
    others = f"test:{run}:another-run"
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as another_run:
        await another_run.set(others, "kept", ex=60)
    async with KeyRecordingRedis.from_url(os.environ["REDIS_URL"], decode_responses=True) as redis:
        await redis.set(mine[0], "1", ex=60)
        async with redis.pipeline(transaction=False) as pipe:
            pipe.set(mine[1], "1", ex=60)
            await pipe.execute()
        await redis.eval("return redis.call('SET', KEYS[1], '1', 'EX', 60)", 1, mine[2])
        await redis.hset(mine[3], "field", "1")
        await redis.set(generation, "shared", ex=60)
        await redis.get(others)
        assert await _exists(*mine) == [True] * len(mine)

    # Leaving the block removed what the client wrote.
    assert await _exists(*mine) == [False] * len(mine)
    # Another run's key, even one this client read, stays; so does the shared
    # cache generation, which other runs read while this one ends.
    assert await _exists(others, generation) == [True, True]
    async with Redis.from_url(os.environ["REDIS_URL"], decode_responses=True) as cleanup:
        await cleanup.delete(others, generation)
