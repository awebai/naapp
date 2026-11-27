"""aweb server/tests/test_app_events_http.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from __future__ import annotations
from aweb.internal_auth import ProxyVerificationConfig
from aweb.routes.events import router as events_router
from fastapi import FastAPI
from uuid import UUID
import hashlib

AWEB_REF = 'server-v1.27.53'
AWEB_PATH = 'server/tests/test_app_events_http.py'
AWEB_BLOB = 'f4a82223721dd8428941dd5f96cd962a2773b975'
COPIED = {
    'TEAM_ID': 'a0314a435bc1df73fbddcd026e522cf0995472aff05fedf43c64e198fdd9dd86',
    'AGENT_ID': 'd3f652a7ad53d398b8b6e3231654e50f7687a10c02bfcb563345feffbcfd49ad',
    '_DbShim': 'bf3dcaf71e6c87ab8caf547575f0efde229d723baa50f27846cc3682f67670e9',
    '_cached_body_receive': '0651973d092c47448f3cdc91d8cf992276998fc68c3347b7c07bfdf1a7a2e89f',
    '_build_events_app': 'bf2a233b34b81fc41682d497da327b652b250f2a573ef6b47133ff5bc626e18d',
    '_seed_team_and_agent': '5dc40ee4dbf26704ce5f0bd887680a86d628305e4b53114ad0237b88682c697e',
}


TEAM_ID = "backend:acme.com"


AGENT_ID = UUID("00000000-0000-0000-0000-000000000123")


class _DbShim:
    def __init__(self, aweb_db) -> None:
        self._db = aweb_db

    def get_manager(self, name: str = "aweb"):
        return self._db


def _cached_body_receive(body: bytes):
    replayed = False

    async def _receive():
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return _receive


def _build_events_app(aweb_db, *, public_origin: str = "http://test") -> FastAPI:
    app = FastAPI()
    ProxyVerificationConfig.capture().install(app)
    app.include_router(events_router)
    app.state.db = _DbShim(aweb_db)
    app.state.redis = object()
    app.state.public_origin = public_origin

    @app.middleware("http")
    async def cache_body_middleware(request, call_next):
        body = await request.body()
        request.state.cached_body = body
        request.state.body_sha256 = hashlib.sha256(body).hexdigest()
        request._receive = _cached_body_receive(body)
        return await call_next(request)

    return app


async def _seed_team_and_agent(aweb_db, *, team_id: str = TEAM_ID) -> None:
    team_name, namespace = team_id.split(":", 1)
    await aweb_db.execute(
        """
        INSERT INTO {{tables.teams}} (team_id, namespace, team_name, team_did_key)
        VALUES ($1, $2, $3, 'did:key:z6Mkteam')
        ON CONFLICT DO NOTHING
        """,
        team_id,
        namespace,
        team_name,
    )
    await aweb_db.execute(
        """
        INSERT INTO {{tables.agents}} (
            agent_id, team_id, did_key, alias, human_name, status, identity_scope, inbound_mode
        )
        VALUES ($1, $2, 'did:key:z6Mkalice', 'alice', 'Alice', 'active', 'global', 'open')
        ON CONFLICT DO NOTHING
        """,
        AGENT_ID,
        team_id,
    )
