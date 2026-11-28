"""Folio's real-stack lane: the lane's AWID with DNS verification, its
coordination server, Folio deployments from the production image behind TLS
and a Chromium on the lane's network, with the session fixtures that start them
and the helper that founds a team in the lane's namespace.

conftest.py imports the fixtures, so every e2e module shares one lane.
"""

from __future__ import annotations

import json
import os
import secrets
import time
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
# The lane's one namespace: every team the tests found is in it.
DOMAIN = "folio.test"
POSTGRES_USER = "folio"
POSTGRES_PASSWORD = "disposable"
# A second name the front routes to the folio deployment: a request signed for
# it is signed for an audience that is not Folio's public origin.
BACKEND_HOST = "folio-backend.svc.test"
BACKEND_ORIGIN = f"https://{BACKEND_HOST}"


@dataclass(frozen=True)
class Deployment:
    """One Folio service on the lane: its Compose service, its own database,
    its app id and whether it holds the AWID service token."""

    service: str
    database: str
    app_id: str
    awid_service_token: bool

    @property
    def host(self) -> str:
        return f"{self.service}.svc.test"

    @property
    def origin(self) -> str:
        return f"https://{self.host}"


# A deployment at a lane origin is not the hosted origin, so it needs its own app id.
FOLIO = Deployment("folio", "folio", "folio-e2e", awid_service_token=True)
WITHOUT_TOKEN = Deployment(
    "folio-without-token", "folio_without_token", "folio-e2e", awid_service_token=False
)
# The deployment a resident's grant installs, with an app id of its own.
INSTALL = Deployment("folio-install", "folio_install", "folio-install", awid_service_token=True)
DEPLOYMENTS = (FOLIO, WITHOUT_TOKEN, INSTALL)


def require_e2e_enabled() -> None:
    if os.environ.get("FOLIO_E2E") != "1":
        pytest.skip("set FOLIO_E2E=1 or run `make e2e` to execute docker-backed e2e tests")


def folio_env(lane: ComposeLane, deployment: Deployment) -> dict[str, str]:
    """The deployment's own settings: its origin, the lane's registry, its
    owner-only app keys, and the lane's coordination server as its operator's
    team server, reached through its egress guard inside the lane's network."""
    values = {
        # The lane's registry is plain http, which AWID's registry discovery
        # accepts only in development, as for the lane's own services.
        "APP_ENV": "development",
        "RENDER_GIT_COMMIT": E2E_BUILD_SHA,
        "FOLIO_DATABASE_URL": (
            f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@postgres:5432/{deployment.database}"
        ),
        "FOLIO_AWID_REGISTRY_URL": REGISTRY_ORIGIN,
        "FOLIO_AUTH_CACHE_TTL_SECONDS": "2",
        "FOLIO_PUBLIC_ORIGIN": deployment.origin,
        "FOLIO_APP_ID": deployment.app_id,
        "FOLIO_APP_KEY_FILES": json.dumps(
            {
                "folio:emit-1": f"/run/folio-keys/{deployment.service}-emit.hex",
                "folio:grant-read-1": f"/run/folio-keys/{deployment.service}-grant-read.hex",
            }
        ),
        "FOLIO_HOOK_INTERNAL_DESTINATIONS": "allowed",
        "FOLIO_HOOK_INTERNAL_CIDR": lane.subnet,
        "FOLIO_TEAM_SERVER_INTERNAL_ORIGINS": json.dumps([TEAM_SERVER_ORIGIN]),
        "FOLIO_HOOK_CA_FILE": "/run/folio-ca/ca.pem",
    }
    if deployment.awid_service_token:
        values["FOLIO_AWID_SERVICE_TOKEN"] = lane.env["NAAPP_E2E_AWID_SERVICE_TOKEN"]
    return values


