"""aweb server/tests/test_app_task_requests.py at server-v1.27.53, the definitions the server-compatibility suite uses.

Copied verbatim by scripts/server_test_support.py and checked against COPIED by
tests/server_compat/test_aweb_server_tests.py. Do not edit them here; rerun the
script from the aweb tag this repository pins.
"""
# ruff: noqa
from aweb.app_credentials import APP_REQUEST, AppCredential
from aweb.app_registry import get_installation_on
from aweb_naapp.runtime.app_credentials import app_request_credential
from aweb_naapp.runtime.task_requests import AUTHORIZATION_PREFIX, sign_stage_receipt
from awid.did import did_from_public_key
from awid.signing import canonical_json_bytes, sign_message
from datetime import datetime, timedelta, timezone
from nacl.signing import SigningKey
from starlette.requests import Request
from uuid import UUID, uuid4
import base64
import hashlib
import json
import pytest_asyncio
from tests.server_compat.aweb_server_tests.dashboard_privacy import TEAM, privacy_app

AWEB_TAG = 'server-v1.27.53'
AWEB_PATH = 'server/tests/test_app_task_requests.py'
AWEB_BLOB = '0e35141b3b3d0924f2235159b6611830cbc08a10'
COPIED = {
    '_verified_install': '7a27522d778a00ad7a2caf77877be4e27961f66c4bdf00b74e4e5dd5f21aace5',
    'app_request_context': '593c2303885bb7e62ecc315fbe59f58b10a6e0c330a314fa51e152d164d95f7c',
    '_app_http': '993610b7a42a5b539560f2e26dd1d509931c04e47790f1d983f90248d9f04cdd',
    '_stage': '99b8f2a28e6a6fa4f7056121d20892f4e1a10e610b3d97928ce1031bef88cc3c',
    '_body': '6a831439b6856722dcf4f5672e36c767f4b404f0630fca8efea687bab078f116',
    '_sign': '0a438710a1f2f5694bc56031d30eb57911639736fa7ed23eff4d5b0f52c4ca90',
}


async def _verified_install(
    db,
    *,
    team_id,
    app_id,
    did_key,
    kid="requests-1",
    installation_id="install-fixture",
    generation=1,
    origin="https://fixture.example.test",
    event_did=None,
):
    """Provision disposable verified registry state; F6 owns the install flow."""
    app_keys = [{"kid": kid, "did_key": did_key, "purposes": ["app-request"]}]
    if event_did:
        app_keys.append({"kid": "event-only", "did_key": event_did, "purposes": ["app-event"]})
    manifest = canonical_json_bytes(
        {
            "manifest_version": 2,
            "app": {"id": app_id, "origin": origin, "version": "1.0.0"},
            "app_keys": app_keys,
        }
    )
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    await db.execute(
        "INSERT INTO {{tables.app_registry_apps}} (app_id,origin) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        app_id,
        origin,
    )
    await db.execute(
        """INSERT INTO {{tables.app_registry_entries}} (app_id,origin,digest,app_version,manifest_version)
        VALUES ($1,$2,$3,'1.0.0',2) ON CONFLICT DO NOTHING""",
        app_id,
        origin,
        digest,
    )
    await db.execute(
        """INSERT INTO {{tables.team_app_installs}}
        (team_id,app_id,origin,digest,app_version,manifest_version,granted_scopes,installation_id,generation,state,origin_verified,manifest_bytes)
        VALUES ($1,$2,$3,$4,'1.0.0',2,ARRAY['requests.create','requests.read'],$5,$6,'active',TRUE,$7)""",
        team_id,
        app_id,
        origin,
        digest,
        installation_id,
        generation,
        manifest,
    )
    await db.execute(
        """INSERT INTO {{tables.team_app_install_counters}} (team_id,app_id,last_generation)
        VALUES ($1,$2,$3)""",
        team_id,
        app_id,
        generation,
    )
    await db.execute(
        """INSERT INTO {{tables.team_app_keys}} (team_id,app_id,origin,digest,key_id,did_key,purposes)
        VALUES ($1,$2,$3,$4,$5,$6,ARRAY['app-request'])""",
        team_id,
        app_id,
        origin,
        digest,
        kid,
        did_key,
    )
    if event_did:
        await db.execute(
            """INSERT INTO {{tables.team_app_keys}} (team_id,app_id,origin,digest,key_id,did_key,purposes)
            VALUES ($1,$2,$3,$4,'event-only',$5,ARRAY['app-event'])""",
            team_id,
            app_id,
            origin,
            digest,
            event_did,
        )
    return dict(await get_installation_on(db, team_id=team_id, app_id=app_id))


