"""Library's real-stack lane: the lane's AWID with DNS verification, its
coordination server and three Library deployments from the production image
behind TLS, on one private network, with the session fixtures that start it
and the helpers that found teams in it.

conftest.py imports the fixtures, so every e2e module shares one lane.
"""

from __future__ import annotations

import json
import os
import secrets
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from aweb_naapp_testing.compose_lane import (
    DEFAULT_SUBNET,
    REGISTRY_ORIGIN,
    TEAM_SERVER_ORIGIN,
    ComposeLane,
)
from aweb_naapp_testing.lane_team import (
    LaneApp,
    LaneClient,
    LaneNamespace,
    LaneTeam,
    connect_team,
    found_team,
    install_app,
    prepare_namespace,
)

APP_DIR = Path(__file__).resolve().parents[1]
E2E_BUILD_SHA = "0123456789abcdef0123456789abcdef01234567"
# A deployment at a lane origin is not the hosted origin, so it needs its own app id.
E2E_APP_ID = "library-e2e"
# The lane's one namespace: every team the tests found is in it.
DOMAIN = "library.test"
PRE_STATUS_REGISTRY = "http://awid-pre-status.svc.test:8010"
POSTGRES_USER = "library"
POSTGRES_PASSWORD = "disposable"


@dataclass(frozen=True)
class Deployment:
    """One Library service on the lane: its Compose service, its own database,
    the registry it reads and whether it holds the AWID service token."""

    service: str
    database: str
    registry: str
    awid_service_token: bool

    @property
    def host(self) -> str:
        return f"{self.service}.svc.test"

    @property
    def origin(self) -> str:
        return f"https://{self.host}"


LIBRARY = Deployment("library", "library", REGISTRY_ORIGIN, awid_service_token=True)
WITHOUT_TOKEN = Deployment(
    "library-without-token", "library_without_token", REGISTRY_ORIGIN, awid_service_token=False
)
PRE_STATUS = Deployment(
    "library-pre-status", "library_pre_status", PRE_STATUS_REGISTRY, awid_service_token=False
)
DEPLOYMENTS = (LIBRARY, WITHOUT_TOKEN, PRE_STATUS)


def require_e2e_enabled() -> None:
    if os.environ.get("LIBRARY_E2E") != "1":
        pytest.skip("set LIBRARY_E2E=1 or run `make e2e` to execute docker-backed e2e tests")


def library_env(lane: ComposeLane, deployment: Deployment) -> dict[str, str]:
    """The deployment's own settings: its origin, its registry, its owner-only
    grant-read key, and the lane's coordination server as its operator's
    team server, reached through its egress guard inside the lane's network."""
    values = {
        # The lane's registry is plain http, which AWID's registry discovery
        # accepts only in development, as for the lane's own services.
        "APP_ENV": "development",
        "RENDER_GIT_COMMIT": E2E_BUILD_SHA,
        "LIBRARY_DATABASE_URL": (
            f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@postgres:5432/{deployment.database}"
        ),
        "LIBRARY_AWID_REGISTRY_URL": deployment.registry,
        "LIBRARY_AUTH_CACHE_TTL_SECONDS": "2",
        "LIBRARY_PUBLIC_ORIGIN": deployment.origin,
        "LIBRARY_APP_ID": E2E_APP_ID,
        "LIBRARY_APP_KEY_FILES": json.dumps(
            {"library:grant-read-1": f"/run/library-keys/{deployment.service}.hex"}
        ),
        "LIBRARY_HOOK_INTERNAL_DESTINATIONS": "allowed",
        "LIBRARY_HOOK_INTERNAL_CIDR": lane.subnet,
        "LIBRARY_TEAM_SERVER_INTERNAL_ORIGINS": json.dumps([TEAM_SERVER_ORIGIN]),
        "LIBRARY_HOOK_CA_FILE": "/run/library-ca/ca.pem",
    }
    if deployment.awid_service_token:
        values["LIBRARY_AWID_SERVICE_TOKEN"] = lane.env["NAAPP_E2E_AWID_SERVICE_TOKEN"]
    return values


def write_library_files(lane: ComposeLane) -> None:
    """Each deployment's env file and grant-read key. Compose checks every
    service's env file when any service starts, so they are written before
    the lane's first service."""
    keys = lane.runtime / "library-keys"
    keys.mkdir()
    keys.chmod(0o755)  # mkdir's mode is masked by the launching umask
    for deployment in DEPLOYMENTS:
        key = secrets.token_hex(32)
        lane.redact(key)
        (keys / f"{deployment.service}.hex").write_text(key)
        (keys / f"{deployment.service}.hex").chmod(0o644)
        (lane.runtime / f"{deployment.service}.env").write_text(
            "".join(f"{k}={v}\n" for k, v in library_env(lane, deployment).items())
        )


