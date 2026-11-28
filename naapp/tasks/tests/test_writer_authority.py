"""The board move's shape and write-authority predicates (writer_authority.sql),
run as the dataset's owner role against the schema the app's migrations build.

Each test gets its own database. Roles are cluster-wide, so every role a test
creates is dropped when it ends. The tests assert refusal codes, never wording.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from uuid import uuid4

import asyncpg
import pytest

from aweb_tasks.board_move import move
from aweb_tasks.board_move.move import Refusal

from .owned_dataset import Dataset

DATASET = ("installations", "tasks", "claims", "locks", "comments", "dependencies", "events")


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

    assert await dataset.refusals() == {Refusal("indirection_view", None, dataset.owner, "outside_view")}


async def test_a_security_definer_function_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE TABLE outside (id INTEGER);"
        " CREATE FUNCTION outside_write() RETURNS void LANGUAGE sql SECURITY DEFINER"
        " AS 'INSERT INTO outside VALUES (1)'"
    )

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, dataset.owner, "outside_write()"),
    }


async def test_a_security_definer_procedure_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    await dataset.as_owner(
        "CREATE PROCEDURE outside_call(id integer) LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"
    )

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, dataset.owner, "outside_call(integer)"),
    }


async def test_a_dml_rule_owned_by_the_owner_refuses(dataset: Dataset) -> None:
    # The rule's action runs as its relation's owner.
    await dataset.as_owner(
        "CREATE TABLE outside (id INTEGER);"
        " CREATE RULE outside_into_tasks AS ON INSERT TO outside DO ALSO"
        " DELETE FROM tasks WHERE number = NEW.id"
    )

    assert await dataset.refusals() == {Refusal("indirection_rule", None, dataset.owner, "outside_into_tasks")}


async def test_an_invoker_function_and_a_trigger_pass(dataset: Dataset) -> None:
    # Triggers fire as the caller; only a SECURITY DEFINER function would not.
    await dataset.as_owner(
        "CREATE FUNCTION stamp() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';"
        " CREATE TRIGGER stamp BEFORE INSERT ON tasks FOR EACH ROW EXECUTE FUNCTION stamp()"
    )

    assert await dataset.refusals() == set()


# --- A3: indirection through objects any other writer owns --------------------


async def _writes(dsn: str, schema: str, sql: str) -> None:
    connection = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


@dataclass(frozen=True)
class Indirection:
    code: str
    object: str
    # Run as the writer's owning role's creator (a superuser), then owned by the writer.
    create: str
    owned: str
    # What the principal runs to write the dataset through it.
    write: str


INDIRECTIONS = [
    Indirection(
        "indirection_view", "tasks_view",
        "CREATE VIEW tasks_view AS SELECT * FROM tasks",
        "ALTER VIEW tasks_view OWNER TO {writer}; GRANT DELETE ON tasks_view TO {login}",
        "DELETE FROM tasks_view",
    ),
    Indirection(
        "indirection_security_definer", "delete_tasks()",
        "CREATE FUNCTION delete_tasks() RETURNS void LANGUAGE sql SECURITY DEFINER"
        " AS 'DELETE FROM {schema}.tasks'",
        "ALTER FUNCTION delete_tasks() OWNER TO {writer}",
        "SELECT delete_tasks()",
    ),
    Indirection(
        "indirection_rule", "outside_deletes_tasks",
        "CREATE TABLE outside (id INTEGER);"
        " CREATE RULE outside_deletes_tasks AS ON INSERT TO outside DO ALSO DELETE FROM {schema}.tasks",
        "ALTER TABLE outside OWNER TO {writer}; GRANT INSERT ON outside TO {login}",
        "INSERT INTO outside VALUES (1)",
    ),
]


@pytest.mark.parametrize("indirection", INDIRECTIONS, ids=[i.code for i in INDIRECTIONS])
async def test_an_object_owned_by_an_unreached_writer_refuses(
    dataset: Dataset, indirection: Indirection,
) -> None:
    # A NOLOGIN writer no principal reaches: A3's closure alone passes it.
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(f'GRANT USAGE ON SCHEMA "{dataset.schema}" TO {writer}')
    login, dsn = await dataset.login()
    names = {"writer": writer, "login": login, "schema": f'"{dataset.schema}"'}
    await dataset.admin.execute(indirection.create.format(**names))
    await dataset.admin.execute(indirection.owned.format(**names))

    # The login can't write the dataset itself, but writes it through the object.
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await _writes(dsn, dataset.schema, "DELETE FROM tasks")
    await _writes(dsn, dataset.schema, indirection.write)

    assert await dataset.refusals() == {Refusal(indirection.code, None, writer, indirection.object)}


async def test_a_view_owned_by_a_superuser_outside_the_system_schemas_refuses(dataset: Dataset) -> None:
    superuser = await dataset.admin.fetch_value("SELECT current_user")
    await dataset.admin.execute(f'CREATE VIEW "{dataset.schema}".tasks_view AS SELECT * FROM "{dataset.schema}".tasks')

    assert await dataset.refusals() == {Refusal("indirection_view", None, superuser, "tasks_view")}


async def test_an_object_owned_by_a_role_that_cannot_write_passes(dataset: Dataset) -> None:
    reader = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT SELECT ON "{dataset.schema}".tasks TO {reader}')
    await dataset.admin.execute(f'CREATE VIEW "{dataset.schema}".tasks_view AS SELECT * FROM "{dataset.schema}".tasks')
    await dataset.admin.execute(f'ALTER VIEW "{dataset.schema}".tasks_view OWNER TO {reader}')
    await dataset.admin.execute(
        f'CREATE FUNCTION "{dataset.schema}".count_tasks() RETURNS bigint LANGUAGE sql SECURITY DEFINER'
        f' AS \'SELECT count(*) FROM "{dataset.schema}".tasks\''
    )
    await dataset.admin.execute(f'ALTER FUNCTION "{dataset.schema}".count_tasks() OWNER TO {reader}')

    assert await dataset.refusals() == set()


async def test_a_trigger_grant_refuses(dataset: Dataset) -> None:
    # A trigger the login attaches runs with the rights of whoever next writes.
    login = await dataset.role("LOGIN")
    await dataset.admin.execute(f'GRANT TRIGGER ON "{dataset.schema}".locks TO {login}')

    assert await dataset.refusals() == {
        Refusal("write_authority_trigger_privilege", login, login, "locks"),
    }


async def test_a_materialized_view_owned_by_an_unreached_writer_refuses(dataset: Dataset) -> None:
    # A refresh runs the view's query, and the functions it calls, as the view's owner.
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(f'GRANT USAGE ON SCHEMA "{dataset.schema}" TO {writer}')
    login, dsn = await dataset.login()
    schema = f'"{dataset.schema}"'
    await dataset.admin.execute(
        f"CREATE FUNCTION {schema}.deleted_tasks() RETURNS bigint LANGUAGE sql"
        f" AS 'WITH gone AS (DELETE FROM {schema}.tasks RETURNING 1) SELECT count(*) FROM gone'"
    )
    await dataset.admin.execute(
        f"CREATE MATERIALIZED VIEW {schema}.tasks_gone AS SELECT {schema}.deleted_tasks() WITH NO DATA"
    )
    await dataset.admin.execute(f"ALTER MATERIALIZED VIEW {schema}.tasks_gone OWNER TO {writer}")
    await dataset.admin.execute(f"GRANT MAINTAIN ON {schema}.tasks_gone TO {login}")

    await _writes(dsn, dataset.schema, "REFRESH MATERIALIZED VIEW tasks_gone")

    assert await dataset.refusals() == {
        Refusal("indirection_view", None, writer, "tasks_gone"),
        Refusal("write_authority_maintain_privilege", login, login, "tasks_gone"),
    }


async def test_a_maintain_grant_on_a_dataset_table_refuses(dataset: Dataset) -> None:
    # Maintenance runs index and statistics expressions as the table's owner.
    login = await dataset.role("LOGIN")
    await dataset.admin.execute(f'GRANT MAINTAIN ON "{dataset.schema}".comments TO {login}')

    assert await dataset.refusals() == {
        Refusal("write_authority_maintain_privilege", login, login, "comments"),
    }


async def test_a_maintain_grant_on_a_writer_owned_outside_table_refuses(dataset: Dataset) -> None:
    login = await dataset.role("LOGIN")
    await dataset.as_owner("CREATE TABLE outside (id INTEGER)")
    await dataset.admin.execute(f'GRANT MAINTAIN ON "{dataset.schema}".outside TO {login}')

    assert await dataset.refusals() == {
        Refusal("write_authority_maintain_privilege", login, login, "outside"),
    }


async def test_a_login_that_reaches_pg_maintain_refuses(dataset: Dataset) -> None:
    login = await dataset.role("LOGIN NOINHERIT")
    await dataset.admin.execute(f"GRANT pg_maintain TO {login}")

    refusals = await dataset.refusals()
    assert {refusal.code for refusal in refusals} == {"write_authority_maintain_privilege"}
    assert {(refusal.principal, refusal.role) for refusal in refusals} == {(login, "pg_maintain")}
    assert {"comments", "tasks"} <= {refusal.object for refusal in refusals}


async def test_a_member_of_an_unreached_writer_owning_a_security_definer_function_refuses(
    dataset: Dataset,
) -> None:
    # Membership in a writer, in any mode, makes a writer.
    writer = await dataset.role("NOLOGIN")
    member = await dataset.role("NOLOGIN NOINHERIT")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(f"GRANT {writer} TO {member}")
    await dataset.admin.execute(
        f'CREATE FUNCTION "{dataset.schema}".noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS \'SELECT\''
    )
    await dataset.admin.execute(f'ALTER FUNCTION "{dataset.schema}".noop() OWNER TO {member}')

    assert await dataset.refusals() == {Refusal("indirection_security_definer", None, member, "noop()")}


@pytest.mark.parametrize("grant", ["TRIGGER", "MAINTAIN", "UPDATE (title)"])
async def test_a_security_definer_function_owned_by_any_kind_of_writer_refuses(
    dataset: Dataset, grant: str,
) -> None:
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT {grant} ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(
        f'CREATE FUNCTION "{dataset.schema}".noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS \'SELECT\''
    )
    await dataset.admin.execute(f'ALTER FUNCTION "{dataset.schema}".noop() OWNER TO {writer}')

    assert await dataset.refusals() == {Refusal("indirection_security_definer", None, writer, "noop()")}


# --- writers whose authority no ACL shows -------------------------------------


async def test_the_owner_is_a_writer_even_with_its_own_privileges_revoked(dataset: Dataset) -> None:
    # An owner may revoke its own privileges, and can always grant them back.
    for table in DATASET:
        await dataset.as_owner(f"REVOKE ALL ON {table} FROM {dataset.owner}")
    await dataset.as_owner(
        "CREATE FUNCTION noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS 'SELECT'"
    )
    assert not await dataset.admin.fetch_value(
        f"SELECT has_table_privilege('{dataset.owner}', '\"{dataset.schema}\".tasks', 'DELETE')"
    )

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, dataset.owner, "noop()"),
    }


async def _owner_callback(dataset: Dataset, schema: str, table_owner: str) -> str:
    """A table in ``schema`` owned by ``table_owner``, whose index function writes the
    dataset and records the role it runs as in ``{schema}.seen``. Returns the table."""
    live = f'"{dataset.schema}"'
    await dataset.admin.execute(f"CREATE TABLE {schema}.seen (role TEXT)")
    await dataset.admin.execute(
        f"CREATE FUNCTION {schema}.record() RETURNS void LANGUAGE plpgsql AS"
        f" 'BEGIN INSERT INTO {schema}.seen VALUES (current_user); DELETE FROM {live}.tasks; END'"
    )
    await dataset.admin.execute(
        f"CREATE FUNCTION {schema}.indexed(integer) RETURNS integer LANGUAGE plpgsql IMMUTABLE AS"
        f" 'BEGIN PERFORM {schema}.record(); RETURN $1; END'"
    )
    await dataset.admin.execute(f"CREATE TABLE {schema}.callback (id INTEGER)")
    await dataset.admin.execute(f"INSERT INTO {schema}.callback VALUES (1)")
    await dataset.admin.execute(f"CREATE INDEX ON {schema}.callback ({schema}.indexed(id))")
    await dataset.admin.execute(f"ALTER TABLE {schema}.callback OWNER TO {table_owner}")
    await dataset.admin.execute(f"ALTER TABLE {schema}.seen OWNER TO {table_owner}")
    await dataset.admin.execute(f"TRUNCATE {schema}.seen")
    return f"{schema}.callback"


async def test_a_schema_owner_reindexing_a_writers_table_refuses(dataset: Dataset) -> None:
    # A schema's owner may reindex every table in it, without MAINTAIN on any,
    # and the index functions run as each table's owner.
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(f'GRANT USAGE ON SCHEMA "{dataset.schema}" TO {writer}')
    login, dsn = await dataset.login()
    await dataset.admin.execute(f"CREATE SCHEMA maintained AUTHORIZATION {login}")
    await dataset.admin.execute(f"GRANT USAGE ON SCHEMA maintained TO {writer}")
    table = await _owner_callback(dataset, "maintained", writer)
    assert not await dataset.admin.fetch_value(f"SELECT has_table_privilege('{login}', '{table}', 'MAINTAIN')")

    await _writes(dsn, dataset.schema, "REINDEX SCHEMA maintained")
    assert await dataset.admin.fetch_value("SELECT array_agg(DISTINCT role) FROM maintained.seen") == [writer]

    # The writer's table names a function in its index, which refuses on its own.
    assert await dataset.refusals() == {
        Refusal("write_authority_maintenance_scope", login, login, "maintained"),
        Refusal("indirection_index_expression", None, writer, "maintained.callback_indexed_idx"),
    }


async def test_a_login_that_reaches_the_database_owner_refuses(dataset: Dataset) -> None:
    owner = await dataset.admin.fetch_value(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
    )
    login = await dataset.role("LOGIN")
    await dataset.admin.execute(f'ALTER DATABASE "{dataset.database}" OWNER TO {login}')
    try:
        assert await dataset.refusals() == {
            Refusal("write_authority_maintenance_scope", login, login, dataset.database),
        }
    finally:
        await dataset.admin.execute(f'ALTER DATABASE "{dataset.database}" OWNER TO "{owner}"')


async def test_a_security_definer_function_owned_by_a_maintainer_of_a_writers_table_refuses(
    dataset: Dataset,
) -> None:
    # MAINTAIN on a writer's table makes a writer: reindexing runs the table owner's code.
    maintainer = await dataset.role("NOLOGIN")
    login, dsn = await dataset.login()
    live = f'"{dataset.schema}"'
    table = await _owner_callback(dataset, live, dataset.owner)
    await dataset.admin.execute(f"GRANT MAINTAIN ON {table} TO {maintainer}")
    await dataset.admin.execute(f"GRANT USAGE ON SCHEMA {live} TO {maintainer}")
    await dataset.admin.execute(
        f"CREATE FUNCTION {live}.rebuild() RETURNS void LANGUAGE sql SECURITY DEFINER"
        f" AS 'REINDEX TABLE {table}'"
    )
    await dataset.admin.execute(f"ALTER FUNCTION {live}.rebuild() OWNER TO {maintainer}")

    await _writes(dsn, dataset.schema, "SELECT rebuild()")
    assert await dataset.admin.fetch_value(f"SELECT array_agg(DISTINCT role) FROM {live}.seen") == [dataset.owner]

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, maintainer, "rebuild()"),
        Refusal("indirection_index_expression", None, dataset.owner, "callback_indexed_idx"),
    }


async def test_a_table_owner_is_a_writer_even_with_its_own_privileges_revoked(dataset: Dataset) -> None:
    other = await dataset.role("NOLOGIN")
    live = f'"{dataset.schema}"'
    await dataset.admin.execute(f"ALTER TABLE {live}.claims OWNER TO {other}")
    await dataset.admin.execute(f"REVOKE ALL ON {live}.claims FROM {other}")
    await dataset.admin.execute(f"CREATE FUNCTION {live}.noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS 'SELECT'")
    await dataset.admin.execute(f"ALTER FUNCTION {live}.noop() OWNER TO {other}")

    assert await dataset.refusals() == {
        Refusal("shape_not_owned", None, None, "claims"),
        Refusal("indirection_security_definer", None, other, "noop()"),
    }


async def test_the_database_owner_is_a_writer(dataset: Dataset) -> None:
    # It may reindex every table in the database, the dataset's included.
    owner = await dataset.admin.fetch_value(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
    )
    database_owner = await dataset.role("NOLOGIN")
    live = f'"{dataset.schema}"'
    await dataset.admin.execute(f'ALTER DATABASE "{dataset.database}" OWNER TO {database_owner}')
    try:
        await dataset.admin.execute(f"CREATE FUNCTION {live}.noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS 'SELECT'")
        await dataset.admin.execute(f"ALTER FUNCTION {live}.noop() OWNER TO {database_owner}")

        assert await dataset.refusals() == {
            Refusal("indirection_security_definer", None, database_owner, "noop()"),
        }
    finally:
        await dataset.admin.execute(f'ALTER DATABASE "{dataset.database}" OWNER TO "{owner}"')


async def test_the_owner_of_a_schema_holding_a_writers_table_is_a_writer(dataset: Dataset) -> None:
    schema_owner = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f"CREATE SCHEMA maintained AUTHORIZATION {schema_owner}")
    await dataset.admin.execute("CREATE TABLE maintained.outside (id INTEGER)")
    await dataset.admin.execute(f"ALTER TABLE maintained.outside OWNER TO {dataset.owner}")
    await dataset.admin.execute("CREATE FUNCTION maintained.noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS 'SELECT'")
    await dataset.admin.execute(f"ALTER FUNCTION maintained.noop() OWNER TO {schema_owner}")

    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, schema_owner, "maintained.noop()"),
    }


async def test_a_login_owning_a_schema_with_no_writers_table_passes(dataset: Dataset) -> None:
    login = await dataset.role("LOGIN")
    await dataset.admin.execute(f"CREATE SCHEMA own AUTHORIZATION {login}")
    await dataset.admin.execute("CREATE TABLE own.notes (id INTEGER)")
    await dataset.admin.execute(f"ALTER TABLE own.notes OWNER TO {login}")

    assert await dataset.refusals() == set()


async def test_the_owner_is_a_writer_even_when_it_owns_no_dataset_table(dataset: Dataset) -> None:
    other = await dataset.role("NOLOGIN")
    live = f'"{dataset.schema}"'
    # Nor the schema, which would make it a writer as the schema's owner.
    await dataset.admin.execute(f"ALTER SCHEMA {live} OWNER TO {other}")
    await dataset.admin.execute(f"GRANT USAGE ON SCHEMA {live} TO {dataset.owner}")
    for table in DATASET:
        await dataset.admin.execute(f"ALTER TABLE {live}.{table} OWNER TO {other}")
    await dataset.admin.execute(f"CREATE FUNCTION {live}.noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS 'SELECT'")
    await dataset.admin.execute(f"ALTER FUNCTION {live}.noop() OWNER TO {dataset.owner}")

    refusals = await dataset.refusals()
    assert Refusal("indirection_security_definer", None, dataset.owner, "noop()") in refusals
    assert {refusal.object for refusal in refusals if refusal.code == "shape_not_owned"} == set(DATASET)


# --- maintenance through a partitioned parent ---------------------------------


async def _partitioned_callback(dataset: Dataset, parent_owner: str, leaf_owner: str) -> None:
    """``maintained.parent``, owned by ``parent_owner``, with one leaf owned by
    ``leaf_owner``. The index function writes the dataset and records the role it
    runs as in ``maintained.seen``."""
    live = f'"{dataset.schema}"'
    await dataset.admin.execute("CREATE SCHEMA maintained")
    for role in {parent_owner, leaf_owner}:
        await dataset.admin.execute(f"GRANT USAGE ON SCHEMA maintained TO {role}")
    await dataset.admin.execute("CREATE TABLE maintained.seen (role TEXT)")
    await dataset.admin.execute("GRANT INSERT ON maintained.seen TO PUBLIC")
    await dataset.admin.execute(
        "CREATE FUNCTION maintained.record() RETURNS void LANGUAGE plpgsql AS"
        f" 'BEGIN INSERT INTO maintained.seen VALUES (current_user); DELETE FROM {live}.tasks; END'"
    )
    await dataset.admin.execute(
        "CREATE FUNCTION maintained.indexed(integer) RETURNS integer LANGUAGE plpgsql IMMUTABLE AS"
        " 'BEGIN PERFORM maintained.record(); RETURN $1; END'"
    )
    await dataset.admin.execute("CREATE TABLE maintained.parent (id INTEGER) PARTITION BY RANGE (id)")
    await dataset.admin.execute("CREATE TABLE maintained.leaf PARTITION OF maintained.parent FOR VALUES FROM (0) TO (10)")
    await dataset.admin.execute("INSERT INTO maintained.parent VALUES (1)")
    await dataset.admin.execute("CREATE INDEX ON maintained.parent (maintained.indexed(id))")
    await dataset.admin.execute(f"ALTER TABLE maintained.parent OWNER TO {parent_owner}")
    await dataset.admin.execute(f"ALTER TABLE maintained.leaf OWNER TO {leaf_owner}")
    await dataset.admin.execute("TRUNCATE maintained.seen")


async def test_maintaining_a_parent_of_a_writers_partition_refuses(dataset: Dataset) -> None:
    # MAINTAIN on the parent reindexes the leaf, whose index functions run as
    # the leaf's owner, with no MAINTAIN on the leaf.
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    await dataset.admin.execute(f'GRANT USAGE ON SCHEMA "{dataset.schema}" TO {writer}')
    parent_owner = await dataset.role("NOLOGIN")
    login, dsn = await dataset.login()
    await _partitioned_callback(dataset, parent_owner, writer)
    await dataset.admin.execute(f"GRANT USAGE ON SCHEMA maintained TO {login}")
    await dataset.admin.execute(f"GRANT MAINTAIN ON maintained.parent TO {login}")
    assert not await dataset.admin.fetch_value(
        f"SELECT has_table_privilege('{login}', 'maintained.leaf', 'MAINTAIN')"
    )

    await _writes(dsn, dataset.schema, "REINDEX TABLE maintained.parent")
    assert await dataset.admin.fetch_value("SELECT array_agg(DISTINCT role) FROM maintained.seen") == [writer]

    refusals = await dataset.refusals()
    assert Refusal("write_authority_maintain_privilege", login, login, "maintained.parent") in refusals
    assert {refusal.principal for refusal in refusals} - {None} == {login}


async def test_owning_a_parent_of_a_writers_partition_refuses(dataset: Dataset) -> None:
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    login = await dataset.role("LOGIN")
    await _partitioned_callback(dataset, login, writer)
    # An owner can grant itself back whatever privilege it revoked.
    await dataset.admin.execute(f"REVOKE ALL ON maintained.parent FROM {login}")

    assert await dataset.refusals() == {
        Refusal("write_authority_maintenance_owner", login, login, "maintained.parent"),
        Refusal("indirection_index_expression", None, login, "maintained.parent_indexed_idx"),
        Refusal("indirection_index_expression", None, writer, "maintained.leaf_indexed_idx"),
    }


async def test_a_security_definer_function_owned_by_a_parent_maintainer_refuses(dataset: Dataset) -> None:
    writer = await dataset.role("NOLOGIN")
    await dataset.admin.execute(f'GRANT DELETE ON "{dataset.schema}".tasks TO {writer}')
    parent_owner = await dataset.role("NOLOGIN")
    maintainer = await dataset.role("NOLOGIN")
    await _partitioned_callback(dataset, parent_owner, writer)
    await dataset.admin.execute(f"GRANT MAINTAIN ON maintained.parent TO {maintainer}")
    await dataset.admin.execute(
        "CREATE FUNCTION maintained.noop() RETURNS void LANGUAGE sql SECURITY DEFINER AS 'SELECT'"
    )
    await dataset.admin.execute(f"ALTER FUNCTION maintained.noop() OWNER TO {maintainer}")

    # The parent's owner is a writer too, so its routine would refuse as well.
    assert await dataset.refusals() == {
        Refusal("indirection_security_definer", None, maintainer, "maintained.noop()"),
        Refusal("indirection_index_expression", None, parent_owner, "maintained.parent_indexed_idx"),
        Refusal("indirection_index_expression", None, writer, "maintained.leaf_indexed_idx"),
    }


async def test_maintaining_a_parent_with_no_writers_partition_passes(dataset: Dataset) -> None:
    neighbour = await dataset.role("NOLOGIN")
    login = await dataset.role("LOGIN")
    await _partitioned_callback(dataset, neighbour, neighbour)
    await dataset.admin.execute(f"GRANT MAINTAIN ON maintained.parent TO {login}")

    assert await dataset.refusals() == set()


# --- what maintenance runs as a writer ----------------------------------------
# ANALYZE, which autovacuum runs with no principal at any time, switches to each
# relation's owner and evaluates what the relation's definition names. So no
# writer-owned relation may name code or support outside PostgreSQL's own.


async def _maintained_schema(dataset: Dataset) -> None:
    """Schema maintained, which the owner can use, with maintained.indexed(integer)
    and maintained.diff(float8, float8): each records the role it runs as in
    maintained.seen, owned by the owner, and deletes the dataset's tasks."""
    live = f'"{dataset.schema}"'
    await dataset.admin.execute("CREATE SCHEMA maintained")
    await dataset.admin.execute(f"GRANT USAGE ON SCHEMA maintained TO {dataset.owner}")
    await dataset.admin.execute("CREATE TABLE maintained.seen (role TEXT)")
    await dataset.admin.execute(f"ALTER TABLE maintained.seen OWNER TO {dataset.owner}")
    await dataset.admin.execute(
        "CREATE FUNCTION maintained.record() RETURNS void LANGUAGE plpgsql AS"
        f" 'BEGIN INSERT INTO maintained.seen VALUES (current_user); DELETE FROM {live}.tasks; END'"
    )
    await dataset.admin.execute(
        "CREATE FUNCTION maintained.indexed(integer) RETURNS integer LANGUAGE plpgsql IMMUTABLE AS"
        " 'BEGIN PERFORM maintained.record(); RETURN $1; END'"
    )
    await dataset.admin.execute(
        "CREATE FUNCTION maintained.diff(float8, float8) RETURNS float8 LANGUAGE plpgsql IMMUTABLE AS"
        " 'BEGIN PERFORM maintained.record(); RETURN $1 - $2; END'"
    )


