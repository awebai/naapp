"""Real PostgreSQL databases for the tests (pgdbm's test_db_factory), each in
its own schema, and real AWID routes on one of them."""

from collections.abc import AsyncIterator
from functools import partial
from typing import Any

import pytest
import pytest_asyncio
from aweb_naapp_testing.databases import SchemaDatabases
from awid_service.testing import Registry, running_registry

pytest_plugins = (
    "pgdbm.fixtures.conftest",
    "aweb_naapp_testing.database_fixtures",
)


@pytest.fixture
def human_member_cache_seconds() -> int:
    return 60


@pytest.fixture
def root_broker_network():
    return None


@pytest.fixture
async def test_db_factory(test_db_factory: Any) -> SchemaDatabases:
    return SchemaDatabases(test_db_factory)


@pytest_asyncio.fixture
async def registry(clone_database, awid_template) -> AsyncIterator[Registry]:
    registry_db = await clone_database(awid_template, suffix="secrets_awid")
    async with running_registry(registry_db.config.get_dsn()) as running:
        yield running


@pytest.fixture
def console_tls(tmp_path, monkeypatch):
    """Private CA for the real local HTTPS console and its browser clients."""
    from aweb_naapp_testing.network import make_certificates

    certificates = make_certificates(tmp_path, ["localhost"], ["127.0.0.1"])
    monkeypatch.setenv("SSL_CERT_FILE", str(certificates.ca_file))
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(certificates.ca_file))
    return certificates


@pytest.fixture
def passkey_origin():
    return "https://secrets.example.com"


@pytest.fixture
def console_listener(console_tls):
    import socket
    from types import SimpleNamespace

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        yield SimpleNamespace(socket=sock, port=port, origin=f"https://localhost:{port}")
    finally:
        sock.close()


@pytest_asyncio.fixture(scope="session")
async def change_set_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from tests.test_change_sets import migrate_change_set_world

    async with database_template(
        migrate_change_set_world, suffix="change_sets_template"
    ) as template:
        yield template


@pytest.fixture
async def change_set_databases(clone_database, change_set_template, awid_template):
    db = await clone_database(change_set_template, suffix="secrets_change_sets")
    registry_db = await clone_database(awid_template, suffix="secrets_change_sets_awid")
    return db, registry_db


@pytest_asyncio.fixture(scope="session")
async def owner_templates():
    from aweb_naapp_testing.database_fixtures import database_template

    from tests.test_owner_bootstrap import migrate_owner_world

    async with (
        database_template(migrate_owner_world, suffix="owner_template") as current,
        database_template(
            partial(migrate_owner_world, owner_history=True), suffix="owner_history_template"
        ) as historical,
    ):
        yield current, historical


@pytest_asyncio.fixture(scope="session")
async def authority_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from tests.production_authority import migrate_authority

    async with database_template(migrate_authority, suffix="authority_template") as template:
        yield template


@pytest.fixture
async def authority_database(clone_database, authority_template):
    return await clone_database(authority_template, suffix="root_external_aweb")


@pytest_asyncio.fixture(scope="session")
async def production_template(tmp_path_factory):
    from aweb_naapp_testing.database_fixtures import database_template
    from aweb_naapp_testing.smtp import SmtpServer

    from aweb_secrets.composition import create_service
    from tests.test_composition import settings

    async def migrate(db):
        directory = tmp_path_factory.mktemp("secrets-template")
        with SmtpServer(directory / "smtp") as smtp:
            service = create_service(settings(db, directory, smtp))
            async with service.api_app.router.lifespan_context(service.api_app):
                pass

    async with database_template(migrate, suffix="secrets_production_template") as template:
        yield template


@pytest.fixture
async def production_database(clone_database, production_template):
    return await clone_database(production_template, suffix="secrets_production")


@pytest_asyncio.fixture(scope="session")
async def admission_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from tests.test_admission import migrate_admission_database

    async with database_template(
        migrate_admission_database, suffix="admission_template"
    ) as template:
        yield template


@pytest_asyncio.fixture(scope="session")
async def records_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from tests.test_records_store import migrate_records_database

    async with database_template(migrate_records_database, suffix="records_template") as template:
        yield template


@pytest_asyncio.fixture(scope="session")
async def app_template(tmp_path_factory):
    from aweb_naapp_testing.database_fixtures import database_template

    from aweb_secrets.app import create_app
    from tests.test_app import InstalledTeams, _settings, allow_all, kms_check

    async def migrate(db):
        settings = _settings(db.config.get_dsn(), tmp_path_factory.mktemp("secrets-api-template"))
        service = create_app(
            settings, grants=InstalledTeams({}), policy=allow_all, kms=kms_check(True)
        )
        async with service.api_app.router.lifespan_context(service.api_app):
            pass

    async with database_template(migrate, suffix="secrets_api_template") as template:
        yield template


@pytest_asyncio.fixture(scope="session")
async def jobs_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from aweb_secrets.jobs import apply_job_migrations

    async with database_template(apply_job_migrations, suffix="jobs_template") as template:
        yield template


@pytest.fixture
async def jobs_database(clone_database, jobs_template):
    return await clone_database(jobs_template, suffix="jobs")


@pytest_asyncio.fixture(scope="session")
async def runners_template():
    from aweb_naapp_testing.database_fixtures import database_template

    from aweb_secrets.runners import apply_runner_migrations

    async with database_template(apply_runner_migrations, suffix="runners_template") as template:
        yield template


@pytest.fixture
async def runners_database(clone_database, runners_template):
    return await clone_database(runners_template, suffix="runners")
