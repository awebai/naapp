"""The aweb server's own fixtures for the suite: its test database and schema, the
real AWID-and-aweb stack, and the app task request context."""

import importlib
import sys

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

__all__ = [
    "app_request_context",
    "aweb_cloud_db",
    "privacy_app",
    "real_stack",
    "shared_test_database",
    "shared_test_pool",
]