@pytest.mark.parametrize(("table", "named", "code", "object"), [
    pytest.param(
        "CREATE TABLE maintained.analysed (id integer)",
        "CREATE INDEX analysed_expression ON maintained.analysed (maintained.indexed(id))",
        "indirection_index_expression", "maintained.analysed_expression", id="index expression",
    ),
    pytest.param(
        "CREATE TABLE maintained.analysed (id integer)",
        "CREATE INDEX analysed_predicate ON maintained.analysed (id) WHERE maintained.indexed(id) > 0",
        "indirection_index_expression", "maintained.analysed_predicate", id="index predicate",
    ),
    pytest.param(
        "CREATE TABLE maintained.analysed (id integer)",
        "CREATE STATISTICS maintained.analysed_statistics ON (maintained.indexed(id)) FROM maintained.analysed",
        "indirection_statistics_expression", "maintained.analysed_statistics", id="statistics expression",
    ),
    pytest.param(
        "CREATE TYPE maintained.span AS RANGE (subtype = float8, subtype_diff = maintained.diff);"
        " CREATE TABLE maintained.analysed (id integer, span maintained.span)",
        "UPDATE maintained.analysed SET span = maintained.span(id, id + 1)",
        "indirection_column_type", "maintained.analysed.span", id="range subtype",
    ),
])
async def test_analyze_runs_what_a_writers_relation_names_as_its_owner_and_refuses(
    dataset: Dataset, table: str, named: str, code: str, object: str,
) -> None:
    await _maintained_schema(dataset)
    await dataset.admin.execute(table)
    await dataset.admin.execute("INSERT INTO maintained.analysed (id) SELECT generate_series(1, 100)")
    await dataset.admin.execute(named)
    await dataset.admin.execute(f"ALTER TABLE maintained.analysed OWNER TO {dataset.owner}")
    await dataset.admin.execute("TRUNCATE maintained.seen")

    # Run by the superuser, as autovacuum's is, ANALYZE runs the named code as the table's owner.
    await dataset.admin.execute("ANALYZE maintained.analysed")
    assert await dataset.admin.fetch_value("SELECT array_agg(DISTINCT role) FROM maintained.seen") == [
        dataset.owner
    ]

    assert await dataset.refusals() == {Refusal(code, None, dataset.owner, object)}


