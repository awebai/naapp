"""Real lane operations for the joined Secrets customer acceptance."""

from __future__ import annotations

import base64
import json
import re
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from aweb_naapp_testing.compose_lane import (
    CA_PATH,
    ComposeLane,
    docker,
    identity_folder,
)
from team_journey_support import aw, first_json

from aweb_secrets.policy import Policy

UPSTREAM = "https://upstream.secrets.svc.test"
SECRET_REF = "customer-api"
REQUEST_ID = "customer-first-use"
AGENT_ROLE = "secrets.role.customer"


def start_upstream(lane: ComposeLane) -> str:
    value = secrets.token_hex(32)
    lane.redact(value, base64.b64encode(value.encode()).decode())
    directory = lane.runtime / "customer-upstream"
    directory.mkdir(mode=0o700)
    (directory / "bearer").write_text(value)
    (directory / "bearer").chmod(0o600)
    lane.up("customer-upstream")
    return value


def create_agent(lane, controller, founder, *, binary, team, domain, registry, aweb):
    agent = lane.machine("customer-agent", aw_binary=binary, isolated=True)
    identity = aw(
        founder,
        "--identity-home",
        identity_folder("customer-agent"),
        "id",
        "create",
        "--name",
        "customer-agent",
        "--domain",
        domain,
        "--root-parent",
        "--registry",
        registry,
    )
    invite = aw(
        controller, "id", "team", "invite", "--team", team, "--namespace", domain, "--member-global"
    )
    aw(
        agent,
        "id",
        "team",
        "accept-invite",
        invite["token"],
        "--name",
        "customer-agent",
        "--global",
        "--address",
        identity["address"],
    )
    aw(agent, "init", "--url", aweb, "--name", "customer-agent", "--do-not-touch-agents-md")
    checked = docker(
        "exec",
        agent,
        "sh",
        "-ec",
        'test "$(ls -a /runtime | sort | tr "\\n" " ")" = ". .. customer-agent tls "',
    )
    assert checked.returncode == 0, "the requester runtime is not isolated"
    return agent, identity["did_aw"]


def configure_use(secret_id, owner, member, subnet):
    policy = Policy.from_body(
        {
            "approval": "per_use",
            "approvers": ["owner:" + owner],
            "approval_ttl_s": 900,
            "rate": [{"per": "principal_secret", "capacity": 10, "window_s": 3600}],
            "windows": None,
            "operations": [],
            "open_request": True,
            "request": {"max_bytes": 65536},
            "result": {"max_bytes": 65536},
            "requires_attestation": None,
            "distinct_approver": "not_required",
        }
    )
    destination = {
        "v": 1,
        "destination_id": "dst_customer",
        "secret_id": secret_id,
        "scheme": "https",
        "host": urlsplit(UPSTREAM).hostname,
        "port": 443,
        "path": "/customer",
        "methods": ["POST"],
        "query_rules": {},
        "header_allow": [],
        "network": "internal",
        "cidr": subnet,
    }
    grant = {
        "v": 1,
        "grant_id": "grt_customer",
        "secret_id": secret_id,
        "subject": {"role": AGENT_ROLE},
        "capability": "use",
        "operations": [],
        "open_request": True,
        "jobs": [],
        "policy_digest": policy.digest,
    }
    return {
        "reason": "Enable approved agent access to the customer API",
        "sealed": [],
        "changes": [
            {
                "kind": "policy",
                "op": "create",
                "ref": policy.digest,
                "after": {"v": 1, **policy.body},
            },
            {
                "kind": "destination",
                "op": "create",
                "ref": destination["destination_id"],
                "after": destination,
            },
            {
                "kind": "role",
                "op": "create",
                "ref": AGENT_ROLE,
                "after": {"v": 1, "role": AGENT_ROLE, "principal": member},
            },
            {"kind": "grant", "op": "create", "ref": grant["grant_id"], "after": grant},
        ],
    }


def chromium(lane: ComposeLane, holder: str, origin: str, scope: str, action: str, **state):
    command = json.dumps({"action": action, "origin": origin, "scope": scope, **state})
    result = docker(
        "run",
        "--rm",
        "-i",
        f"--network=container:{holder}",
        "--user",
        f"{lane.runtime.stat().st_uid}:{lane.runtime.stat().st_gid}",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{lane.runtime / 'holder'}:/holder",
        "-v",
        f"{lane.runtime / 'tls' / 'ca.pem'}:{CA_PATH}:ro",
        f"{lane.project}-chromium",
        "customer-console",
        "--device",
        "/holder/authenticator.json",
        "--ca",
        CA_PATH,
        input=command,
        timeout=180,
    )
    return first_json(lane.checked(result, f"customer browser {action}"))


