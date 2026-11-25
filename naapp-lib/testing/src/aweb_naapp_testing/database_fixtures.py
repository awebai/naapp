"""Pytest fixtures for cloning a migrated schema into test-owned databases."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest_asyncio
from pgdbm import AsyncDatabaseManager, AsyncMigrationManager, SchemaManager
from pgdbm.testing import AsyncTestDatabase, DatabaseTestConfig

from aweb_naapp_testing.databases import TEST_SCHEMA


@asynccontextmanager
async def database_template(
    migrate: Callable[[AsyncDatabaseManager], Awaitable[None]],
    *,
    suffix: str,
    schema: str = TEST_SCHEMA,
) -> AsyncIterator[DatabaseTestConfig]:
    config = DatabaseTestConfig.from_env()
    template = AsyncTestDatabase(config)
    name = await template.create_test_database(suffix)
    try:
        async with template.get_test_db_manager(schema=schema) as db:
            # PostgreSQL cannot copy a database with any open connections.
            await SchemaManager(db).ensure_schema_exists()
            await migrate(db)
        yield config.model_copy(update={"test_db_template": name})
    finally:
        await template.drop_test_database()


@pytest_asyncio.fixture
async def clone_database():
    async with AsyncExitStack() as databases:

        async def clone(
            config: DatabaseTestConfig, *, suffix: str, schema: str = TEST_SCHEMA
        ) -> AsyncDatabaseManager:
            return await databases.enter_async_context(
                AsyncTestDatabase.create(schema=schema, config=config, suffix=suffix)
            )

        yield clone


@pytest_asyncio.fixture(scope="session")
async def awid_template() -> AsyncIterator[DatabaseTestConfig]:
    from awid_service.testing import running_registry

    async def migrate(db: AsyncDatabaseManager) -> None:
        async with running_registry(db.config.get_dsn()):
            pass

    async with database_template(migrate, suffix="awid_template") as template:
        yield template


async def migrate_passkeys(db: AsyncDatabaseManager) -> None:
    from aweb_naapp.runtime.passkeys.service import MIGRATION_MODULE, MIGRATIONS

    await AsyncMigrationManager(
        db, migrations_path=str(MIGRATIONS), module_name=MIGRATION_MODULE
    ).apply_pending_migrations()


async def migrate_credential_audit(db: AsyncDatabaseManager, module_name: str) -> None:
    from aweb_naapp.runtime.credential_audit import recorder

    await AsyncMigrationManager(
        db,
        migrations_path=str(Path(recorder.__file__).with_name("migrations")),
        module_name=module_name,
    ).apply_pending_migrations()