@pytest.mark.parametrize(("statements", "code", "object"), [
    pytest.param(
        ["CREATE TYPE maintained.mood AS ENUM ('calm')",
         "CREATE TABLE maintained.analysed (mood maintained.mood)"],
        "indirection_column_type", "maintained.analysed.mood", id="column type",
    ),
    pytest.param(
        ['CREATE COLLATION maintained.ordered FROM "C"',
         "CREATE TABLE maintained.analysed (label text COLLATE maintained.ordered)"],
        "indirection_collation", "maintained.analysed.label", id="column collation",
    ),
    pytest.param(
        ['CREATE COLLATION maintained.ordered FROM "C"',
         "CREATE TABLE maintained.analysed (label text)",
         "CREATE INDEX analysed_ordered ON maintained.analysed (label COLLATE maintained.ordered)"],
        "indirection_collation", "maintained.analysed_ordered", id="index collation",
    ),
    pytest.param(
        ["CREATE OPERATOR CLASS maintained.int4_order FOR TYPE int4 USING btree AS"
         " OPERATOR 1 <, OPERATOR 2 <=, OPERATOR 3 =, OPERATOR 4 >=, OPERATOR 5 >,"
         " FUNCTION 1 btint4cmp(int4, int4)",
         "CREATE TABLE maintained.analysed (id integer)",
         "CREATE INDEX analysed_ordered ON maintained.analysed (id maintained.int4_order)"],
        "indirection_operator_class", "maintained.analysed_ordered", id="operator class",
    ),
    pytest.param(
        ["CREATE ACCESS METHOD writer_check_heap TYPE TABLE HANDLER heap_tableam_handler",
         "CREATE TABLE maintained.analysed (id integer) USING writer_check_heap"],
        "indirection_access_method", "maintained.analysed", id="table access method",
    ),
])
async def test_a_writers_relation_naming_support_outside_postgresql_refuses(
    dataset: Dataset, statements: list[str], code: str, object: str,
) -> None:
    await _maintained_schema(dataset)
    for statement in statements:
        await dataset.admin.execute(statement)
    await dataset.admin.execute(f"ALTER TABLE maintained.analysed OWNER TO {dataset.owner}")

    assert await dataset.refusals() == {Refusal(code, None, dataset.owner, object)}


