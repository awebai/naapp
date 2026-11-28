"""The board move's shape and write-authority predicates (writer_authority.sql),
run as the dataset's owner role against the schema the app's migrations build.

Each test gets its own database. Roles are cluster-wide, so every role a test
creates is dropped when it ends. The tests assert refusal codes, never wording.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from pgdbm import AsyncDatabaseManager, AsyncMigrationManager, DatabaseConfig

from aweb_tasks.app import MIGRATIONS_PATH
from aweb_tasks.board_move import move

DATASET = ("installations", "tasks", "claims", "locks", "comments", "dependencies", "events")


@dataclass(frozen=True)
class Refusal:
    code: str
    principal: str | None
    role: str | None
    object: str | None


@dataclass
class Dataset:
    """The live dataset in its own database, owned by a non-superuser owner role."""

    admin: AsyncDatabaseManager
    database: str
    schema: str
    owner: str
    owner_dsn: str
    role: Callable[..., Awaitable[str]]

    async def as_owner(self, sql: str) -> None:
        connection = await asyncpg.connect(self.owner_dsn, server_settings={"search_path": self.schema})
        try:
            await connection.execute(sql)
        finally:
            await connection.close()

    async def refusals(self) -> set[Refusal]:
        """The predicates' rows, run as the owner inside a read-only transaction."""
        connection = await asyncpg.connect(self.owner_dsn, server_settings={"search_path": self.schema})
        try:
            async with connection.transaction(readonly=True):
                rows = await connection.fetch(move.WRITER_AUTHORITY_SQL.read_text(encoding="utf-8"))
        finally:
            await connection.close()
        return {Refusal(**dict(row)) for row in rows}

    async def codes(self) -> set[str]:
        return {refusal.code for refusal in await self.refusals()}


def _dsn(config: Any, role: str, password: str, database: str) -> str:
    return f"postgresql://{role}:{password}@{config.host}:{config.port}/{database}"


@pytest.fixture
async def dataset(test_db_factory: Any) -> AsyncIterator[Dataset]:
    admin = await test_db_factory.create_db(suffix="writer_authority")
    database = await admin.fetch_value("SELECT current_database()")
    schema = admin.schema
    created: list[str] = []

    async def role(options: str = "LOGIN") -> str:
        name = f"writer_check_{uuid4().hex[:12]}"
        await admin.execute(f"CREATE ROLE {name} {options}")
        created.append(name)
        return name

    owner, password = await role(), uuid4().hex
    await admin.execute(f"ALTER ROLE {owner} PASSWORD '{password}'")
    # pgdbm's connect runs CREATE SCHEMA IF NOT EXISTS, which needs CREATE.
    await admin.execute(f'GRANT CONNECT, CREATE ON DATABASE "{database}" TO {owner}')
    await admin.execute(f'ALTER SCHEMA "{schema}" OWNER TO {owner}')
    owner_dsn = _dsn(admin.config, owner, password, database)
    migrator = AsyncDatabaseManager(DatabaseConfig(
        connection_string=owner_dsn, schema=schema, min_connections=1, max_connections=1,
    ))
    await migrator.connect()
    try:
        await AsyncMigrationManager(
            migrator, migrations_path=str(MIGRATIONS_PATH), module_name="tasks"
        ).apply_pending_migrations()
    finally:
        await migrator.disconnect()
    try:
        yield Dataset(admin=admin, database=database, schema=schema, owner=owner,
                      owner_dsn=owner_dsn, role=role)
    finally:
        for name in reversed(created):
            await admin.execute(f"DROP OWNED BY {name} CASCADE")
            await admin.execute(f"DROP ROLE {name}")


# --- what must pass -----------------------------------------------------------


async def test_the_migrated_dataset_passes_with_roles_that_reach_no_write_authority(
    dataset: Dataset,
) -> None:
    # The platform's kinds of login: one with no memberships, one NOINHERIT
    # with none, and a monitoring member of pg_monitor.
    await dataset.role("LOGIN")
    await dataset.role("LOGIN NOINHERIT")
    monitor = await dataset.role("LOGIN")
    await dataset.admin.execute(f"GRANT pg_monitor TO {monitor}")
    # pg_write_all_data exists but is assigned to nobody.
    assert not await dataset.admin.fetch_value(
        "SELECT count(*) FROM pg_auth_members WHERE roleid = 'pg_write_all_data'::regrole"
    )

    assert await dataset.refusals() == set()


async def test_a_reader_and_a_nologin_writer_pass(dataset: Dataset) -> None:
    reader = await dataset.role("LOGIN")
    await dataset.admin.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{dataset.schema}" TO {reader}')
    # A NOLOGIN role is not a principal: only a principal that reaches it counts.
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT INSERT ON "{dataset.schema}".tasks TO {writer}')

    assert await dataset.refusals() == set()


