"""aweb server/tests/test_dashboard_privacy.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from aweb.api import create_app
from aweb.db import DatabaseInfra
from aweb.embedding import OutboxRecoveryOwnership
from aweb.mutation_hooks import create_mutation_handler
from aweb.outbox_recovery import stop_outbox_recovery
from awid.did import did_key_from_signing_key
from awid.ratelimit import MemoryFixedWindowRateLimiter
from awid.registry import RegistryClient
from awid_service.db import AwidDatabaseInfra
from awid_service.main import create_app as create_registry_app
from awid_service.testing import Registry, ServiceTransport
from contextlib import asynccontextmanager
from redis.asyncio import Redis
from types import SimpleNamespace
from uuid import UUID, uuid4
import asyncio
import httpx
import os
import pytest_asyncio
import socket
import ssl
import uvicorn
from tests.server_compat.aweb_server_tests.dashboard_test_support import _JWT_SECRET
from tests.server_compat.aweb_server_tests.messages_http import _encode_certificate, _make_certificate, _make_keypair

AWEB_REF = 'server-v1.27.53'
AWEB_PATH = 'server/tests/test_dashboard_privacy.py'
AWEB_BLOB = '25b566ac473b487140908574ce2d1cbe6f3c1332'
COPIED = {
    'TEAM_NAME': '48741b6ebc1188e9ccfb6c5fa643141e6abec7f1f2b3b34ab6049a20f8d3505f',
    'TEAM': 'bb932753a0e6d745ecc4f36ee5e6428e044fec9994b6a3e1785724cdd280f6c7',
    '_': 'b040519bf5e209b6d14704132d468ae198b71f40d87e4312e3279444b5e50cd9',
    'privacy_app': 'd47c1f69dea10d755e5cd6707589d38bedc2328dba5cff545d56a671b3a2deee',
    'privacy_stack': 'bbb700f7b0d6b9d5d4a7ec993b7a5d7db39bb1469ef20c47bf325fedc540f538',
}


TEAM_NAME = f"privacy-{uuid4().hex[:12]}"


TEAM = f"{TEAM_NAME}:example.test"


_TEAM_SIGNING_KEY, _, _TEAM_DID_KEY = _make_keypair()


@pytest_asyncio.fixture
async def privacy_app(shared_test_pool, monkeypatch, request):
    async with privacy_stack(shared_test_pool, monkeypatch, proxy_verification=getattr(request, "param", None)) as env:
        yield env


@asynccontextmanager
async def privacy_stack(
    shared_test_pool, monkeypatch, tls=None, *,
    aliases=("alice", "bob", "carol"), team_visibility="public", database=None,
    outbox_recovery=OutboxRecoveryOwnership.NATIVE, proxy_verification=None,
    workspace_counts=None,
):
    """The real apps behind one HTTP server; tls is (host, Certificates covering
    host) to serve over TLS, so the server's public origin is https."""
    monkeypatch.setenv("AWID_DATABASE_URL", "postgresql://unused/test")
    service_token = "privacy-tests-disposable-service-token"
    monkeypatch.setenv("AWID_SERVICE_TOKEN", service_token)
    # Candidate runners provide Redis as a service, not a local executable.
    # Never flush this shared database; team/workspace/session keys are unique.
    async with Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    ) as redis:
        await redis.ping()
        db = database if database is not None else DatabaseInfra()
        await db.initialize(shared_pool=shared_test_pool)
        registry_db = AwidDatabaseInfra(schema="privacy_registry")
        await registry_db.initialize(shared_pool=shared_test_pool)
        # Registry rate limits are not under test here; an in-memory window
        # keeps each app's DID registrations out of the per-IP window that
        # every run on the shared Redis would otherwise consume.
        registry_app = create_registry_app(
            db_infra=registry_db,
            redis=redis,
            rate_limiter=MemoryFixedWindowRateLimiter(),
        )
        # The registry's public origin, which covering controller signatures name.
        registry_app.state.awid_public_origins = ("http://registry.test",)
        async with registry_app.router.lifespan_context(registry_app):
            fixture_registry = Registry(
                app=registry_app,
                db=registry_db.get_manager(),
                transport=ServiceTransport(registry_app),
                registry_url="http://registry.test",
            )
            namespace_key = await fixture_registry.ensure_namespace(domain="example.test")
            registry = RegistryClient(
                registry_url="http://registry.test",
                transport=fixture_registry.transport,
                service_token=service_token,
                domain_verifier=fixture_registry.namespace_authority,
            )
            app = create_app(
                host_runs_outbox_recovery=outbox_recovery is OutboxRecoveryOwnership.HOST,
                proxy_verification=proxy_verification,
            )
            # Externally provisioned resources, exactly as an embedding host
            # supplies them; lifecycle startup would create different ones.
            app.state.db = db
            app.state.redis = redis
            app.state.awid_registry_client = registry
            app.state.dashboard_jwt_secret = _JWT_SECRET
            dispatched = []
            handler = create_mutation_handler(redis, db)

            async def observe_mutation(event_type, context):
                dispatched.append((event_type, dict(context)))
                return await handler(event_type, context)

            app.state.on_mutation = observe_mutation
            manager = db.get_manager("aweb")
            team_sk, team_did = _TEAM_SIGNING_KEY, _TEAM_DID_KEY
            await manager.execute(
                "INSERT INTO {{tables.teams}} (team_id, namespace, team_name, team_did_key) "
                "VALUES ($1, 'example.test', $2, $3)",
                TEAM,
                TEAM_NAME,
                team_did,
            )
            await registry.register_team(
                domain="example.test",
                name=TEAM_NAME,
                display_name=TEAM_NAME,
                team_did_key=team_did,
                visibility=team_visibility,
                namespace_controller_signing_key=namespace_key,
            )
            actors = {}
            for alias in aliases:
                sk, _, did = _make_keypair()
                agent_id, workspace_id = uuid4(), uuid4()
                await manager.execute(
                    "INSERT INTO {{tables.agents}} "
                    "(agent_id, team_id, did_key, alias, identity_scope, inbound_mode) "
                    "VALUES ($1, $2, $3, $4, 'local', 'open')",
                    agent_id,
                    TEAM,
                    did,
                    alias,
                )
                extras = (workspace_counts or {}).get(alias, 1) - 1
                workspace_ids = [workspace_id, *(uuid4() for _ in range(extras))]
                for index, workspace in enumerate(workspace_ids):
                    await manager.execute(
                        "INSERT INTO {{tables.workspaces}} (workspace_id, team_id, agent_id, alias) "
                        "VALUES ($1, $2, $3, $4)",
                        workspace,
                        TEAM,
                        agent_id,
                        alias if index == 0 else f"{alias}-workspace-{index + 1}",
                    )
                certificate = _encode_certificate(
                    _make_certificate(
                        team_sk,
                        team_did,
                        did,
                        team_id=TEAM,
                        alias=alias,
                        identity_scope="local",
                        certificate_id=f"privacy-{alias}",
                    )
                )
                actors[alias] = SimpleNamespace(
                    sk=sk,
                    did=did,
                    id=agent_id,
                    workspace=workspace_id,
                    workspace_ids=workspace_ids,
                    certificate=certificate,
                )
            host, certificates = tls or ("127.0.0.1", None)
            scheme = "https" if certificates else "http"
            sock = socket.socket()
            sock.bind((host, 0))
            base_url = f"{scheme}://{host}:{sock.getsockname()[1]}"
            app.state.public_origin = base_url
            server_tls = (
                {
                    "ssl_certfile": str(certificates.cert_file),
                    "ssl_keyfile": str(certificates.key_file),
                }
                if certificates
                else {}
            )
            server = uvicorn.Server(
                uvicorn.Config(app, lifespan="off", log_level="error", ws="none", **server_tls)
            )
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.02)
                assert server.started
                async with httpx.AsyncClient(
                    base_url=base_url,
                    timeout=5,
                    verify=ssl.create_default_context(cafile=str(certificates.ca_file))
                    if certificates
                    else True,
                ) as client:
                    yield SimpleNamespace(
                        client=client,
                        db=manager,
                        redis=redis,
                        actors=actors,
                        registry_db=registry_db.get_manager(),
                        registry_app=registry_app,
                        registry=fixture_registry,
                        dispatched=dispatched,
                        app=app,
                        infra=db,
                        namespace_sk=namespace_key,
                        namespace_did=did_key_from_signing_key(namespace_key),
                        team_sk=team_sk,
                        team_did=team_did,
                    )
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 5)
                sock.close()
                await stop_outbox_recovery(app)
                await registry.aclose()
                await fixture_registry.aclose()
        await registry_db.close()
        if database is None:
            await db.close()