async def test_a_writers_index_on_an_access_method_outside_postgresql_refuses(dataset: Dataset) -> None:
    await _maintained_schema(dataset)
    # contrib's bloom, an index access method an extension adds.
    await dataset.admin.execute("CREATE EXTENSION bloom SCHEMA maintained")
    await dataset.admin.execute("CREATE TABLE maintained.analysed (id integer)")
    await dataset.admin.execute("CREATE INDEX analysed_custom ON maintained.analysed USING bloom (id)")
    await dataset.admin.execute(f"ALTER TABLE maintained.analysed OWNER TO {dataset.owner}")

    # The extension's operator class for it is outside pg_catalog too.
    assert await dataset.refusals() == {
        Refusal("indirection_access_method", None, dataset.owner, "maintained.analysed_custom"),
        Refusal("indirection_operator_class", None, dataset.owner, "maintained.analysed_custom"),
    }


@pytest.mark.parametrize("descent", ["partition", "inheritance grandchild"])
async def test_a_foreign_table_below_a_writers_relation_refuses(dataset: Dataset, descent: str) -> None:
    # ANALYZE of the writer's relation samples it through its wrapper, as the writer.
    await _maintained_schema(dataset)
    await dataset.admin.execute("CREATE FOREIGN DATA WRAPPER writer_check_wrapper")
    await dataset.admin.execute("CREATE SERVER writer_check_server FOREIGN DATA WRAPPER writer_check_wrapper")
    if descent == "partition":
        await dataset.admin.execute("CREATE TABLE maintained.parent (id integer) PARTITION BY RANGE (id)")
        await dataset.admin.execute(
            "CREATE FOREIGN TABLE maintained.remote PARTITION OF maintained.parent"
            " FOR VALUES FROM (0) TO (10) SERVER writer_check_server"
        )
    else:
        # The child between them is no writer's, so only the descent from the parent finds it.
        bystander = await dataset.role("NOLOGIN")
        await dataset.admin.execute("CREATE TABLE maintained.parent (id integer)")
        await dataset.admin.execute("CREATE TABLE maintained.child () INHERITS (maintained.parent)")
        await dataset.admin.execute(f"ALTER TABLE maintained.child OWNER TO {bystander}")
        await dataset.admin.execute(
            "CREATE FOREIGN TABLE maintained.remote () INHERITS (maintained.child) SERVER writer_check_server"
        )
    await dataset.admin.execute(f"ALTER TABLE maintained.parent OWNER TO {dataset.owner}")

    assert await dataset.refusals() == {
        Refusal("indirection_foreign_descendant", None, dataset.owner, "maintained.remote"),
    }


