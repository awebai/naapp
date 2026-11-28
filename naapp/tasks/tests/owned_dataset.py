"""The live dataset as production runs it: in its own database, built by the
app's migrations as a non-superuser owner role, and checked as that owner.

No superuser session stays in the database: the board move's session check
refuses one. The test's superuser acts through one short connection per
statement. Roles are cluster-wide, so every role a test creates is dropped
when it ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from pgdbm import AsyncDatabaseManager, AsyncMigrationManager, DatabaseConfig

from aweb_tasks.app import MIGRATIONS_PATH
from aweb_tasks.board_move import move


@dataclass(frozen=True)
class Superuser:
    """The test's superuser, connected to the dataset's database only while it runs a statement."""

    dsn: str
    schema: str

    async def connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(self.dsn, server_settings={"search_path": self.schema})

    async def execute(self, sql: str, *args: Any) -> None:
        connection = await self.connect()
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    async def fetch_value(self, sql: str, *args: Any) -> Any:
        connection = await self.connect()
        try:
            return await connection.fetchval(sql, *args)
        finally:
            await connection.close()


@dataclass
class Dataset:
    """The live dataset in its own database, owned by a non-superuser owner role."""

    admin: Superuser
    database: str
    schema: str
    owner: str
    owner_config: DatabaseConfig
    role: Callable[..., Awaitable[str]]
    login: Callable[[], Awaitable[tuple[str, str]]]

    @property
    def owner_dsn(self) -> str:
        return self.owner_config.get_dsn()

    @property
    def target(self) -> move.Target:
        return move.Target(dsn=self.owner_dsn, live_schema=self.schema)

    async def owner_connection(self) -> asyncpg.Connection:
        return await asyncpg.connect(self.owner_dsn, server_settings={"search_path": self.schema})

    async def as_owner(self, sql: str) -> None:
        connection = await self.owner_connection()
        try:
            await connection.execute(sql)
        finally:
            await connection.close()

    async def refusals(self) -> set[move.Refusal]:
        """The predicates' rows, run as the owner inside a read-only transaction."""
        connection = await self.owner_connection()
        try:
            async with connection.transaction(readonly=True):
                rows = await connection.fetch(move.WRITER_AUTHORITY_SQL.read_text(encoding="utf-8"))
        finally:
            await connection.close()
        return {move.Refusal(**dict(row)) for row in rows}

    async def codes(self) -> set[str]:
        return {refusal.code for refusal in await self.refusals()}


def dsn(config: Any, role: str, password: str, database: str) -> str:
    return f"postgresql://{role}:{password}@{config.host}:{config.port}/{database}"


@pytest.fixture
async def dataset(test_db_factory: Any) -> AsyncIterator[Dataset]:
    factory_db = await test_db_factory.create_db(suffix="writer_check")
    database = await factory_db.fetch_value("SELECT current_database()")
    schema = factory_db.schema
    admin = Superuser(dsn=factory_db.config.get_dsn(), schema=schema)
    config = factory_db.config
    # The factory's pool would be a superuser session in the database.
    await factory_db.disconnect()
    created: list[str] = []

    async def role(options: str = "LOGIN") -> str:
        name = f"writer_check_{uuid4().hex[:12]}"
        await admin.execute(f"CREATE ROLE {name} {options}")
        created.append(name)
        return name

    async def login() -> tuple[str, str]:
        """A principal that can connect and resolve the dataset's schema: its name and DSN."""
        name, secret = await role(), uuid4().hex
        await admin.execute(f"ALTER ROLE {name} PASSWORD '{secret}'")
        await admin.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {name}')
        await admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {name}')
        return name, dsn(config, name, secret, database)

    try:
        owner, password = await role(), uuid4().hex
        await admin.execute(f"ALTER ROLE {owner} PASSWORD '{password}'")
        # pgdbm's connect runs CREATE SCHEMA IF NOT EXISTS, which needs CREATE.
        await admin.execute(f'GRANT CONNECT, CREATE ON DATABASE "{database}" TO {owner}')
        await admin.execute(f'ALTER SCHEMA "{schema}" OWNER TO {owner}')
        owner_config = DatabaseConfig(
            host=config.host, port=config.port, database=database, user=owner, password=password,
            schema=schema, min_connections=1, max_connections=1,
        )
        migrator = AsyncDatabaseManager(owner_config)
        await migrator.connect()
        try:
            await AsyncMigrationManager(
                migrator, migrations_path=str(MIGRATIONS_PATH), module_name="tasks"
            ).apply_pending_migrations()
        finally:
            await migrator.disconnect()
        yield Dataset(admin=admin, database=database, schema=schema, owner=owner,
                      owner_config=owner_config, role=role, login=login)
    finally:
        for name in reversed(created):
            # A test may drop a role itself, to leave a session whose role is gone.
            if await admin.fetch_value("SELECT 1 FROM pg_roles WHERE rolname = $1", name):
                await admin.execute(f"DROP OWNED BY {name} CASCADE")
                await admin.execute(f"DROP ROLE {name}")
        # The factory disconnects its pool again before it drops the database.
        await factory_db.connect()