def mailbox_json(lane: ComposeLane, controller: str, path: str):
    result = docker(
        "exec",
        controller,
        "python3",
        "-c",
        "import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())",
        "http://mail.svc.test:8025/api/v1/" + path,
    )
    assert result.returncode == 0, "could not read the test mailbox"
    return json.loads(result.stdout)


def approval_messages(lane, controller, contact):
    return {
        msg["ID"]
        for msg in mailbox_json(lane, controller, "messages")["messages"]
        if msg["Subject"] == "Approval requested"
        and any(recipient["Address"] == contact for recipient in msg["To"])
    }


def new_approval_link(lane, controller, contact, origin, before):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        new = approval_messages(lane, controller, contact) - before
        if new:
            assert len(new) == 1, "expected one new approval message"
            message = mailbox_json(lane, controller, "message/" + new.pop())
            pattern = re.escape(origin) + r"/approvals#token=[^\s<>]+"
            match = re.search(pattern, message["Text"])
            assert match is not None, "approval email did not contain the console link"
            link = match[0]
            lane.redact(link, urlsplit(link).fragment.removeprefix("token="))
            return link
        time.sleep(0.2)
    raise AssertionError("approval email did not arrive")


def assert_value_absent(text, value):
    if value in text or base64.b64encode(value.encode()).decode() in text:
        raise AssertionError("the agent or audit received the credential")


def agent_submit(agent, origin, value, request_id, *, status, state, code=None):
    body = {
        "request_id": request_id,
        "method": "POST",
        "url": UPSTREAM + "/customer",
        "data": "customer acceptance",
    }
    result = docker(
        "exec",
        agent,
        "aw",
        "id",
        "request",
        "POST",
        f"{origin}/v1/secrets/{SECRET_REF}/requests",
        "--team-auth",
        "--raw",
        "--header",
        "Content-Type: application/json",
        "--body",
        json.dumps(body),
    )
    assert_value_absent(result.stdout + result.stderr, value)
    status_matches = re.findall(r"(?m)^HTTP (\d{3})$", result.stderr)
    assert status_matches == [str(status)], (status_matches, result.returncode)
    assert (result.returncode == 0) == (status < 400), result.returncode
    reply = json.loads(result.stdout)
    assert reply.get("state") == state and reply.get("code") == code, reply
    assert reply["request_id"] == request_id
    return reply


def upstream_receipts(lane: ComposeLane) -> list[str]:
    result = lane.compose(
        "exec", "-T", "customer-upstream", "cat", "/tmp/customer-upstream-receipts"
    )
    return lane.checked(result, "read upstream receipt counts").splitlines()


def audit_watermark(lane: ComposeLane) -> int:
    return int(
        lane.query(
            "secrets", "secrets", "SELECT coalesce(max(position), 0) FROM secrets.outbox_events"
        )
    )


def audit_events(lane: ComposeLane, after: int) -> dict[str, str]:
    rows = lane.query(
        "secrets",
        "secrets",
        "SELECT event_id, kind FROM secrets.outbox_events"
        f" WHERE position > {after} ORDER BY position",
    )
    return dict(row.split("|", 1) for row in rows.splitlines())


def back_up_customer(lane: ComposeLane, destination: Path) -> None:
    # Stop the app writers so the three database archives describe the same
    # customer state. Compose owns the disposable database and keys.
    lane.checked(
        lane.compose("stop", "secrets", "logs", "awid", "aweb"), "quiesce the customer apps"
    )
    destination.mkdir(mode=0o700)
    for database, expected in (
        ("secrets", "sealed_versions"),
        ("logs", "chain_events"),
        ("logsledger", "event_purges"),
    ):
        archive = f"/tmp/{database}.dump"
        dump = lane.compose(
            "exec", "-T", "postgres", "pg_dump", "-U", "secrets", "-Fc", "-f", archive, database
        )
        lane.checked(dump, f"back up {database}")
        contents = lane.compose("exec", "-T", "postgres", "pg_restore", "--list", archive)
        assert re.search(
            rf"\bTABLE DATA \S+ {expected}\b", lane.checked(contents, f"inspect {database} backup")
        )
        lane.checked(
            lane.compose("cp", f"postgres:{archive}", str(destination / Path(archive).name)),
            f"retain {database} backup",
        )
        assert (destination / f"{database}.dump").stat().st_size > 0
        (destination / f"{database}.dump").chmod(0o600)
    shutil.copytree(lane.runtime / "keys", destination / "secrets-keys")
    shutil.copytree(lane.runtime / "logs", destination / "logs-keys")