def create_databases(lane: ComposeLane) -> None:
    for deployment in DEPLOYMENTS:
        if deployment.database == POSTGRES_USER:
            continue
        lane.checked(
            lane.compose(
                "exec", "-T", "postgres", "psql", "-U", POSTGRES_USER, "-d", POSTGRES_USER,
                "-v", "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {deployment.database}",
            ),
            f"create {deployment.database}",
        )


@dataclass(frozen=True)
class LibraryLane:
    lane: ComposeLane
    namespace: LaneNamespace
    client: LaneClient

    def deployment(self, deployment: Deployment) -> LaneApp:
        return LaneApp(origin=deployment.origin, app_id=E2E_APP_ID, http=self.client)

    def requests_served(self) -> Counter[str]:
        """The requests every Library deployment has logged, apart from the
        image's own /live healthcheck, counted by log line. Compose interleaves
        the services' logs in no fixed order, so they are not a sequence."""
        logs = self.lane.checked(
            self.lane.compose(
                "logs", "--no-color", *(deployment.service for deployment in DEPLOYMENTS)
            ),
            "read Library's logs",
        )
        return Counter(
            line
            for line in logs.splitlines()
            if " HTTP/1.1\" " in line and '"GET /live HTTP/1.1"' not in line
        )


@pytest.fixture(scope="session")
def library_lane(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[LibraryLane]:
    require_e2e_enabled()
    released_aw = os.environ.get("NAAPP_AW_BINARY")
    assert released_aw, "set NAAPP_AW_BINARY to the pinned Linux aw release"
    lane = ComposeLane(
        os.environ.get("NAAPP_E2E_PROJECT", "library-e2e"),
        tmp_path_factory.mktemp("library-lane"),
        APP_DIR / "docker-compose.e2e.yml",
        env={
            "NAAPP_E2E_POSTGRES_USER": POSTGRES_USER,
            "NAAPP_E2E_POSTGRES_PASSWORD": POSTGRES_PASSWORD,
            "NAAPP_E2E_POSTGRES_DB": POSTGRES_USER,
        },
        subnet=DEFAULT_SUBNET,
    )
    lane.prepare(
        domain=DOMAIN,
        upstreams={deployment.host: f"{deployment.service}:8765" for deployment in DEPLOYMENTS},
        app_origins=[deployment.origin for deployment in DEPLOYMENTS],
        controller_did="did:key:z6MkpendingpendingpendingpendingpendingpendingP",
    )
    write_library_files(lane)
    try:
        lane.start_core()
        create_databases(lane)
        lane.up(*(deployment.service for deployment in DEPLOYMENTS))
        lane.checked(lane.compose("build", "cli"), "build the lane CLI")
        machine = lane.machine("operator", aw_binary=Path(released_aw))
        namespace = prepare_namespace(lane, machine, home="operator", domain=DOMAIN)
        yield LibraryLane(lane=lane, namespace=namespace, client=LaneClient(lane, machine))
    finally:
        logs = lane.close()
        if request.session.testsfailed:
            reporter = request.config.pluginmanager.get_plugin("terminalreporter")
            reporter.write_sep("-", "library lane Compose")
            reporter.write_line(logs)


@pytest.fixture(scope="session")
def library(library_lane: LibraryLane) -> LaneApp:
    return library_lane.deployment(LIBRARY)


@pytest.fixture(scope="session")
def library_without_awid_service_token(library_lane: LibraryLane) -> LaneApp:
    return library_lane.deployment(WITHOUT_TOKEN)


@pytest.fixture(scope="session")
def library_on_pre_status_registry(library_lane: LibraryLane) -> LaneApp:
    return library_lane.deployment(PRE_STATUS)


@pytest.fixture(scope="session")
def namespace(library_lane: LibraryLane) -> LaneNamespace:
    return library_lane.namespace


def connected_team(namespace: LaneNamespace) -> LaneTeam:
    """A new team whose server is the lane's, with Library not installed."""
    team = found_team(namespace)
    connect_team(team)
    return team


def installed_team(
    namespace: LaneNamespace, library: LaneApp, *, grant_scopes: tuple[str, ...] = ()
) -> LaneTeam:
    """A new team that installed Library; ``grant_scopes`` narrows its grant."""
    team = connected_team(namespace)
    install_app(team, library, grant_scopes=grant_scopes)
    return team
