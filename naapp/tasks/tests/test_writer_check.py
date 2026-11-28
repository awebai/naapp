"""The board move's writer check as the rollback compare runs it: in the listing's
transaction, as the dataset's owner, the dataset's EXCLUSIVE locks first, then
S and A3 (writer_authority.sql), then P and C3 (sessions.sql).

Each test gets its own database with no superuser session left in it. The
tests assert refusal codes, never wording.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from aweb_tasks.board_move import move
from aweb_tasks.board_move.move import Refusal

from .owned_dataset import Dataset

INSTALLATION = "writer-check"


async def check(dataset: Dataset, *platform_roles: str) -> set[Refusal]:
    """The check's refusals; none when the listing is returned."""
    try:
        listed = await move.live_listing(dataset.target, INSTALLATION, platform_roles=platform_roles)
    except move.CheckRefused as refused:
        return set(refused.refusals)
    assert listed == b""
    return set()


async def pid(connection: asyncpg.Connection) -> str:
    return str(await connection.fetchval("SELECT pg_backend_pid()"))


async def until(condition: Callable[[], Awaitable[Any]], what: str, seconds: float = 10) -> Any:
    """Waits for a condition the test caused to become visible; fails naming it."""
    for _ in range(int(seconds * 20)):
        if value := await condition():
            return value
        await asyncio.sleep(0.05)
    pytest.fail(f"{what} did not happen within {seconds} s")


async def superuser_name(dataset: Dataset) -> str:
    return str(await dataset.admin.fetch_value("SELECT current_user"))


# --- what must pass -----------------------------------------------------------


async def test_the_check_lists_the_dataset_with_an_idle_owner_session_beside_it(dataset: Dataset) -> None:
    # The owner's idle sessions, such as the service's pool, hold no transaction.
    idle = await dataset.owner_connection()
    try:
        assert await check(dataset) == set()
    finally:
        await idle.close()


async def test_a_named_platform_role_passes_and_the_same_role_unnamed_refuses(dataset: Dataset) -> None:
    monitor, dsn = await dataset.login()
    await dataset.admin.execute(f"GRANT pg_monitor TO {monitor}")
    connected = await asyncpg.connect(dsn)
    try:
        assert await check(dataset) == {Refusal("session_connected", None, monitor, await pid(connected))}
        assert await check(dataset, monitor) == set()
    finally:
        await connected.close()


async def test_an_autovacuum_worker_row_is_seen_by_the_owner_and_passes(dataset: Dataset) -> None:
    # A slowed vacuum of a table full of dead rows keeps one worker in the database.
    await dataset.as_owner(
        "CREATE TABLE churn (id int, pad text) WITH (autovacuum_vacuum_threshold = 0,"
        " autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_cost_delay = 100,"
        " autovacuum_vacuum_cost_limit = 1, autovacuum_analyze_threshold = 1000000000);"
        " INSERT INTO churn SELECT g, repeat('x', 200) FROM generate_series(1, 100000) g;"
        " DELETE FROM churn"
    )
    owner = await dataset.owner_connection()
    try:
        # The owner reads only that the row is in its database and carries no role.
        worker = await until(lambda: owner.fetchrow(
            "SELECT pid, usesysid, backend_type FROM pg_stat_activity"
            " WHERE datid = (SELECT oid FROM pg_database WHERE datname = current_database())"
            " AND usesysid IS NULL"
        ), "an autovacuum worker in the database", seconds=180)
        assert worker["backend_type"] is None
        assert await dataset.admin.fetch_value(
            "SELECT backend_type FROM pg_stat_activity WHERE pid = $1", worker["pid"]
        ) == "autovacuum worker"

        assert await check(dataset) == set()
    finally:
        await owner.close()
        await dataset.as_owner("DROP TABLE churn")