async def test_a_writers_plain_indexes_and_statistics_on_built_in_types_pass(dataset: Dataset) -> None:
    await _maintained_schema(dataset)
    await dataset.admin.execute("CREATE TABLE maintained.plain (id integer, label text, at timestamptz)")
    await dataset.admin.execute("CREATE INDEX ON maintained.plain (id)")
    await dataset.admin.execute("CREATE INDEX ON maintained.plain (label, at)")
    await dataset.admin.execute("CREATE STATISTICS maintained.plain_statistics (dependencies) ON id, label FROM maintained.plain")
    await dataset.admin.execute("CREATE TABLE maintained.parted (id integer) PARTITION BY RANGE (id)")
    await dataset.admin.execute("CREATE TABLE maintained.part PARTITION OF maintained.parted FOR VALUES FROM (0) TO (10)")
    for table in ("plain", "parted", "part"):
        await dataset.admin.execute(f"ALTER TABLE maintained.{table} OWNER TO {dataset.owner}")

    assert await dataset.refusals() == set()


async def test_a_writers_expression_and_partial_indexes_on_postgresqls_own_code_pass(dataset: Dataset) -> None:
    # As the app runtime's own tables have: predicates and expressions whose
    # functions and operators are all PostgreSQL's.
    await _maintained_schema(dataset)
    await dataset.admin.execute("CREATE TABLE maintained.analysed (id integer, label text)")
    await dataset.admin.execute("CREATE INDEX ON maintained.analysed (id) WHERE label IS NULL")
    await dataset.admin.execute("CREATE INDEX ON maintained.analysed (id) WHERE label IN ('a', 'b')")
    await dataset.admin.execute("CREATE INDEX ON maintained.analysed (lower(label))")
    # A partition's index attaches to its parent's, on which it depends.
    await dataset.admin.execute("CREATE TABLE maintained.parted (id integer) PARTITION BY RANGE (id)")
    await dataset.admin.execute("CREATE INDEX ON maintained.parted (id) WHERE id IS NOT NULL")
    await dataset.admin.execute("CREATE TABLE maintained.part PARTITION OF maintained.parted FOR VALUES FROM (0) TO (10)")
    for table in ("analysed", "parted", "part"):
        await dataset.admin.execute(f"ALTER TABLE maintained.{table} OWNER TO {dataset.owner}")

    assert await dataset.refusals() == set()


