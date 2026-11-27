"""The production AWID fixture starts only from an environment prepared before the
tests run (make production-awid-sync); it never builds one itself."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import asyncpg
import pytest
from aweb_naapp_testing import production_registry
from aweb_naapp_testing.production_registry import (
    POOL_APPLICATION_NAME,
    running_production_registry,
)

PINNED = Path(production_registry.__file__).resolve().parents[4] / (
    "scripts/test-egress/production-awid"
)


async def test_an_unprepared_production_awid_is_refused_without_syncing_it(tmp_path, monkeypatch):
    project = tmp_path / "production-awid"
    project.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(PINNED / name, project / name)
    monkeypatch.setenv("PRODUCTION_AWID_PROJECT", str(project))

    with pytest.raises(RuntimeError, match="make production-awid-sync"):
        async with running_production_registry("postgresql://unused.invalid/none"):
            pass

    assert not (project / ".venv").exists()


async def named_backends(database_url: str) -> int:
    """The connections the fixture's pool holds on this database, read by name."""
    admin = await asyncpg.connect(database_url)
    try:
        return await admin.fetchval(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND application_name = $1",
            POOL_APPLICATION_NAME,
        )
    finally:
        await admin.close()


async def test_the_fixture_pool_holds_one_to_five_named_connections(test_db_factory):
    registry_db = await test_db_factory.create_db(suffix="naapp_production_awid_pool")
    database_url = registry_db.config.get_dsn()
    async with running_production_registry(database_url) as registry:
        assert await named_backends(database_url) == 1
        busy = [asyncio.create_task(registry.db.execute("SELECT pg_sleep(0.5)")) for _ in range(12)]
        try:
            await asyncio.sleep(0.25)
            assert await named_backends(database_url) == 5
        finally:
            await asyncio.gather(*busy)