async def test_the_check_runs_as_the_readme_says(dataset: Dataset) -> None:
    monitor, dsn = await dataset.login()
    await dataset.admin.execute(f"GRANT pg_monitor TO {monitor}")
    files = (move.LOCK_SQL, move.WRITER_AUTHORITY_SQL, move.SESSIONS_SQL)

    def checked(platform_roles: str) -> subprocess.CompletedProcess[str]:
        # The README's command, with a test DSN in place of the service name.
        options = f"-c search_path={dataset.schema}"
        if platform_roles:
            options += f" -c board_move.platform_roles={platform_roles}"
        return subprocess.run(
            ["psql", dataset.owner_dsn, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", "BEGIN",
             *(argument for sql in files for argument in ("-f", str(sql))), "-c", "ROLLBACK"],
            env={**os.environ, "PGOPTIONS": options}, capture_output=True, text=True, check=False,
        )

    connected = await asyncpg.connect(dsn)
    try:
        refused = await asyncio.to_thread(checked, "")
        assert (refused.returncode, refused.stderr) == (0, "")
        assert refused.stdout.splitlines() == [
            "BEGIN", "SET", "DO", f"session_connected||{monitor}|{await pid(connected)}", "ROLLBACK",
        ]

        passed = await asyncio.to_thread(checked, monitor)
        assert (passed.returncode, passed.stderr, passed.stdout) == (0, "", "BEGIN\nSET\nDO\nROLLBACK\n")
    finally:
        await connected.close()


@pytest.mark.parametrize("sql", [move.LOCK_SQL, move.SESSIONS_SQL], ids=lambda path: path.name)
def test_the_lock_and_its_check_name_every_dataset_table(sql: Any) -> None:
    named = sql.read_text(encoding="utf-8")
    assert all(f"'{table}'" in named for table in move.TABLES)


# --- C3, the sessions ---------------------------------------------------------


@pytest.mark.parametrize("state", ["idle in transaction", "active"])
async def test_an_owner_session_inside_a_transaction_refuses(dataset: Dataset, state: str) -> None:
    other = await dataset.owner_connection()
    canceller = await dataset.owner_connection()
    running: asyncio.Task[Any] | None = None
    try:
        other_pid = await pid(other)
        if state == "idle in transaction":
            pending = other.transaction()
            await pending.start()
            await other.fetchval("SELECT 1")
        else:
            running = asyncio.create_task(other.fetchval("SELECT pg_sleep(60)"))
        await until(lambda: canceller.fetchval(
            "SELECT 1 FROM pg_stat_activity WHERE pid = $1 AND state = $2", int(other_pid), state
        ), f"the owner session {state}")

        assert await check(dataset) == {
            Refusal("session_owner_in_transaction", None, dataset.owner, other_pid)
        }
    finally:
        if running is not None:
            await canceller.execute(f"SELECT pg_cancel_backend({other_pid})")
            with pytest.raises(asyncpg.QueryCanceledError):
                await running
        await canceller.close()
        await other.close()


async def test_a_superuser_session_refuses_whenever_it_is_connected(dataset: Dataset) -> None:
    # Idle, with no transaction, and hidden from the owner but for its role.
    connected = await dataset.admin.connect()
    try:
        assert await check(dataset) == {
            Refusal("session_superuser", None, await superuser_name(dataset), await pid(connected))
        }
    finally:
        await connected.close()


async def test_any_other_principals_session_refuses_while_it_is_connected(dataset: Dataset) -> None:
    name, dsn = await dataset.login()
    connected = await asyncpg.connect(dsn)
    try:
        assert await check(dataset) == {Refusal("session_connected", None, name, await pid(connected))}
    finally:
        await connected.close()


async def test_a_session_that_kept_the_owner_role_after_losing_the_membership_refuses(
    dataset: Dataset,
) -> None:
    name, dsn = await dataset.login()
    await dataset.admin.execute(f"GRANT {dataset.owner} TO {name}")
    kept = await asyncpg.connect(dsn, server_settings={"search_path": dataset.schema})
    try:
        await kept.execute(f"SET ROLE {dataset.owner}")
        await dataset.admin.execute(f"REVOKE {dataset.owner} FROM {name}")
        # The catalogue no longer shows the membership, and the session still writes as the owner.
        assert not await dataset.admin.fetch_value(
            "SELECT 1 FROM pg_auth_members WHERE member = $1::regrole", name
        )
        assert await kept.fetchval("SELECT current_user") == dataset.owner
        await kept.execute("DELETE FROM tasks")

        assert await check(dataset) == {Refusal("session_connected", None, name, await pid(kept))}
    finally:
        await kept.close()