@pytest_asyncio.fixture
async def app_request_context(privacy_app):
    env = privacy_app
    env.app.state.public_origin = str(env.client.base_url).rstrip("/")
    from types import SimpleNamespace
    from test_messages_http import _make_keypair, _make_certificate, _encode_certificate

    sk, _, did = _make_keypair()
    agent_id, workspace_id = uuid4(), uuid4()
    await env.db.execute(
        """INSERT INTO {{tables.agents}} (agent_id,team_id,did_key,alias,identity_scope,inbound_mode)
        VALUES ($1,$2,$3,'dave','local','open')""",
        agent_id,
        TEAM,
        did,
    )
    await env.db.execute(
        """INSERT INTO {{tables.workspaces}} (workspace_id,team_id,agent_id,alias)
        VALUES ($1,$2,$3,'dave')""",
        workspace_id,
        TEAM,
        agent_id,
    )
    env.actors["dave"] = SimpleNamespace(
        sk=sk,
        did=did,
        id=agent_id,
        workspace=workspace_id,
        certificate=_encode_certificate(
            _make_certificate(
                env.team_sk,
                env.team_did,
                did,
                team_id=TEAM,
                alias="dave",
                identity_scope="local",
                certificate_id="privacy-dave",
            )
        ),
    )
    for alias, actor in env.actors.items():
        cert = json.loads(base64.b64decode(actor.certificate))
        await env.app.state.awid_registry_client.register_team_certificate(
            "example.test",
            TEAM.split(":", 1)[0],
            team_controller_signing_key=env.team_sk,
            certificate_id=cert["certificate_id"],
            member_did_key=actor.did,
            member_did_aw=None,
            member_address=None,
            alias=alias,
            identity_scope="local",
            certificate=actor.certificate,
        )
    env.app_signing_key = SigningKey.generate()
    env.event_signing_key = SigningKey.generate()
    app_did = did_from_public_key(bytes(env.app_signing_key.verify_key))
    installation = await _verified_install(
        env.db,
        team_id=TEAM,
        app_id="fixture",
        did_key=app_did,
        event_did=did_from_public_key(bytes(env.event_signing_key.verify_key)),
    )
    credential = AppCredential(
        purpose=APP_REQUEST,
        team_id=TEAM,
        app_id="fixture",
        kid="requests-1",
        did_key=app_did,
        digest=installation["digest"],
        installation_id="install-fixture",
        installation_generation=1,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/tasks/app-requests",
            "headers": [(b"x-aweb-team-id", TEAM.encode()), (b"x-aweb-app-id", b"fixture")],
            "app": env.app,
        }
    )
    return env, request, credential, installation


async def _app_http(context, method, path, body=None):
    env, _, credential, installation = context
    raw = canonical_json_bytes(body) if body is not None else b""
    headers = app_request_credential(
        signing_key=env.app_signing_key,
        aud=env.app.state.public_origin,
        method=method,
        path=path,
        team_id=credential.team_id,
        app_id=credential.app_id,
        kid=credential.kid,
        installation_id=installation["installation_id"],
        installation_generation=installation["generation"],
        body=raw,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    headers["Content-Type"] = "application/json"
    return await env.client.request(method, path, content=raw, headers=headers)


def _stage(context, task, actor, answer, **overrides):
    env, _, credential, installation = context
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = dict(
        v=1,
        type="aweb.app-answer-stage",
        team_id=credential.team_id,
        app_id=credential.app_id,
        installation_id=installation["installation_id"],
        installation_generation=installation["generation"],
        kid=credential.kid,
        did_key=credential.did_key,
        task_id=task["task_id"],
        question_rev=task["question_rev"],
        answer_key=answer["answer_key"],
        answer_sha256=answer["answer_sha256"],
        answerer_did=env.actors[actor].did,
        choice=answer.get("choice"),
        proposal_rev=answer.get("proposal_rev"),
        proposal_sha256=answer.get("proposal_sha256"),
        issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        nonce=uuid4().hex,
    )
    payload.update(overrides)
    return {"payload": payload, "signature": sign_stage_receipt(payload, env.app_signing_key)}


def _body(env, *, request_id="action-1", expired=False):
    task = dict(
        title="Review a page request",
        description=None,
        assignee_did=env.actors["bob"].did,
        app_request=dict(
            external_request_id=request_id,
            resource_ref="opaque-page",
            action_ref="decide",
            proposal_rev=2,
            proposal_sha256="sha256:" + "d" * 64,
            choices=[
                {"value": "approve", "label": "Approve"},
                {"value": "reject", "label": "Reject"},
            ],
            eligible_dids=[env.actors["bob"].did],
            role=None,
            fields=[{"field": "price", "label": "Price", "format": "number"}],
        ),
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if expired:
        now -= timedelta(days=2)
    payload = dict(
        v=1,
        type="aweb.app-task-authorization",
        aud=env.app.state.public_origin,
        app_origin="https://fixture.example.test",
        team_id=TEAM,
        app_id="fixture",
        installation_id="install-fixture",
        installation_generation=1,
        external_request_id=request_id,
        actor_did_key=env.actors["alice"].did,
        certificate_id="privacy-alice",
        target_dids=task["app_request"]["eligible_dids"],
        task_body_sha256=hashlib.sha256(canonical_json_bytes(task)).hexdigest(),
        action_id="decide",
        proposal_rev=2,
        proposal_sha256=task["app_request"]["proposal_sha256"],
        issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        nonce="1" * 32,
    )
    return _sign(
        env,
        dict(
            task=task,
            task_authorization=dict(
                payload=payload, signature="", certificate=env.actors["alice"].certificate
            ),
        ),
    )


def _sign(env, body):
    signature = sign_message(
        env.actors["alice"].sk,
        AUTHORIZATION_PREFIX + canonical_json_bytes(body["task_authorization"]["payload"]),
    )
    body["task_authorization"]["signature"] = signature.replace("+", "-").replace("/", "_")
    return body
