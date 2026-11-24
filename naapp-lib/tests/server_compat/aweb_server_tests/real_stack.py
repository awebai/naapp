"""aweb server/tests/real_stack.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from aweb.routes.app_hooks import router as app_hooks_router
from aweb.routes.apps import router as apps_router
from aweb_naapp.runtime.team_auth import AWIDTeamCache
from aweb_naapp.runtime.team_server import TeamServerDirectory
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

AWEB_TAG = 'server-v1.27.53'
AWEB_PATH = 'server/tests/real_stack.py'
AWEB_BLOB = '19ce339871770f3d9754f2937ed927f2a4bb9991'
COPIED = {
    'REGISTRY_URL': '2078c4cc7a32370057f247fc3898fd819aa5747b2d35f1b285e0eb0e7148978c',
    'AWEB_URL': '6c467561ef7a12d33e135465dd69db37076e410d5cc85f069b8273e11e46cc22',
    'ResponseFault': 'eea1fa0b31e14ae2b2119904407301f4372e8898e9fac90b19f94e7f1ac70fee',
    'ServiceTransport': '44f2546ce62582b5cd58391ed7d677e4a47e06a4e96c2dd17029aa98b2b09e29',
    'RealStack': 'c63abe2c47a11d86575bf0e5ed21e9004fce17bf876dd2552f2b8c7abddb1a37',
    'real_stack': '84d9a6038f125650d28c64201135ead0ac51ba8dc02cf8176515bf2d55b6ca00',
}


REGISTRY_URL = "https://registry.example"


AWEB_URL = "https://aweb.example"


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
        self, team_id: str, team_key: SigningKey, *, server_url: str = AWEB_URL, issued_at: datetime | None = None
    ) -> None:
        """Publish the team's aweb.team-server/1 record through AWID's own route."""
        name, domain = team_id.split(":", 1)
        record = {
            "v": 1,
            "type": "aweb.team-server",
            "team_id": team_id,
            "team_did_key": did_from_public_key(bytes(team_key.verify_key)),
            "server_url": server_url,
            "issued_at": (issued_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        body = {"record": record, "signature": sign_message(bytes(team_key), record_signing_bytes(record))}
        async with httpx.AsyncClient(transport=self.awid) as client:
            response = await client.put(f"{REGISTRY_URL}/v1/namespaces/{domain}/teams/{name}/server", json=body)
        response.raise_for_status()

    def directory(
        self, *, clock: Callable[[], float] | None = None, ttl_seconds: int | None = None
    ) -> TeamServerDirectory:
        """Team-server discovery against the stack's AWID."""

        async def no_registry_override(domain: str) -> str | None:
            return None

        team_cache = AWIDTeamCache(
            registry_url=REGISTRY_URL, ttl_seconds=60, service_token=self.service_token, transport=self.awid
        )
        options: dict = {} if clock is None else {"clock": clock}
        if ttl_seconds is not None:
            options["ttl_seconds"] = ttl_seconds
        return TeamServerDirectory(
            team_cache=team_cache, transport=self.awid, registry_override=no_registry_override, **options
        )


@pytest_asyncio.fixture
async def real_stack(aweb_cloud_db, test_db_factory, monkeypatch) -> AsyncIterator[RealStack]:
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

    aweb_app = _build_events_app(aweb_cloud_db.aweb_db, public_origin=AWEB_URL)
    aweb_app.include_router(apps_router)
    aweb_app.include_router(app_hooks_router)
    try:
        yield RealStack(
            aweb_db=aweb_cloud_db.aweb_db,
            awid_db=infra.get_manager("aweb"),
            awid=ServiceTransport(awid_app),
            aweb=ServiceTransport(aweb_app),
            service_token=service_token,
        )
    finally:
        await registry_redis.aclose()
        await infra.close()