def write_folio_files(lane: ComposeLane) -> None:
    """Each deployment's env file and app keys. Compose checks every service's
    env file when any service starts, so they are written before the lane's
    first service."""
    keys = lane.runtime / "folio-keys"
    keys.mkdir()
    keys.chmod(0o755)  # mkdir's mode is masked by the launching umask
    for deployment in DEPLOYMENTS:
        for purpose in ("emit", "grant-read"):
            key = secrets.token_hex(32)
            lane.redact(key)
            path = keys / f"{deployment.service}-{purpose}.hex"
            path.write_text(key)
            path.chmod(0o644)
        (lane.runtime / f"{deployment.service}.env").write_text(
            "".join(f"{k}={v}\n" for k, v in folio_env(lane, deployment).items())
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


def browser_endpoint(lane: ComposeLane) -> str:
    """The Playwright server's address on the runner, once it accepts connections."""
    published = lane.checked(lane.compose("port", "playwright", "3000"), "find Playwright")
    endpoint = f"ws://{published}/"
    from playwright.sync_api import Error, sync_playwright

    deadline = time.monotonic() + 60
    with sync_playwright() as playwright:
        while True:
            try:
                playwright.chromium.connect(endpoint, timeout=5000).close()
                return endpoint
            except Error:
                assert time.monotonic() < deadline, f"Playwright never answered at {endpoint}"
                time.sleep(1.0)


@dataclass(frozen=True)
class FolioLane:
    lane: ComposeLane
    namespace: LaneNamespace
    client: LaneClient
    browser: str

    def deployment(self, deployment: Deployment) -> LaneApp:
        return LaneApp(origin=deployment.origin, app_id=deployment.app_id, http=self.client)


@pytest.fixture(scope="session")
def folio_lane(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[FolioLane]:
    require_e2e_enabled()
    released_aw = os.environ.get("NAAPP_AW_BINARY")
    assert released_aw, "set NAAPP_AW_BINARY to the pinned Linux aw release"
    lane = ComposeLane(
        os.environ.get("NAAPP_E2E_PROJECT", "folio-e2e"),
        tmp_path_factory.mktemp("folio-lane"),
        APP_DIR / "docker-compose.e2e.yml",
        env={
            "NAAPP_E2E_POSTGRES_USER": POSTGRES_USER,
            "NAAPP_E2E_POSTGRES_PASSWORD": POSTGRES_PASSWORD,
            "NAAPP_E2E_POSTGRES_DB": POSTGRES_USER,
        },
        subnet=DEFAULT_SUBNET,
    )
    upstreams = {deployment.host: f"{deployment.service}:8765" for deployment in DEPLOYMENTS}
    lane.prepare(
        domain=DOMAIN,
        upstreams={**upstreams, BACKEND_HOST: f"{FOLIO.service}:8765"},
        app_origins=[deployment.origin for deployment in DEPLOYMENTS],
        controller_did="did:key:z6MkpendingpendingpendingpendingpendingpendingP",
    )
    write_folio_files(lane)
    try:
        lane.start_core()
        create_databases(lane)
        lane.up(*(deployment.service for deployment in DEPLOYMENTS), "playwright")
        lane.checked(lane.compose("build", "cli"), "build the lane CLI")
        machine = lane.machine("operator", aw_binary=Path(released_aw))
        namespace = prepare_namespace(lane, machine, home="operator", domain=DOMAIN)
        yield FolioLane(
            lane=lane,
            namespace=namespace,
            client=LaneClient(lane, machine),
            browser=browser_endpoint(lane),
        )
    finally:
        logs = lane.close()
        if request.session.testsfailed:
            reporter = request.config.pluginmanager.get_plugin("terminalreporter")
            reporter.write_sep("-", "folio lane Compose")
            reporter.write_line(logs)


@pytest.fixture(scope="session")
def folio(folio_lane: FolioLane) -> LaneApp:
    return folio_lane.deployment(FOLIO)


@pytest.fixture(scope="session")
def folio_without_awid_service_token(folio_lane: FolioLane) -> LaneApp:
    return folio_lane.deployment(WITHOUT_TOKEN)


@pytest.fixture(scope="session")
def folio_install(folio_lane: FolioLane) -> LaneApp:
    return folio_lane.deployment(INSTALL)


@pytest.fixture(scope="session")
def namespace(folio_lane: FolioLane) -> LaneNamespace:
    return folio_lane.namespace


@pytest.fixture(scope="session")
def lane_browser(folio_lane: FolioLane) -> str:
    """Where ``playwright.chromium.connect`` reaches the lane's Chromium."""
    return folio_lane.browser


def provision_team(
    namespace: LaneNamespace, *, alias: str = "alice", folio: LaneApp | None = None
) -> LaneTeam:
    """A new team with ``alias`` as its member; with ``folio``, the team also
    connects to the lane's coordination server and installs that deployment."""
    team = found_team(namespace, alias=alias)
    if folio is not None:
        connect_team(team)
        install_app(team, folio)
    return team