@pytest.mark.parametrize(("statements", "object"), [
    # An operator is recorded as itself, not as the function it calls.
    pytest.param(
        ["CREATE OPERATOR maintained.=== (leftarg = integer, rightarg = integer, function = int4eq)",
         "CREATE INDEX analysed_named ON maintained.analysed (id) WHERE id OPERATOR(maintained.===) 1"],
        "maintained.analysed_named", id="user operator in a predicate",
    ),
    # A regclass constant is recorded as the relation it names.
    pytest.param(
        ["CREATE TABLE maintained.other (id integer)",
         "CREATE INDEX analysed_named ON maintained.analysed (id) WHERE id::oid <> 'maintained.other'::regclass"],
        "maintained.analysed_named", id="another relation",
    ),
    # A relation in a system schema that PostgreSQL does not pin is recorded too.
    pytest.param(
        ["CREATE INDEX analysed_named ON maintained.analysed (id)"
         " WHERE id::oid <> 'information_schema.tables'::regclass"],
        "maintained.analysed_named", id="a system schema's relation",
    ),
])
async def test_a_writers_index_naming_anything_outside_postgresql_refuses(
    dataset: Dataset, statements: list[str], object: str,
) -> None:
    await _maintained_schema(dataset)
    await dataset.admin.execute("CREATE TABLE maintained.analysed (id integer)")
    for statement in statements:
        await dataset.admin.execute(statement)
    await dataset.admin.execute(f"ALTER TABLE maintained.analysed OWNER TO {dataset.owner}")

    assert await dataset.refusals() == {
        Refusal("indirection_index_expression", None, dataset.owner, object),
    }


