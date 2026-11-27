"""Real PostgreSQL databases for the Tasks tests (pgdbm's test_db_factory), each
in its own schema, and the real AWID and aweb server from the pinned release."""

from typing import Any

import pytest
from aweb_naapp_testing.databases import SchemaDatabases

pytest_plugins = (
    "pgdbm.fixtures.conftest",
    "aweb_naapp_testing.server",
    "aweb_naapp_testing.database_fixtures",
)


@pytest.fixture
async def test_db_factory(test_db_factory: Any) -> SchemaDatabases:
    return SchemaDatabases(test_db_factory)