async def test_the_predicates_are_one_read_only_statement(dataset: Dataset) -> None:
    connection = await asyncpg.connect(dataset.owner_dsn, server_settings={"search_path": dataset.schema})
    try:
        async with connection.transaction(readonly=True):
            # A prepared statement holds exactly one statement.
            statement = await connection.prepare(move.WRITER_AUTHORITY_SQL.read_text(encoding="utf-8"))
            assert [column.name for column in statement.get_attributes()] == [
                "code", "principal", "role", "object",
            ]
    finally:
        await connection.close()


async def test_the_preflight_runs_as_the_readme_says(dataset: Dataset) -> None:
    writer = await dataset.role("LOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".comments TO {writer}')
    sql = move.WRITER_AUTHORITY_SQL

    def preflight() -> subprocess.CompletedProcess[str]:
        # The README's command, with a test DSN in place of the service name.
        return subprocess.run(
            ["psql", dataset.owner_dsn, "-X", "-At", "-v", "ON_ERROR_STOP=1",
             "-c", "BEGIN READ ONLY", "-f", str(sql), "-c", "ROLLBACK"],
            env={**os.environ, "PGOPTIONS": f"-c search_path={dataset.schema}"},
            capture_output=True, text=True, check=False,
        )

    refused = await asyncio.to_thread(preflight)
    assert (refused.returncode, refused.stderr) == (0, "")
    assert refused.stdout.splitlines() == [
        "BEGIN", f"write_authority_table_privilege|{writer}|{writer}|comments", "ROLLBACK",
    ]

    await dataset.admin.execute(f"DROP OWNED BY {writer}")
    passed = await asyncio.to_thread(preflight)
    assert (passed.returncode, passed.stderr, passed.stdout) == (0, "", "BEGIN\nROLLBACK\n")


# --- S: the shape -------------------------------------------------------------


async def test_a_missing_dataset_table_refuses(dataset: Dataset) -> None:
    await dataset.as_owner("DROP TABLE dependencies")

    assert await dataset.refusals() == {Refusal("shape_missing_table", None, None, "dependencies")}


async def test_a_partitioned_dataset_table_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "DROP TABLE dependencies;"
        " CREATE TABLE dependencies (installation_id TEXT NOT NULL) PARTITION BY LIST (installation_id)"
    )

    assert await dataset.refusals() == {Refusal("shape_not_ordinary_table", None, None, "dependencies")}


async def test_a_dataset_table_with_an_inheritance_child_refuses(dataset: Dataset) -> None:
    # A child's rows are read and written through its parent.
    await dataset.as_owner("CREATE TABLE outside_comments () INHERITS (comments)")

    assert await dataset.refusals() == {Refusal("shape_inheritance", None, None, "comments")}


async def test_a_dataset_table_that_inherits_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE TABLE outside_parent (); ALTER TABLE locks INHERIT outside_parent"
    )

    assert await dataset.refusals() == {Refusal("shape_inheritance", None, None, "locks")}


@pytest.mark.parametrize("action", ["ON DELETE CASCADE", "ON DELETE SET NULL", "ON UPDATE CASCADE"])
async def test_an_outside_foreign_key_that_writes_into_the_dataset_refuses(
    dataset: Dataset, action: str,
) -> None:
    # The referential action writes the dataset table as its owner, on nobody's grant.
    await dataset.as_owner(
        "CREATE TABLE outside_parents (id INTEGER PRIMARY KEY);"
        f" ALTER TABLE events ADD COLUMN outside_id INTEGER REFERENCES outside_parents (id) {action}"
    )

    assert await dataset.refusals() == {
        Refusal("shape_outside_referential_action", None, None, "events"),
    }


async def test_an_outside_foreign_key_without_an_action_passes(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE TABLE outside_parents (id INTEGER PRIMARY KEY);"
        " ALTER TABLE events ADD COLUMN outside_id INTEGER REFERENCES outside_parents (id) ON DELETE RESTRICT"
    )

    assert await dataset.refusals() == set()


async def test_a_dataset_table_owned_by_another_role_refuses(dataset: Dataset) -> None:
    other = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'ALTER TABLE "{dataset.schema}".claims OWNER TO {other}')

    assert await dataset.refusals() == {Refusal("shape_not_owned", None, None, "claims")}


# --- A3: write authority, per principal ---------------------------------------


async def test_a_noinherit_login_member_of_the_owner_refuses(dataset: Dataset) -> None:
    member = await dataset.role("LOGIN NOINHERIT")
    await dataset.admin.execute(f"GRANT {dataset.owner} TO {member}")

    # Reaching the owner also reaches its tables and their privileges.
    refusals = await dataset.refusals()
    assert Refusal("write_authority_owner", member, dataset.owner, None) in refusals
    assert {refusal.principal for refusal in refusals} == {member}


