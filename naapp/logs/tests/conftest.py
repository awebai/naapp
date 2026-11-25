"""Real PostgreSQL databases for the logs tests (pgdbm's test_db_factory), each
in its own schema."""

from typing import Any

import pytest
import pytest_asyncio
from aweb_naapp_testing.databases import SchemaDatabases

pytest_plugins = (
    "pgdbm.fixtures.conftest",
    "aweb_naapp_testing.server",
    "aweb_naapp_testing.database_fixtures",
)


@pytest.fixture
async def test_db_factory(test_db_factory: Any) -> SchemaDatabases:
    return SchemaDatabases(test_db_factory)


@pytest.fixture
async def purges(test_db_factory: SchemaDatabases, db: Any) -> Any:
    """The purge ledger beside the test module's ``db``."""
    from .fixtures import purge_ledger

    return await purge_ledger(test_db_factory, db)


@pytest.fixture
async def purge_ledger_database(test_db_factory: SchemaDatabases) -> Any:
    """An empty database of its own for a Logs deployment's purge ledger."""
    return await test_db_factory.create_db(suffix="logs_purge_ledger")


@pytest_asyncio.fixture(scope="session")
async def reader_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from .reader_world import migrate_reader_world

    async with database_template(migrate_reader_world, suffix="logs_reader_template") as template:
        yield template


@pytest_asyncio.fixture(scope="session")
async def reader_purge_template():
    from aweb_naapp_testing.database_fixtures import database_template
    from pgdbm import AsyncMigrationManager

    from aweb_logs.purge_ledger import MIGRATIONS_MODULE, MIGRATIONS_PATH

    async def migrate(db):
        await AsyncMigrationManager(
            db, migrations_path=str(MIGRATIONS_PATH), module_name=MIGRATIONS_MODULE
        ).apply_pending_migrations()

    async with database_template(migrate, suffix="logs_purge_template") as template:
        yield template
