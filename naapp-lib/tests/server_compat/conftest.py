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
from tests.server_compat.aweb_server_tests.server_conftest import (
    ENVIRONMENT_DEFAULTS,
    aweb_cloud_db,
    shared_test_database,
    shared_test_pool,
)
from tests.server_compat.real_stack import real_stack

# Copied definitions import some of aweb's test modules by aweb's own names.
for aweb_name, copy in AWEB_MODULE_NAMES.items():
    sys.modules.setdefault(
        aweb_name, importlib.import_module(f"tests.server_compat.aweb_server_tests.{copy}")
    )

@pytest.fixture(scope="package", autouse=True)
def aweb_test_environment():
    """aweb's server-test environment defaults, set only while this suite's tests
    run and restored afterwards, so no other test in the process sees them."""
    previous = {name: os.environ.get(name) for name in ENVIRONMENT_DEFAULTS}
    for name, value in ENVIRONMENT_DEFAULTS.items():
        os.environ.setdefault(name, value)
    yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# The Redis key families the aweb server's presence code writes (aweb/presence.py,
# aweb/presence_transitions.py), which a run that starts the server can leave.
SUITE_KEY_PATTERNS = ("presence:*", "presence_coordinates:*", "presence-transitions:*")


@pytest.fixture(scope="session", autouse=True)
def redis_left_as_found():
    """Delete the Redis keys of the kinds the aweb server's presence code writes
    that appeared during the run, and fail if any remain. Other keys are left
    alone, so a run never removes another process's keys."""
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("set REDIS_URL to a Redis database for this run's own keys")
    client = redis.Redis.from_url(url)

    def suite_keys():
        return {key for pattern in SUITE_KEY_PATTERNS for key in client.scan_iter(match=pattern)}

    before = suite_keys()
    yield
    added = suite_keys() - before
    if added:
        client.delete(*added)
    left = suite_keys() - before
    client.close()
    assert not left, f"the suite left Redis keys behind: {sorted(left)}"


__all__ = [
    "app_request_context",
    "aweb_cloud_db",
    "privacy_app",
    "real_stack",
    "shared_test_database",
    "shared_test_pool",
]
