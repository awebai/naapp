"""Deployment address denials reach the production broker before any connect."""

import asyncio
import json
from ipaddress import ip_address, ip_network
from uuid import uuid4

import httpcore
import pytest
from aweb_naapp.egress import AddressPolicy, EgressRefused, InternalDestinations
from aweb_naapp.egress.send import _PinnedBackend
from aweb_naapp_testing.network import local_address
from aweb_naapp_testing.smtp import SmtpServer
from aweb_naapp_testing.team_server_site import team_server_site
from pydantic import ValidationError

from aweb_secrets import records
from aweb_secrets.composition import create_service
from aweb_secrets.config import ServiceSettings
from aweb_secrets.destination import DestinationRecord
from aweb_secrets.grant import GrantRecord
from aweb_secrets.policy import Policy
from tests.test_composition import settings
from tests.test_production_bootstrap import approval_request
from tests.test_production_bootstrap import root as root
from tests.test_use_approvals import POLICY_BODY, SECRET

# The denied destination: an internal destination the owner confirmed with
# exactly this CIDR, under internal destinations allowed, so the guard would
# reach it and only the operator's denial refuses it. A documentation address,
# never this host's, so the denial cannot also cover the team server the app
# reads at this host's address.
DENIED_DESTINATION = "192.0.2.10"


@pytest.fixture(autouse=True)
def deployment_environment(monkeypatch, request):
    """Set the operator input before the existing production-root fixture starts."""
    mode = getattr(request, "param", "address")
    entries = {
        "address": [DENIED_DESTINATION],
        "cidr": [str(ip_network(DENIED_DESTINATION + "/24", strict=False))],
        "empty": [],
    }[mode]
    monkeypatch.setenv("SECRETS_INTERNAL_DESTINATIONS", "allowed")
    monkeypatch.setenv("SECRETS_EGRESS_DENIED_NETWORKS", json.dumps(entries))
    return entries


async def scoped_request(root, url, host, port):
    """Confirmed metadata is fixture state; submission uses actual B1/custody/egress."""
    pending = await approval_request(root, "use")
    service, client, configured, member, installed, *_ = root
    db = service.runtime.db()
    policy = Policy.from_body({**POLICY_BODY, "approval": "none", "approvers": []})
    async with db.transaction() as tx:
        await records.revoke_grant(tx, installed["installation_id"], "grt_root_dispatch")
        await records.create_policy(
            tx, installed["installation_id"], policy, team_id=member.team_id
        )
        await records.create_grant(
            tx,
            installed["installation_id"],
            member.team_id,
            GrantRecord.from_after(
                {
                    "v": 1,
                    "grant_id": "grt_deployment_egress",
                    "secret_id": SECRET["secret_id"],
                    # The caller's canonical principal: approval_request's
                    # owner enrolment gives the member's certificate a
                    # member_did_aw, and a certificate with one acts as it.
                    "subject": {"principal": member.extra_cert_fields["member_did_aw"]},
                    "capability": "use",
                    "operations": [],
                    "open_request": True,
                    "jobs": [],
                    "policy_digest": policy.digest,
                }
            ),
        )
        await records.create_destination(
            tx,
            installed["installation_id"],
            member.team_id,
            DestinationRecord.from_after(
                {
                    "v": 1,
                    "destination_id": "dst_deployment_egress",
                    "secret_id": SECRET["secret_id"],
                    "scheme": "https",
                    "host": host,
                    "port": port,
                    "path": "/**",
                    "methods": ["POST"],
                    "query_rules": {},
                    "header_allow": [],
                    "network": "internal",
                    "cidr": host + "/32",
                },
                internal_destinations=InternalDestinations.ALLOWED,
            ),
        )
    raw = json.dumps(
        {"request_id": uuid4().hex, "method": "POST", "url": url, "data": "probe"}
    ).encode()
    response = await client.post(
        configured.public_origin + pending.path,
        content=raw,
        headers=member.headers(
            aud=configured.public_origin, method="POST", path=pending.path, body=raw
        )
        | {"Content-Type": "application/json"},
    )
    if pending.credential in response.text or pending.token in response.text:
        raise AssertionError("private material reached the broker receipt")
    return response


