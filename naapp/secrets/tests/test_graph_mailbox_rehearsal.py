"""An operator-staged vault member calls the real Secrets tool with aw."""

import asyncio
import base64
import json
import os
import ssl
import subprocess
from contextlib import asynccontextmanager
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from aweb_naapp.egress import DnsResolver, InternalDestinations
from aweb_naapp_testing import Member, canonical_json_bytes, sign_message
from aweb_naapp_testing.network import DnsServer, local_address, make_certificates
from fastapi import FastAPI, Request, Response
from upstream.serve import serving

from aweb_secrets import records
from aweb_secrets.custody import Custody, LocalKeyFileProvider, encode_value
from aweb_secrets.destination import DestinationRecord
from aweb_secrets.grant import GrantRecord
from aweb_secrets.graph_mailbox import mailbox_changes
from aweb_secrets.operation import OperationTemplate
from aweb_secrets.policy import Policy
from aweb_secrets.secret import SecretRecord
from tests.harness import NAME, CertificateTokenEndpoint, oauth_certificate
from tests.test_console_bootstrap import delivered_url
from tests.test_graph_mailbox import MAILBOX, SENDER, TOKEN, graph_secret
from tests.test_production_bootstrap import enrolled_owner
from tests.test_production_bootstrap import root as root

MESSAGE_ID = "AAMkAA="
INTERNET_ID = "<original@example.test>"
REPLY_TO = "support-reply@example.test"
MAILBOX_PATH = f"/v1.0/users/{MAILBOX}"
TOKEN_PATH = urlsplit(TOKEN).path


@pytest.fixture
async def graph_stand_in(tmp_path):
    directory = tmp_path / "graph"
    directory.mkdir()
    host = local_address()
    tls = make_certificates(directory, [NAME], [])
    registry_cert, credential = oauth_certificate("mailbox-client")
    verifier = CertificateTokenEndpoint("mailbox-client", (registry_cert,))
    access_token = "local-graph-access-token"
    calls = []
    app = FastAPI()

    @app.post(TOKEN_PATH)
    async def token(request: Request):
        if not verifier(request, parse_qs((await request.body()).decode())):
            return Response(status_code=401)
        calls.append(("token", None))
        return {"access_token": access_token, "token_type": "Bearer", "expires_in": 600}

    @app.api_route(MAILBOX_PATH + "/{endpoint:path}", methods=["GET", "POST"])
    async def mailbox(endpoint: str, request: Request):
        if request.headers.get("authorization") != f"Bearer {access_token}":
            return Response(status_code=401)
        if request.method == "GET" and endpoint == "messages":
            calls.append(("list", dict(request.query_params)))
            return {"value": [{"id": MESSAGE_ID}], "@odata.nextLink": "https://example.test/rest"}
        if request.method == "GET" and endpoint == f"messages/{MESSAGE_ID}":
            calls.append(("get", dict(request.query_params)))
            return {
                "id": MESSAGE_ID, "internetMessageId": INTERNET_ID,
                "replyTo": [{"emailAddress": {"address": REPLY_TO}}],
                "body": {"contentType": "text", "content": "Synthetic mailbox message"},
            }
        if request.method == "POST" and endpoint == "sendMail":
            if request.headers["content-type"] == "text/plain":
                message = BytesParser(policy=email_policy.default).parsebytes(
                    base64.b64decode(await request.body(), validate=True)
                )
                calls.append(("reply", message))
            else:
                calls.append(("send", await request.json()))
            return Response(status_code=202)
        if request.method == "POST" and endpoint == f"messages/{MESSAGE_ID}/move":
            calls.append(("archive", await request.json()))
            return {"id": MESSAGE_ID, "parentFolderId": "archive"}
        return Response(status_code=404)

    dns = DnsServer(records={(NAME, "A"): [host]})
    await dns.start()
    try:
        async with serving(app, tls, host) as port:
            verifier.audience = f"https://{NAME}:{port}{TOKEN_PATH}"
            yield port, host, tls.ca_file, dns.port, credential, calls
    finally:
        dns.close()


@pytest.fixture
def root_broker_network(console_listener, graph_stand_in):
    _, _, ca_file, dns_port, *_ = graph_stand_in
    return console_listener.origin, ca_file, DnsResolver(nameservers=["127.0.0.1"], port=dns_port)


