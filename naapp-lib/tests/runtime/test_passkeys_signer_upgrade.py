"""Real pgdbm upgrade of the passkeys schema past the retired signer, including
a configurable schema the signer used."""

from collections.abc import AsyncIterator
from pathlib import Path
from shutil import copyfile

import pytest
import pytest_asyncio
from asyncpg.pool import Pool
from pgdbm import AsyncDatabaseManager, AsyncMigrationManager, DatabaseConfig

ROOT = Path(__file__).resolve().parents[3]


@pytest_asyncio.fixture
async def shared_test_pool(test_db_factory) -> AsyncIterator[Pool]:
    """One pool on a fresh test database, shared by managers of several schemas."""
    database = await test_db_factory.create_db(suffix="passkeys_upgrade")
    # The pool is the database's alone; each manager on it names its own schema.
    pool = await AsyncDatabaseManager.create_shared_pool(
        DatabaseConfig(connection_string=database.config.get_dsn())
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_schema", ["protected_signer", "custom_passkeys"])
async def test_upgrade_discards_only_signer_proof_contexts(
    shared_test_pool, tmp_path, legacy_schema
):
    old = tmp_path / "old_passkeys"
    old.mkdir()
    migrations = ROOT / "naapp-lib/src/aweb_naapp/runtime/passkeys/migrations"
    for path in migrations.glob("*.sql"):
        if int(path.name.split("_", 1)[0]) <= 6:
            copyfile(path, old / path.name)
    databases = []
    for schema in ("aweb", legacy_schema):
        db = AsyncDatabaseManager(pool=shared_test_pool, schema=schema)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        await AsyncMigrationManager(
            db, migrations_path=str(old), module_name="aweb_naapp_passkeys"
        ).apply_pending_migrations()
        await db.execute(
            "INSERT INTO {{tables.passkey_approvers}} "
            "(subject,scope,contact,designated_by,designated_at,effective_at) "
            "VALUES ('subject','scope','contact','owner',NOW(),NOW())"
        )
        await db.execute(
            "INSERT INTO {{tables.passkey_credentials}} "
            "(credential_id,subject,public_key,sign_count,created_at) "
            "VALUES ($1,'subject',$2,1,NOW())",
            b"credential",
            b"public key",
        )
        for evidence in ("ordinary", "signer"):
            await db.execute(
                "INSERT INTO {{tables.passkey_assertions}} "
                "(evidence_ref,subject,credential_id,purpose,sign_count,verified_at) "
                "VALUES ($1,'subject',$2,'login',1,NOW())",
                evidence,
                b"credential",
            )
        await db.execute(
            "INSERT INTO {{tables.passkey_proofs}} VALUES "
            "('ordinary','app','team','installation',1,$1,'ordinary commitment',NULL), "
            "('signer',NULL,NULL,NULL,1,$2,'signer commitment',$3)",
            b"ordinary retained bytes",
            b"obsolete signer bytes",
            b"signer context",
        )
        databases.append(db)
    # Upgrading one active app also reaches the signer's configurable schema.
    runner = AsyncMigrationManager(
        databases[0], migrations_path=str(migrations), module_name="aweb_naapp_passkeys"
    )
    await runner.apply_pending_migrations()
    for db in databases:
        assert (
            await db.fetch_value("SELECT record FROM {{tables.passkey_proofs}}")
            == b"ordinary retained bytes"
        )
        assert (
            await db.fetch_value("SELECT commitment FROM {{tables.passkey_proofs}}")
            == "ordinary commitment"
        )
        assert (
            await db.fetch_value("SELECT count(*) FROM {{tables.passkey_proofs}}") == 1
        )
        columns = await db.fetch_all(
            "SELECT column_name,is_nullable FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='passkey_proofs'",
            db.schema,
        )
        shape = {row["column_name"]: row["is_nullable"] for row in columns}
        assert "authority_context" not in shape
        assert all(
            shape[name] == "NO" for name in ("app_id", "team_id", "installation_id")
        )
        assert not await db.fetch_value(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conrelid=to_regclass($1) AND conname='passkey_proof_context_shape')",
            f"{db.schema}.passkey_proofs",
        )
    await databases[0].execute((migrations / "007_drop_retired_signer.sql").read_text())
    assert (await runner.apply_pending_migrations())["status"] == "up_to_date"
