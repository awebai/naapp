"""aweb server/tests/conftest.py at 2788969b6fce509437eab53637927bb97cf18b58, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from asyncpg import Pool
from aweb.migrations import apply_native_migrations
from awid.db_config import build_database_config
from dataclasses import dataclass, field
from pgdbm import AsyncDatabaseManager, DatabaseConfig
import pytest_asyncio

# Copied from an aweb commit, not a release: replaced by the re-copy
# from the first server tag that carries this change.
AWEB_REF = '2788969b6fce509437eab53637927bb97cf18b58'
AWEB_PATH = 'server/tests/conftest.py'
AWEB_BLOB = 'a0f720a6b49063981a928c0d16106d39e397dd18'
COPIED = {
    'SharedTestDatabase': '5679856c34565a8f2906614123701c57553bb8e5c1d44c3d9a94fcd25d6f8c3f',
    'shared_test_database': '0a900baf9f3b593c39f7a769d4f43921d7b557576197c98235ff338b55553f20',
    'shared_test_pool': '313171e11d1e9b23ba2740299255643e64782ec0802ba9d8031bc49f5341d452',
    'DatabaseManagers': '11dca34711ddfb3d24ea6daa82f75067910a3d1b5b9311c453a0f2bcfb5d147a',
    'aweb_cloud_db': 'ce217d9f1e728b0fd26da32224a077f72215580488d105162c039edab48cbdad',
}


ENVIRONMENT_DEFAULTS = {'APP_ENV': 'testing', 'ENVIRONMENT': 'testing', 'AWEB_INTERNAL_AUTH_SECRET': 'test-internal-auth-secret', 'AWID_SERVICE_TOKEN': 'aweb-server-tests-service-token-32-bytes'}


@dataclass(frozen=True)
class SharedTestDatabase:
    """The selected disposable database configuration and its one owned pool."""

    config: DatabaseConfig = field(repr=False)
    pool: Pool = field(repr=False)

    @property
    def dsn(self) -> str:
        return self.config.get_dsn()


@pytest_asyncio.fixture
async def shared_test_database(test_db_factory):
    db_manager = await test_db_factory.create_db(suffix="aweb_server")
    # Pre-provision pgcrypto in public at the point the fresh database is
    # materialized (pgdbm creates test databases from template0, so no
    # template can carry it): migration 001's unqualified CREATE EXTENSION
    # then no-ops instead of resolving into the pinned pg_catalog and
    # colliding with the built-in gen_random_uuid.
    await db_manager.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
    config = build_database_config(
        connection_string=db_manager.config.get_dsn(),
        min_connections=2,
        max_connections=5,
    )
    pool = await AsyncDatabaseManager.create_shared_pool(config)
    try:
        yield SharedTestDatabase(config=db_manager.config, pool=pool)
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def shared_test_pool(shared_test_database):
    """Borrow the same pool; the selected database fixture owns its cleanup."""
    yield shared_test_database.pool


@dataclass(frozen=True)
class DatabaseManagers:
    aweb_db: AsyncDatabaseManager

    @property
    def oss_db(self) -> AsyncDatabaseManager:
        return self.aweb_db


@pytest_asyncio.fixture
async def aweb_cloud_db(shared_test_pool):
    """Database manager for the unified aweb schema tests."""

    temp_manager = AsyncDatabaseManager(pool=shared_test_pool, schema=None)
    await temp_manager.execute("CREATE SCHEMA IF NOT EXISTS aweb")

    aweb_db = AsyncDatabaseManager(pool=shared_test_pool, schema="aweb")

    await apply_native_migrations(aweb_db)

    yield DatabaseManagers(aweb_db)