async def test_a_session_whose_role_no_longer_exists_refuses_as_unattributed(dataset: Dataset) -> None:
    name, dsn = await dataset.login()
    orphaned = await asyncpg.connect(dsn)
    try:
        orphaned_pid = await pid(orphaned)
        await dataset.admin.execute(f"DROP OWNED BY {name}")
        await dataset.admin.execute(f"DROP ROLE {name}")
        owner = await dataset.owner_connection()
        try:
            # The owner sees the row's role id, and no role has it.
            assert await owner.fetchval(
                "SELECT usesysid IS NOT NULL AND usesysid NOT IN (SELECT oid FROM pg_roles)"
                " FROM pg_stat_activity WHERE pid = $1", int(orphaned_pid)
            )
        finally:
            await owner.close()

        assert await check(dataset) == {Refusal("session_unattributed", None, None, orphaned_pid)}
    finally:
        await orphaned.close()


async def test_a_background_worker_carrying_a_role_is_checked_as_that_role(dataset: Dataset) -> None:
    name, dsn = await dataset.login()
    leader = await asyncpg.connect(dsn)
    running: asyncio.Task[Any] | None = None
    try:
        leader_pid = await pid(leader)
        await leader.execute("SET debug_parallel_query = on")
        # A row-limited fetch never runs in parallel: the simple protocol has no limit.
        running = asyncio.create_task(leader.execute("SELECT pg_sleep(60)"))
        worker_pid = str(await until(lambda: dataset.admin.fetch_value(
            "SELECT pid FROM pg_stat_activity WHERE leader_pid = $1 AND backend_type = 'parallel worker'",
            int(leader_pid),
        ), "a parallel worker for the login's query"))
        owner = await dataset.owner_connection()
        try:
            # The owner sees the worker's role, not its kind.
            assert await owner.fetchval(
                "SELECT usesysid = $2::regrole FROM pg_stat_activity WHERE pid = $1", int(worker_pid), name
            )
        finally:
            await owner.close()

        assert await check(dataset) == {
            Refusal("session_connected", None, name, leader_pid),
            Refusal("session_connected", None, name, worker_pid),
        }
        assert await check(dataset, name) == set()
    finally:
        if running is not None:
            await dataset.admin.execute(f"SELECT pg_cancel_backend({leader_pid})")
            with pytest.raises(asyncpg.QueryCanceledError):
                await running
        await leader.close()


# --- the platform roles -------------------------------------------------------


@pytest.mark.parametrize("kind", ["owner", "superuser", "unknown", "nologin"])
async def test_a_platform_role_must_be_an_existing_login_principal(dataset: Dataset, kind: str) -> None:
    name = {
        "owner": dataset.owner,
        "superuser": await superuser_name(dataset),
        "unknown": f"writer_check_{uuid4().hex[:12]}",
        "nologin": await dataset.role("NOLOGIN") if kind == "nologin" else "",
    }[kind]
    refused = {
        "owner": Refusal("platform_role_owner", None, name, None),
        "superuser": Refusal("platform_role_superuser", None, name, None),
        "unknown": Refusal("platform_role_unknown", None, None, name),
        "nologin": Refusal("platform_role_not_login", None, name, None),
    }[kind]

    assert await check(dataset, name) == {refused}


# --- the lock and P -----------------------------------------------------------


async def test_an_uncommitted_write_refuses_at_the_lock(dataset: Dataset) -> None:
    writer = await dataset.owner_connection()
    try:
        async with writer.transaction():
            await writer.execute("DELETE FROM tasks")
            # The lock times out before C3 would see the transaction.
            assert await check(dataset) == {Refusal("lock_timeout", None, None, None)}
    finally:
        await writer.close()


async def rollback_prepared(database: Any, gid: str) -> None:
    """Rolls back the transaction prepared as ``gid``, if there is one, through a fresh connection."""
    if await database.fetch_value("SELECT 1 FROM pg_prepared_xacts WHERE gid = $1", gid):
        await database.execute(f"ROLLBACK PREPARED '{gid}'")


async def test_a_prepared_transaction_in_this_database_refuses(dataset: Dataset) -> None:
    assert int(await dataset.admin.fetch_value("SHOW max_prepared_transactions")) > 0, (
        "the test PostgreSQL server needs max_prepared_transactions > 0 to prepare a transaction"
    )
    gid = f"writer_check_{uuid4().hex[:12]}"
    # A transaction that wrote outside the dataset leaves the dataset's locks obtainable.
    await dataset.as_owner("CREATE TABLE scratch (n int)")
    try:
        preparing = await dataset.owner_connection()
        try:
            await preparing.execute("BEGIN")
            await preparing.execute("INSERT INTO scratch VALUES (1)")
            await preparing.execute(f"PREPARE TRANSACTION '{gid}'")
        finally:
            await preparing.close()

        assert await check(dataset) == {Refusal("prepared_transaction", None, dataset.owner, gid)}
    finally:
        await rollback_prepared(dataset.admin, gid)