def confirm_native_bearer(lane, controller, holder, origin, scope, installation_id, contact, value):
    before = approval_messages(lane, controller, contact)
    staged = chromium(
        lane,
        holder,
        origin,
        scope,
        "enter-native",
        path=f"/installations/{installation_id}/native-values",
        ref=SECRET_REF,
        value=value,
    )
    chromium(
        lane,
        holder,
        origin,
        scope,
        "approve-change",
        path=f"/installations/{installation_id}/change-sets",
        link=new_approval_link(lane, controller, contact, origin, before),
    )
    return staged["secret_id"]


def exercise_customer_use(
    lane: ComposeLane,
    *,
    controller: str,
    holder: str,
    agent: str,
    agent_principal: str,
    owner_principal: str,
    owner_scope: str,
    reader_scope: str,
    contact: str,
    api: str,
    console: str,
    logs: str,
    installation_id: str,
    secret_id: str,
    value: str,
):
    change_sets = f"/installations/{installation_id}/change-sets"
    messages = approval_messages(lane, controller, contact)
    draft = chromium(
        lane,
        holder,
        console,
        owner_scope,
        "post",
        path=change_sets,
        body=configure_use(secret_id, owner_principal, agent_principal, lane.subnet),
    )
    assert draft["state"] == "pending"
    link = new_approval_link(lane, controller, contact, console, messages)
    chromium(
        lane,
        holder,
        console,
        owner_scope,
        "approve-change",
        link=link,
        path=change_sets,
        change_id=draft["change_id"],
    )

    before_use = audit_watermark(lane)
    messages = approval_messages(lane, controller, contact)
    pending = agent_submit(agent, api, value, REQUEST_ID, status=202, state="awaiting_approval")
    assert upstream_receipts(lane) == []

    link = new_approval_link(lane, controller, contact, console, messages)
    chromium(lane, holder, console, owner_scope, "approve-use", link=link)
    assert upstream_receipts(lane) == []

    completed = agent_submit(agent, api, value, REQUEST_ID, status=200, state="completed")
    assert completed["request_ref"] == pending["request_ref"]
    assert completed["result"]["status"] == 200
    assert completed["result"]["redaction_count"] > 0
    assert upstream_receipts(lane) == ["accepted"]

    replay = agent_submit(agent, api, value, REQUEST_ID, status=200, state="completed")
    assert replay["request_ref"] == completed["request_ref"]
    assert replay["result"]["body"] is None
    assert upstream_receipts(lane) == ["accepted"]

    removal = chromium(
        lane,
        holder,
        console,
        owner_scope,
        "post",
        path=f"/installations/{installation_id}/containment",
        body={
            "request_id": "customer-remove-agent",
            "action": "remove-role-member",
            "target": AGENT_ROLE,
            "principal": agent_principal,
        },
    )
    assert removal["action"] == "remove-role-member" and removal["target"] == AGENT_ROLE
    agent_submit(
        agent,
        api,
        value,
        "customer-after-removal",
        status=403,
        state="refused",
        code="grant_missing",
    )
    assert upstream_receipts(lane) == ["accepted"]

    attempts = lane.query(
        "secrets",
        "secrets",
        "SELECT d.attempts FROM secrets.dispatches d"
        " JOIN secrets.use_contexts c USING(dispatch_id)"
        " JOIN secrets.intents i ON i.intent_id = c.intent_id"
        f" WHERE i.request_id = '{REQUEST_ID}'",
    )
    assert attempts == "1", "expected exactly one persisted dispatch attempt"
    source = audit_events(lane, before_use)
    audit = chromium(lane, holder, logs, reader_scope, "read-audit", event_ids=list(source))
    assert_value_absent(json.dumps(audit), value)
    assert {event_id: event["hook"] for event_id, event in audit.items()} == source
    uses = [event["payload"] for event in audit.values() if event["hook"] == "post-secret-use-hook"]
    assert [event["action"] for event in uses] == [
        "approval_requested",
        "approval_decided",
        "admitted",
        "dispatching",
        "completed",
        "refused",
    ]
    assert all(event["use_ref"] == completed["request_ref"] for event in uses[:-1])
    assert uses[1]["approval_decision"] == "approved"
    assert uses[-1]["refusal_code"] == "grant_missing"
    (revoked,) = (
        event["payload"]
        for event in audit.values()
        if event["hook"] == "post-governance-hook" and event["payload"]["act"] == "role.revoked"
    )
    assert revoked["role"] == AGENT_ROLE
    assert revoked["subject_ref"] == f"member:{agent_principal}"

    back_up_customer(lane, lane.runtime / "customer-backup")
