"""The aweb server's own fixtures for the suite: its test database and schema, the
real AWID-and-aweb stack, and the app task request context."""

import importlib
import os
import sys

import pytest
import redis

from tests.server_compat.aweb_server_tests import AWEB_MODULE_NAMES
from tests.server_compat.aweb_server_tests.app_task_requests import app_request_context
from tests.server_compat.aweb_server_tests.dashboard_privacy import privacy_app
from tests.server_compat.aweb_server_tests.real_stack import real_stack
from tests.server_compat.aweb_server_tests.server_conftest import (
    aweb_cloud_db,
    shared_test_database,
    shared_test_pool,
)

# Copied definitions import some of aweb's test modules by aweb's own names.
for aweb_name, copy in AWEB_MODULE_NAMES.items():
    sys.modules.setdefault(
        aweb_name, importlib.import_module(f"tests.server_compat.aweb_server_tests.{copy}")
    )

@pytest.fixture(scope="session", autouse=True)
def redis_left_as_found():
    """Delete the Redis keys the suite added, such as the presence transitions the
    copied stack records, so a run leaves the database at REDIS_URL as it found it.
    The suites run on a Redis database of their own: a key another process adds
    there during the run is deleted too."""
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    before = set(client.scan_iter())
    yield
    added = set(client.scan_iter()) - before
    if added:
        client.delete(*added)
    left = set(client.scan_iter())
    client.close()
    assert left == before, f"the suite left Redis keys behind: {sorted(left - before)}"


__all__ = [
    "app_request_context",
    "aweb_cloud_db",
    "privacy_app",
    "real_stack",
    "shared_test_database",
    "shared_test_pool",
]
