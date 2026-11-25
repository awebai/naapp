"""aweb server/tests/real_stack.py at 2788969b6fce509437eab53637927bb97cf18b58, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from aweb.routes.app_hooks import router as app_hooks_router
from aweb.routes.apps import router as apps_router
from aweb_naapp.runtime.operator_egress import OperatorNetworkSettings
from aweb_naapp.runtime.team_auth import AWIDTeamCache
from aweb_naapp.runtime.team_server import TeamServerDirectory, TeamServerReach
from aweb_naapp_testing.team_server_site import TeamServerSite, team_server_site
from awid.did import did_from_public_key
from awid.ratelimit import NoOpRateLimiter
from awid.signing import sign_message
from awid.team_server_record import record_signing_bytes
from awid_service.db import AwidDatabaseInfra
from awid_service.main import mount_registry
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from fastapi import FastAPI
from nacl.signing import SigningKey
from redis.asyncio import Redis
import asyncio
import httpx
import os
import pytest_asyncio
from tests.server_compat.aweb_server_tests.app_events_http import _build_events_app, _seed_team_and_agent

# Copied from an aweb commit, not a release: replaced by the re-copy
# from the first server tag that carries this change.
AWEB_REF = '2788969b6fce509437eab53637927bb97cf18b58'
AWEB_PATH = 'server/tests/real_stack.py'
AWEB_BLOB = '143c18a5bee900b3876b6baa43b3840526c0be9d'
COPIED = {
    'REGISTRY_URL': '2078c4cc7a32370057f247fc3898fd819aa5747b2d35f1b285e0eb0e7148978c',
    'MOVED_NAME': '6c1eb0d5972b1ad08490c872a803166267e73efd1e78ba9ddbfb59871eb6185d',
    'ResponseFault': 'eea1fa0b31e14ae2b2119904407301f4372e8898e9fac90b19f94e7f1ac70fee',
    'ServiceTransport': '44f2546ce62582b5cd58391ed7d677e4a47e06a4e96c2dd17029aa98b2b09e29',
    'RealStack': 'b06b4bd9a884b3cee7bcf55d6436415b3d88911d35970f4bc19dce5a0fdef31d',
    'real_stack': '4a0eab92cdeefc6477064565ec4b015b3e4061c4f79536eb86962f6038b97e28',
}


REGISTRY_URL = "https://registry.example"


MOVED_NAME = "aweb.moved.example"


ResponseFault = Callable[[httpx.Request, httpx.Response], httpx.Response | None]


class ServiceTransport(httpx.AsyncBaseTransport):
    """Delivers each request to a real in-process service and records it."""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.calls: list[httpx.Request] = []
        self.fault: ResponseFault | None = None
        self.down = False
        self.delay = 0.0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectError("service unreachable", request=request)
        if self.delay:
            await asyncio.sleep(self.delay)
        response = await self._inner.handle_async_request(request)
        if self.fault is None:
            return response
        await response.aread()
        replacement = self.fault(request, response)
        return response if replacement is None else replacement


@dataclass
class RealStack:
    aweb_db: object
    awid_db: object
    awid: ServiceTransport
    aweb: ServiceTransport
    service_token: str
    # Where aweb listens: TLS on this host's private address, behind which
    # ``aweb`` records and faults every request as before.
    site: TeamServerSite

    @property
    def aweb_url(self) -> str:
        return self.site.origin

    @property
    def moved_url(self) -> str:
        return f"https://{MOVED_NAME}:{self.site.tls.port}"

    async def add_team(self, team_id: str) -> SigningKey:
        """Create the team in AWID and in aweb; returns its team key."""
        team_key = SigningKey.generate()
        name, domain = team_id.split(":", 1)
        await self.awid_db.execute(
            "INSERT INTO {{tables.teams}} (domain, name, team_did_key, visibility) VALUES ($1, $2, $3, 'public')",
            domain,
            name,
            did_from_public_key(bytes(team_key.verify_key)),
        )
        await _seed_team_and_agent(self.aweb_db, team_id=team_id)
        return team_key

    async def publish_server(
        self,
        team_id: str,
        team_key: SigningKey,
        *,
        server_url: str | None = None,
        issued_at: datetime | None = None,
    ) -> None:
        """Publish the team's aweb.team-server/1 record through AWID's own route."""
        name, domain = team_id.split(":", 1)
        record = {
            "v": 1,
            "type": "aweb.team-server",
            "team_id": team_id,
            "team_did_key": did_from_public_key(bytes(team_key.verify_key)),
            "server_url": server_url or self.aweb_url,
            "issued_at": (issued_at or datetime.now(timezone.utc)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        body = {
            "record": record,
            "signature": sign_message(bytes(team_key), record_signing_bytes(record)),
        }
        async with httpx.AsyncClient(transport=self.awid) as client:
            response = await client.put(
                f"{REGISTRY_URL}/v1/namespaces/{domain}/teams/{name}/server", json=body
            )
        response.raise_for_status()

    def directory(
        self,
        *,
        clock: Callable[[], float] | None = None,
        ttl_seconds: int | None = None,
    ) -> TeamServerDirectory:
        """Team-server discovery against the stack's AWID."""

        async def no_registry_override(domain: str) -> str | None:
            return None

        team_cache = AWIDTeamCache(
            registry_url=REGISTRY_URL,
            ttl_seconds=60,
            service_token=self.service_token,
            transport=self.awid,
        )
        options: dict = {} if clock is None else {"clock": clock}
        if ttl_seconds is not None:
            options["ttl_seconds"] = ttl_seconds
        return TeamServerDirectory(
            team_cache=team_cache,
            transport=self.awid,
            registry_override=no_registry_override,
            **options,
        )

    def team_server_reach(
        self, *, internal_origins: frozenset[str] | None = None
    ) -> TeamServerReach:
        """Reads of the stack's aweb through the app's egress guard: the
        operator's own servers (by default the stack's aweb), listed as such
        inside a network of exactly the site's address."""
        settings = OperatorNetworkSettings(
            **{
                **self.site.settings(),
                "team_server_internal_origins": internal_origins
                if internal_origins is not None
                else frozenset({self.aweb_url}),
            }
        )
        egress = self.site.egress(settings)
        return TeamServerReach(egress=egress.egress, internal=egress.team_servers)