@asynccontextmanager
async def listening(service, listener, tls):
    server = uvicorn.Server(uvicorn.Config(
        service, log_level="warning", lifespan="off",
        ssl_certfile=str(tls.cert_file), ssl_keyfile=str(tls.key_file),
    ))
    task = asyncio.create_task(server.serve(sockets=[listener.socket]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("the Secrets listener stopped during startup")
                await asyncio.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def staged_home(parent: Path, member: Member, certificate: dict, origin: str) -> Path:
    """Stage the fixture-issued local member certificate, never the team key."""
    home = parent / member.alias
    cert = "team-certs/" + member.team_id.replace("__", "____").replace(":", "__") + ".pem"
    (home / "team-certs").mkdir(parents=True, mode=0o700)
    (home / "signing.key").write_text(
        "-----BEGIN ED25519 PRIVATE KEY-----\n"
        + base64.b64encode(bytes(member.member_key)).decode()
        + "\n-----END ED25519 PRIVATE KEY-----\n"
    )
    (home / "signing.key").chmod(0o600)
    (home / cert).write_text(json.dumps(certificate))
    membership = {"team_id": member.team_id, "alias": member.alias, "cert_path": cert}
    (home / "workspace.yaml").write_text(json.dumps({
        "aweb_url": origin, "memberships": [membership],
    }))
    (home / "teams.yaml").write_text(json.dumps({
        "active_team": member.team_id, "memberships": [membership],
    }))
    return home


async def aw_submit(home, tls, url, body, status):
    body_file = home.parent / "mailbox-request.json"
    body_file.write_text(json.dumps(body))
    body_file.chmod(0o600)
    result = await asyncio.to_thread(
        subprocess.run,
        [
            os.environ["NAAPP_AW_BINARY"], "--identity-home", str(home), "id", "request",
            "POST", url, "--team-auth", "--raw", "--header", "Content-Type: application/json",
            "--body-file", str(body_file),
        ],
        cwd=home.parent,
        env={"PATH": os.environ["PATH"], "SSL_CERT_FILE": str(tls.ca_file)},
        capture_output=True, text=True, timeout=20,
    )
    assert f"HTTP {status}" in result.stderr.splitlines(), (result.stderr, result.stdout)
    assert (result.returncode == 0) is (status < 400)
    return json.loads(result.stdout)


async def configure_mailbox(root, stand_in, seat, approver):
    """Seed confirmed records, rebinding only provider transport to the fixture."""
    service, _, settings, owner, installed, *_ = root
    installation, team = installed["installation_id"], owner.team_id
    port, host, _, _, credential, _ = stand_in
    canonical = graph_secret()
    changes = mailbox_changes(
        canonical, mailbox=MAILBOX, mailbox_address=SENDER, vault_agents=[seat.did_key],
        approver=approver, include_archive=True,
    )
    target = f"https://{NAME}:{port}{TOKEN_PATH}"
    secret = SecretRecord.from_after({
        **canonical.after,
        "kind_params": {**canonical.kind_params, "token_endpoint": target, "audience": target},
    })
    db = service.runtime.db()
    custody = Custody(LocalKeyFileProvider(settings.custody_key_file), db)
    operation_pins = {}
    policy_pins = {}
    async with db.transaction() as tx:
        await records.create_secret(tx, installation, team, secret)
        version = await custody.sealer.ingest(
            tx, installation_id=installation, team_id=team, secret_id=secret.secret_id,
            value=encode_value(secret.kind, credential),
        )
        await records.set_active_version(tx, installation, team, secret.secret_id, version)
        for item in changes:
            after = item["after"]
            if item["kind"] == "destination":
                destination = DestinationRecord.from_after(
                    {**after, "host": NAME, "port": port, "network": "internal", "cidr": f"{host}/32"},
                    internal_destinations=InternalDestinations.ALLOWED,
                )
                await records.create_destination(tx, installation, team, destination)
            elif item["kind"] == "operation":
                operation = OperationTemplate.from_after({
                    **after, "url": {**after["url"], "host": NAME, "port": port},
                })
                await records.create_operation(tx, installation, team, operation)
                operation_pins[operation.operation_id] = operation.digest
            elif item["kind"] == "policy":
                body = {key: value for key, value in after.items() if key != "v"}
                body["operations"] = [
                    {**pin, "digest": operation_pins[pin["operation_id"]]}
                    for pin in body["operations"]
                ]
                policy = Policy.from_body(body)
                await records.create_policy(tx, installation, policy, team_id=team)
                policy_pins[item["ref"]] = policy.digest
            elif item["kind"] == "grant":
                grant = GrantRecord.from_after({
                    **after,
                    "policy_digest": policy_pins[after["policy_digest"]],
                    "operations": [
                        {**pin, "digest": operation_pins[pin["operation_id"]]}
                        for pin in after["operations"]
                    ],
                })
                await records.create_grant(tx, installation, team, grant)
    return secret


async def approve_operation(root, holder, expected_params, since):
    _, client, settings, _, _, smtp, *_ = root
    url = delivered_url(smtp.received[since:], contact=holder.body["contact"], path="/approvals")
    [token] = parse_qs(urlsplit(url).fragment)["token"]
    page = settings.console_origin + "/use-approvals"
    headers = {"X-CSRF-Token": holder.csrf_token}
    view_options = await client.post(page + "/view-options", json={"token": token}, headers=headers)
    assert view_options.status_code == 200, view_options.text
    shown = await client.post(
        page + "/view", json={"token": token, "assertion": holder.device.get(view_options.json())},
        headers=headers,
    )
    assert shown.status_code == 200, shown.text
    assert shown.json()["view"]["operation"]["params"] == expected_params
    decision = {"token": token, "decision": "approve"}
    options = await client.post(page + "/decision-options", json=decision, headers=headers)
    assert options.status_code == 200, options.text
    decided = await client.post(
        page + "/decide", json={**decision, "assertion": holder.device.get(options.json())},
        headers=headers,
    )
    assert decided.status_code == 200 and decided.json()["state"] == "approved", decided.text


@pytest.mark.parametrize("transport", ["signed-http", pytest.param("aw", marks=pytest.mark.e2e)])
async def test_staged_vault_member_reads_and_replies_after_owner_approval(
    root, registry, graph_stand_in, tmp_path, console_listener, console_tls, transport,
):
    service, _, settings, owner, _, smtp, *_ = root
    agents = []
    for alias in ("vault-agent", "same-team-ungranted"):
        seat = Member(
            team_id=owner.team_id, team_key=owner.team_key, certificate_id=alias, alias=alias,
            extra_cert_fields={"identity_scope": "local"},
        )
        certificate = seat.certificate()
        del certificate["lifetime"], certificate["signature"]
        certificate["signature"] = sign_message(bytes(seat.team_key), canonical_json_bytes(certificate))
        await registry.add_certificate(certificate, team_key=seat.team_key)
        agents.append((seat, certificate))
    holder = await enrolled_owner(root)
    (agent, _), neighbor = agents
    secret = await configure_mailbox(root, graph_stand_in, agent, holder.body["principal"])
    url = f"{settings.public_origin}/v1/secrets/{secret.ref}/requests"
    calls = graph_stand_in[-1]
    async def submit(identity, body, status):
        seat, certificate = identity
        if transport == "aw":
            home = homes[seat.alias]
            return await aw_submit(home, console_tls, url, body, status)
        raw = json.dumps(body).encode()
        response = await http.post(
            url, content=raw, headers={"Content-Type": "application/json"} | seat.headers(
                aud=settings.public_origin, method="POST", path=urlsplit(url).path,
                body=raw, cert=certificate,
            ),
        )
        assert response.status_code == status, response.text
        return response.json()

    homes = {
        seat.alias: staged_home(tmp_path, seat, certificate, settings.public_origin)
        for seat, certificate in agents
    } if transport == "aw" else {}
    async with (
        listening(service, console_listener, console_tls),
        httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=str(console_tls.ca_file)), trust_env=False,
        ) as http,
    ):
        read = {"request_id": "list-1", "operation": "list-messages", "params": {}}
        denied = await submit(neighbor, read, 403)
        assert denied["code"] == "grant_missing" and calls == []
        listing = await submit(agents[0], read, 200)
        assert listing["state"] == "completed"
        assert json.loads(listing["result"]["body"]) == {"/value": [{"id": MESSAGE_ID}]}
        got = await submit(agents[0], {
            "request_id": "get-1", "operation": "get-message", "params": {"message_id": MESSAGE_ID},
        }, 200)
        message = json.loads(got["result"]["body"])
        assert message["/internetMessageId"] == INTERNET_ID
        assert message["/replyTo"] == [{"emailAddress": {"address": REPLY_TO}}]

        params = {
            "recipient": REPLY_TO, "in_reply_to": INTERNET_ID, "subject": "Re: Support",
            "text": "Thank you for getting in touch.",
        }
        reply = {"request_id": "reply-1", "operation": "reply", "params": params}
        smtp_before = len(smtp.received)
        pending = await submit(agents[0], reply, 202)
        assert pending["state"] == "awaiting_approval"
        assert [call for call, _ in calls] == ["token", "list", "get"]
        await approve_operation(root, holder, params, smtp_before)
        assert [call for call, _ in calls] == ["token", "list", "get"]
        sent = await submit(agents[0], reply, 200)
        assert sent["state"] == "completed" and sent["result"]["status"] == 202
        assert (await submit(agents[0], reply, 200))["state"] == "completed"
        assert [call for call, _ in calls] == ["token", "list", "get", "reply"]
        assert calls[1][1]["$top"] == "25"
        outgoing = calls[-1][1]
        assert [address.addr_spec for address in outgoing["To"].addresses] == [params["recipient"]]
        assert outgoing["In-Reply-To"] == INTERNET_ID
        assert outgoing["Cc"] is None and outgoing["Bcc"] is None

        for operation, action, params, status, provider_body in (
            (
                "send-mail", "send", {"recipient": REPLY_TO, "subject": "Support", "text": "Hello"},
                202, {"message": {
                    "subject": "Support", "body": {"contentType": "Text", "content": "Hello"},
                    "toRecipients": [{"emailAddress": {"address": REPLY_TO}}],
                }},
            ),
            (
                "archive-message", "archive", {"message_id": MESSAGE_ID}, 200,
                {"destinationId": "archive"},
            ),
        ):
            body = {"request_id": operation + "-1", "operation": operation, "params": params}
            before_calls, before_mail = len(calls), len(smtp.received)
            assert (await submit(agents[0], body, 202))["state"] == "awaiting_approval"
            await approve_operation(root, holder, params, before_mail)
            assert len(calls) == before_calls
            completed = await submit(agents[0], body, 200)
            assert completed["state"] == "completed" and completed["result"]["status"] == status
            assert calls[before_calls:] == [(action, provider_body)]