@pytest.mark.parametrize("deployment_environment", ["address", "cidr"], indirect=True)
async def test_operator_denials_stop_the_real_broker_before_tcp_connect(
    root, deployment_environment, monkeypatch
):
    check = AddressPolicy.check
    connect = _PinnedBackend.connect_tcp
    refused: list[EgressRefused] = []
    connects: list[str] = []

    def observed_check(self, addresses, *, internal=None):
        addresses = list(addresses)
        try:
            check(self, addresses, internal=internal)
        except EgressRefused as exc:
            if ip_address(DENIED_DESTINATION) in addresses:
                refused.append(exc)
            raise

    async def no_connect(self, host, port, *args, **kwargs):
        # The guard's only way to the network; a denied destination never gets
        # here. Any other connection, the team server's included, is real.
        if str(self._address) != DENIED_DESTINATION:
            return await connect(self, host, port, *args, **kwargs)
        connects.append(str(self._address))
        raise httpcore.ConnectError("the test answers no connect to the denied destination")

    monkeypatch.setattr(AddressPolicy, "check", observed_check)
    monkeypatch.setattr(_PinnedBackend, "connect_tcp", no_connect)
    response = await scoped_request(
        root, f"https://{DENIED_DESTINATION}:443/probe", DENIED_DESTINATION, 443
    )
    # The receipt is the same for any unsent request; the guard's refusal,
    # before any connect, is what shows the operator's denial stopped it.
    assert ([exc.code for exc in refused], connects) == (["destination_address_denied"], [])
    assert response.status_code == 503 and response.json()["state"] == "not_sent"


@pytest.mark.parametrize("deployment_environment", ["empty"], indirect=True)
async def test_without_operator_denials_the_real_broker_reaches_the_destination(root):
    host = local_address()
    connections = []

    async def accepted(reader, writer):
        connections.append(True)
        writer.close()
        await writer.wait_closed()

    # Count real TCP accepts, before TLS or HTTP. The endpoint deliberately
    # closes: the control proves the broker actually reaches it, without
    # exchanging a credential or weakening TLS verification.
    listener = await asyncio.start_server(accepted, host, 0)
    async with listener:
        port = listener.sockets[0].getsockname()[1]
        response = await scoped_request(root, f"https://{host}:{port}/probe", host, port)
        assert response.status_code == 503 and response.json()["state"] == "not_sent"
        assert connections


async def test_a_deployment_denying_its_own_team_server_refuses_to_start(
    test_db_factory, registry, tmp_path
):
    database = await test_db_factory.create_db(suffix="secrets_denies_own_team_server")
    async with team_server_site(tmp_path / "aweb") as site:
        with SmtpServer(tmp_path / "smtp") as smtp:
            configured = settings(database, tmp_path, smtp).model_copy(
                update={
                    **site.settings(),
                    "egress_denied_networks": (ip_network(f"{site.host}/32"),),
                }
            )
            service = create_service(
                configured, awid_transport=registry.transport, egress=site.egress(configured)
            )
            with pytest.raises(EgressRefused) as refused:
                async with service.api_app.router.lifespan_context(service.api_app):
                    pytest.fail("startup accepted a team server its own denials cover")
    assert (refused.value.code, refused.value.args[0].split(": ")[-1]) == (
        "destination_address_denied",
        site.host,
    )
    assert service.runtime.connected_db() is None
    assert site.tls.seen == []


def configuration(tmp_path):
    """Only input validation runs here; no key file is opened or service started."""
    return ServiceSettings(
        _env_file=None,
        custody_key_file=tmp_path / "custody",
        secret_request_key_file=tmp_path / "request",
        secret_params_key_file=tmp_path / "params",
        response_key_file=tmp_path / "response",
        runner_service_key_file=tmp_path / "runner",
        email_smtp_host="localhost",
        email_username="test",
        email_password_file=tmp_path / "smtp",
        email_from="noreply@example.com",
    )


def test_environment_parses_ipv4_ipv6_and_exact_ranges(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SECRETS_EGRESS_DENIED_NETWORKS",
        json.dumps(["8.8.8.8", "2606:4700::1111", "192.0.2.0/24", "2001:db8::/32"]),
    )
    assert tuple(str(network) for network in configuration(tmp_path).egress_denied_networks) == (
        "8.8.8.8/32",
        "2606:4700::1111/128",
        "192.0.2.0/24",
        "2001:db8::/32",
    )


@pytest.mark.parametrize("entry", ["not-an-address", "192.0.2.1/24", "2001:db8::1/32", True])
def test_invalid_operator_networks_refuse_configuration(monkeypatch, tmp_path, entry):
    monkeypatch.setenv("SECRETS_EGRESS_DENIED_NETWORKS", json.dumps([entry]))
    with pytest.raises(ValidationError):
        configuration(tmp_path)