# --- S and A3 in the check ----------------------------------------------------


async def test_the_check_refuses_on_write_authority(dataset: Dataset) -> None:
    member = await dataset.role("LOGIN")
    await dataset.admin.execute(f"GRANT {dataset.owner} TO {member}")

    assert Refusal("write_authority_owner", member, dataset.owner, None) in await check(dataset)


async def test_a_missing_dataset_table_refuses_as_the_shape_not_at_the_lock(dataset: Dataset) -> None:
    await dataset.as_owner("DROP TABLE dependencies")

    assert await check(dataset) == {
        Refusal("shape_missing_table", None, None, "dependencies"),
        Refusal("lock_not_held", None, None, "dependencies"),
    }


async def test_a_dataset_table_that_appears_after_the_lock_step_refuses(dataset: Dataset) -> None:
    # The job's three files, in one transaction, with the table back between the lock and S.
    await dataset.as_owner("ALTER TABLE dependencies RENAME TO dependencies_away")
    checking = await dataset.owner_connection()
    try:
        async with checking.transaction():
            await checking.execute(move.LOCK_SQL.read_text(encoding="utf-8"))
            await dataset.as_owner("ALTER TABLE dependencies_away RENAME TO dependencies")
            authority = await checking.fetch(move.WRITER_AUTHORITY_SQL.read_text(encoding="utf-8"))
            sessions = await checking.fetch(move.SESSIONS_SQL.read_text(encoding="utf-8"))
    finally:
        await checking.close()

    # S sees all seven, as they are now; only the lock check sees the one this transaction did not lock.
    assert {Refusal(**dict(row)) for row in authority} == set()
    assert {Refusal(**dict(row)) for row in sessions} == {Refusal("lock_not_held", None, None, "dependencies")}


@pytest.mark.parametrize(("replacement", "refused"), [
    pytest.param("CREATE SEQUENCE dependencies", "shape_not_ordinary_table", id="a sequence in its place"),
    pytest.param("CREATE SCHEMA elsewhere; CREATE TABLE elsewhere.dependencies (id integer)",
                 "shape_missing_table", id="a table of its name in another schema"),
])
async def test_a_dataset_table_replaced_by_another_relation_refuses_as_the_shape(
    dataset: Dataset, replacement: str, refused: str,
) -> None:
    await dataset.as_owner(f"DROP TABLE dependencies; {replacement}")

    assert refused in {refusal.code for refusal in await check(dataset)}


async def test_a_prepared_transaction_in_another_database_passes(dataset: Dataset, test_db_factory: Any) -> None:
    other = await test_db_factory.create_db(suffix="writer_check_other")
    gid = f"writer_check_{uuid4().hex[:12]}"
    try:
        preparing = await asyncpg.connect(other.config.get_dsn())
        try:
            await preparing.execute("BEGIN")
            await preparing.execute("SELECT 1")
            await preparing.execute(f"PREPARE TRANSACTION '{gid}'")
        finally:
            await preparing.close()

        assert await check(dataset) == set()
    finally:
        await rollback_prepared(other, gid)


@pytest.mark.parametrize("held", ["a weaker lock of its own", "another backend's exclusive lock"])
async def test_a_dataset_lock_this_transaction_does_not_hold_exclusive_refuses(dataset: Dataset, held: str) -> None:
    checking = await dataset.owner_connection()
    other = await dataset.owner_connection()
    try:
        async with other.transaction():
            async with checking.transaction():
                if held == "a weaker lock of its own":
                    await checking.execute(f"LOCK TABLE {', '.join(move.TABLES)} IN ROW SHARE MODE")
                else:
                    await other.execute(move.LOCK_SQL.read_text(encoding="utf-8"))
                sessions = await checking.fetch(move.SESSIONS_SQL.read_text(encoding="utf-8"))
    finally:
        await checking.close()
        await other.close()

    assert {Refusal(**dict(row)) for row in sessions if row["code"] == "lock_not_held"} == {
        Refusal("lock_not_held", None, None, table) for table in move.TABLES
    }
