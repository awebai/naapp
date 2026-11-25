from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from aweb_naapp_testing.database_fixtures import database_template

from tests.console.support import Console, console_world, migrate_console_world
from tests.runtime.credential_audit.support import world  # noqa: F401


@pytest_asyncio.fixture(scope="session")
async def console_template():
    async with database_template(
        migrate_console_world, suffix="console_template", schema="app"
    ) as template:
        yield template


@pytest.fixture
async def console_database(clone_database, console_template):
    return await clone_database(console_template, suffix="naapp_console", schema="app")


@pytest.fixture
async def console(console_database, registry) -> AsyncIterator[Console]:
    yield await console_world(console_database, registry)
