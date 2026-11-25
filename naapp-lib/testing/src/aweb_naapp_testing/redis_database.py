"""The Redis database naapp tests use, and a client that removes what it wrote.

Local runs share one Redis database. A test client records every key a
command or script it sends may write, and removes exactly those keys, never
another run's, when its ``async with`` block ends or on
:meth:`KeyRecordingRedis.forget_written`.
"""

from __future__ import annotations

import os
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

# A shared domain's cache generation is read by every run while this one
# ends; it stays, and expires by itself.
SHARED_PREFIXES = ("awid:cache_generation:",)

# Commands that may write their first key.
_WRITES = frozenset(
    {
        "APPEND",
        "DECR",
        "DECRBY",
        "EXPIRE",
        "EXPIREAT",
        "GETDEL",
        "GETEX",
        "GETSET",
        "HDEL",
        "HINCRBY",
        "HINCRBYFLOAT",
        "HMSET",
        "HSET",
        "HSETNX",
        "INCR",
        "INCRBY",
        "INCRBYFLOAT",
        "LPUSH",
        "LREM",
        "LSET",
        "LTRIM",
        "PERSIST",
        "PEXPIRE",
        "PEXPIREAT",
        "PSETEX",
        "RPUSH",
        "SADD",
        "SET",
        "SETEX",
        "SETNX",
        "SPOP",
        "SREM",
        "XADD",
        "ZADD",
        "ZINCRBY",
        "ZREM",
        "ZREMRANGEBYSCORE",
    }
)
# Scripts may write any key they declare.
_SCRIPTS = frozenset({"EVAL", "EVALSHA", "EVAL_RO", "EVALSHA_RO", "FCALL"})


def redis_test_url() -> str:
    """``REDIS_URL``, which must name a test Redis database: tests never fall
    back to a default database that may hold something else."""
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("set REDIS_URL to a test Redis database")
    return url


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _written_keys(args: tuple[Any, ...]) -> list[str]:
    """The keys the command ``args`` may write."""
    if not args:
        return []
    name = _text(args[0]).upper()
    if name in _SCRIPTS:
        count = int(args[2])
        return [_text(key) for key in args[3 : 3 + count]]
    if name in ("MSET", "MSETNX"):
        return [_text(key) for key in args[1::2]]
    if name in _WRITES and len(args) > 1:
        return [_text(args[1])]
    return []


class KeyRecordingRedis(Redis):
    """A Redis client that records each key it may write, directly, in a
    pipeline or by script, so the test can remove exactly those keys."""

    @property
    def written(self) -> set[str]:
        recorded = self.__dict__.get("_written")
        if recorded is None:
            recorded = self.__dict__["_written"] = set()
        return recorded

    def record(self, args: tuple[Any, ...]) -> None:
        self.written.update(_written_keys(args))

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        self.record(args)
        return await super().execute_command(*args, **options)

    def pipeline(self, transaction: bool = True, shard_hint: str | None = None) -> Pipeline:
        return KeyRecordingPipeline(
            self, self.connection_pool, self.response_callbacks, transaction, shard_hint
        )

    async def __aexit__(self, *exc: Any) -> None:
        """Forget what this client wrote, then close it."""
        try:
            await self.forget_written()
        finally:
            await super().__aexit__(*exc)

    async def forget_written(self) -> None:
        """Delete every key this client wrote, except the shared ones."""
        keys = [key for key in self.written if not key.startswith(SHARED_PREFIXES)]
        if keys:
            await super().execute_command("DEL", *keys)
        self.written.clear()


class KeyRecordingPipeline(Pipeline):
    """A pipeline whose queued and immediate commands its client records."""

    def __init__(self, client: KeyRecordingRedis, *args: Any) -> None:
        super().__init__(*args)
        self.client = client

    def execute_command(self, *args: Any, **kwargs: Any) -> Any:
        self.client.record(args)
        return super().execute_command(*args, **kwargs)