async def test_a_writers_expression_statistics_refuse_even_on_postgresqls_own_code(dataset: Dataset) -> None:
    # PostgreSQL accepts a volatile statistics expression, and records no
    # dependency on a pinned core function, so no expression statistics pass.
    await _maintained_schema(dataset)
    await dataset.admin.execute("CREATE TABLE maintained.analysed (label text)")
    await dataset.admin.execute("CREATE STATISTICS maintained.lowered ON (lower(label)) FROM maintained.analysed")
    await dataset.admin.execute(f"ALTER TABLE maintained.analysed OWNER TO {dataset.owner}")

    assert await dataset.refusals() == {
        Refusal("indirection_statistics_expression", None, dataset.owner, "maintained.lowered"),
    }


@pytest.mark.parametrize(("statements", "column"), [
    pytest.param(["CREATE DOMAIN pg_catalog.writer_check_calm AS maintained.mood",
                  "CREATE TABLE maintained.analysed (mood pg_catalog.writer_check_calm)"],
                 "mood", id="a catalogue domain's base"),
    pytest.param(["CREATE DOMAIN pg_catalog.writer_check_calm AS maintained.mood",
                  "CREATE TABLE maintained.analysed (moods pg_catalog.writer_check_calm[])"],
                 "moods", id="a catalogue array's element"),
    pytest.param(["CREATE TYPE pg_catalog.writer_check_span AS RANGE (subtype = maintained.mood,"
                  " multirange_type_name = pg_catalog.writer_check_spans)",
                  "CREATE TABLE maintained.analysed (spans pg_catalog.writer_check_spans)"],
                 "spans", id="a catalogue multirange's range subtype"),
])
async def test_a_column_type_whose_chain_leaves_pg_catalog_refuses(
    dataset: Dataset, statements: list[str], column: str,
) -> None:
    # A superuser can put a type in pg_catalog over one from another schema.
    await _maintained_schema(dataset)
    await dataset.admin.execute("CREATE TYPE maintained.mood AS ENUM ('calm')")
    for statement in statements:
        await dataset.admin.execute(statement)
    await dataset.admin.execute(f"ALTER TABLE maintained.analysed OWNER TO {dataset.owner}")
    assert await dataset.admin.fetch_value(
        "SELECT typnamespace = 'pg_catalog'::regnamespace FROM pg_attribute JOIN pg_type ON pg_type.oid = atttypid"
        " WHERE attrelid = 'maintained.analysed'::regclass AND attname = $1", column,
    )

    assert await dataset.refusals() == {
        Refusal("indirection_column_type", None, dataset.owner, f"maintained.analysed.{column}"),
    }