@pytest_asyncio.fixture
async def real_stack(
    aweb_cloud_db, test_db_factory, monkeypatch, tmp_path_factory
) -> AsyncIterator[RealStack]:
    service_token = os.environ["AWID_SERVICE_TOKEN"]
    awid_database = await test_db_factory.create_db(suffix="aweb_real_stack_awid")
    infra = AwidDatabaseInfra(schema="awid")
    monkeypatch.setenv("AWID_DATABASE_URL", awid_database.config.get_dsn())
    await infra.initialize(run_migrations=True)
    awid_app = FastAPI()
    mount_registry(awid_app)
    registry_redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    awid_app.state.redis = registry_redis
    awid_app.state.db = infra
    awid_app.state.rate_limiter = NoOpRateLimiter()
    awid_app.state.awid_service_token = service_token
    # Its public origin, which covering controller signatures name.
    awid_app.state.awid_public_origins = (REGISTRY_URL,)

    try:
        async with team_server_site(
            tmp_path_factory.mktemp("real-stack-aweb"), also_names=(MOVED_NAME,)
        ) as site:
            aweb_app = _build_events_app(aweb_cloud_db.aweb_db, public_origin=site.origin)
            aweb_app.include_router(apps_router)
            aweb_app.include_router(app_hooks_router)
            aweb = ServiceTransport(aweb_app)
            site.serve_transport(aweb)
            yield RealStack(
                aweb_db=aweb_cloud_db.aweb_db,
                awid_db=infra.get_manager("aweb"),
                awid=ServiceTransport(awid_app),
                aweb=aweb,
                service_token=service_token,
                site=site,
            )
    finally:
        await registry_redis.aclose()
        await infra.close()