async def test_a_login_that_reaches_a_nologin_writer_refuses(dataset: Dataset) -> None:
    writer = await dataset.role("NOLOGIN")
    middle = await dataset.role("NOLOGIN NOINHERIT")
    login = await dataset.role("LOGIN")
    await dataset.admin.execute(f'GRANT INSERT ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(f"GRANT {writer} TO {middle}")
    await dataset.admin.execute(f"GRANT {middle} TO {login} WITH INHERIT FALSE, SET TRUE")

    assert await dataset.refusals() == {
        Refusal("write_authority_table_privilege", login, writer, "tasks"),
    }


async def test_a_login_that_reaches_pg_write_all_data_refuses(dataset: Dataset) -> None:
    login = await dataset.role("LOGIN NOINHERIT")
    await dataset.admin.execute(f"GRANT pg_write_all_data TO {login}")

    assert "write_authority_write_all_data" in await dataset.codes()
    assert {refusal.principal for refusal in await dataset.refusals()} == {login}


async def test_a_login_that_reaches_a_superuser_refuses(dataset: Dataset) -> None:
    superuser = await dataset.role("NOLOGIN SUPERUSER")
    login = await dataset.role("LOGIN NOINHERIT")
    await dataset.admin.execute(f"GRANT {superuser} TO {login}")

    assert "write_authority_superuser" in await dataset.codes()
    assert {refusal.principal for refusal in await dataset.refusals()} == {login}


async def test_a_column_only_update_grant_refuses(dataset: Dataset) -> None:
    login = await dataset.role("LOGIN")
    await dataset.admin.execute(f'GRANT UPDATE (title) ON "{dataset.schema}".tasks TO {login}')

    assert await dataset.refusals() == {
        Refusal("write_authority_column_privilege", login, login, "tasks"),
    }


async def test_a_login_that_reaches_a_table_owner_refuses(dataset: Dataset) -> None:
    other = await dataset.role("NOLOGIN")
    login = await dataset.role("LOGIN NOINHERIT")
    await dataset.admin.execute(f'ALTER TABLE "{dataset.schema}".claims OWNER TO {other}')
    await dataset.admin.execute(f"GRANT {other} TO {login}")

    codes = await dataset.codes()
    assert {"shape_not_owned", "write_authority_table_owner"} <= codes


async def test_a_subscription_owner_holding_a_write_grant_refuses(dataset: Dataset) -> None:
    # A NOLOGIN subscription owner: an apply worker runs as it.
    owner = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT INSERT ON "{dataset.schema}".events TO {owner}')
    await dataset.admin.execute(f'GRANT CREATE ON DATABASE "{dataset.database}" TO {owner}')
    await dataset.admin.execute(f"GRANT pg_create_subscription TO {owner}")
    subscription = f"writer_check_{uuid4().hex[:12]}"
    await dataset.admin.execute(
        f"CREATE SUBSCRIPTION {subscription} CONNECTION 'dbname=writer_check_absent'"
        f" PUBLICATION absent WITH (connect = false)"
    )
    try:
        await dataset.admin.execute(f"ALTER SUBSCRIPTION {subscription} OWNER TO {owner}")

        assert await dataset.refusals() == {
            Refusal("write_authority_table_privilege", owner, owner, "events"),
        }
    finally:
        await dataset.admin.execute(f"ALTER SUBSCRIPTION {subscription} SET (slot_name = NONE)")
        await dataset.admin.execute(f"DROP SUBSCRIPTION {subscription}")


# --- A3: indirection through objects the owner owns ---------------------------


async def test_a_view_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE TABLE outside (id INTEGER); CREATE VIEW outside_view AS SELECT id FROM outside"
    )

    assert await dataset.refusals() == {Refusal("indirection_view", None, None, "outside_view")}


async def test_a_security_definer_function_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE TABLE outside (id INTEGER);"
        " CREATE FUNCTION outside_write() RETURNS void LANGUAGE sql SECURITY DEFINER"
        " AS 'INSERT INTO outside VALUES (1)'"
    )

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, None, "outside_write()"),
    }


async def test_a_security_definer_procedure_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE PROCEDURE outside_call(id integer) LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
    )

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, None, "outside_call(integer)"),
    }


async def test_a_dml_rule_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    # The rule's action runs as its relation's owner.
    await dataset.as_owner(
        "CREATE TABLE outside (id INTEGER);"
        " CREATE RULE outside_into_tasks AS ON INSERT TO outside DO ALSO"
        " DELETE FROM tasks WHERE number = NEW.id"
    )

    assert await dataset.refusals() == {Refusal("indirection_rule", None, None, "outside_into_tasks")}


async def test_an_invoker_function_and_a_trigger_pass(dataset: Dataset) -> None:
    # Triggers fire as the caller; only a SECURITY DEFINER function would not.
    await dataset.as_owner(
        "CREATE FUNCTION stamp() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';"
        " CREATE TRIGGER stamp BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION stamp()"
    )

    assert await dataset.refusals() == set()
