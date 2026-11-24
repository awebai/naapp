"""The Redis database naapp tests use."""

from __future__ import annotations

import os


def redis_test_url() -> str:
    """``REDIS_URL``, which must name a test Redis database: tests never fall
    back to a default database that may hold something else."""
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("set REDIS_URL to a test Redis database")
    return url
