"""Real PostgreSQL databases for the tests (pgdbm's test_db_factory), each in
its own schema, and real AWID routes on one of them."""

from collections.abc import AsyncIterator
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
