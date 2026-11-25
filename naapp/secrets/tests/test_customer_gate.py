import asyncio
import json
import sys

from aweb_naapp.roles import begin_holding, end_holding

from tests.human_owner import fixture_principal
from tests.test_production_bootstrap import (
    enrolled_owner,
    mounted_owner_console,
)
from tests.test_production_bootstrap import root as root


async def observe(configured, credential_file, setup, *, schema=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "aweb_secrets.gate_observe",
        "--team-id",
        setup.team_id,
        "--setup-principal",
        setup.principal_did,
        "--database-url-file",
        str(credential_file),
        "--schema",
        schema or configured.db_schema,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    assert not stderr, stderr.decode()
    assert configured.database_url.encode() not in stdout
    return process.returncode, json.loads(stdout)


async def test_gate_refuses_a_retained_setup_owner_and_then_its_remaining_session(root, tmp_path):
    service, client, configured, member, installed, smtp, transport, dns = root
    customer = await enrolled_owner(root)
    console = mounted_owner_console(service)
    setup = fixture_principal(member.team_id, "setup")
    credential_file = tmp_path / "customer-database-url"
    credential_file.write_text(configured.database_url, encoding="utf-8")
    credential_file.chmod(0o400)

    exit_code, section = await observe(configured, credential_file, setup)
    assert exit_code == 0 and section["passed"] and section["readable"]
    assert {(item["code"], item["subject"]) for item in section["findings"]} == {
        ("role_assignment", customer.body["principal"]),
        ("active_session", customer.body["principal"]),
    }

    await console.sessions.open(
        subject="fixture-unattributed-subject",
        scope="fixture-no-current-authority",
        evidence_ref="fixture-private-evidence",
    )
    exit_code, section = await observe(configured, credential_file, setup)
    assert exit_code == 0 and not section["passed"] and section["readable"]
    assert "session_authority_unknown" in [item["code"] for item in section["findings"]]
    await console.db.execute(
        "UPDATE console_sessions SET closed_at=NOW() WHERE scope=$1",
        "fixture-no-current-authority",
    )

    role = {
        "installation_id": installed["installation_id"],
        "team_id": setup.team_id,
        "role": "secrets.owner",
        "principal": setup.principal_did,
    }
    async with console.db.transaction() as tx:
        holding = await begin_holding(tx, certificate=None, **role)
    assert holding is not None

    exit_code, section = await observe(configured, credential_file, setup)
    assert exit_code == 0 and not section["passed"] and section["readable"]
    assert [
        finding["code"] for finding in section["findings"] if finding["code"].startswith("setup_")
    ] == ["setup_role_retained"]

    issued = await console.sessions.open(
        subject="fixture-setup-subject",
        scope=holding.b3_scope,
        evidence_ref="fixture-private-evidence",
    )
    async with console.db.transaction() as tx:
        assert await end_holding(tx, **role)

    exit_code, section = await observe(configured, credential_file, setup)
    assert exit_code == 0 and not section["passed"] and section["readable"]
    assert [
        finding["code"] for finding in section["findings"] if finding["code"].startswith("setup_")
    ] == ["setup_session_retained"]
    serialized = json.dumps(section)
    assert issued.token not in serialized
    assert issued.csrf_token not in serialized
    assert "fixture-private-evidence" not in serialized
    assert "fixture-setup-subject" not in serialized

    await console.db.execute(
        "UPDATE console_sessions SET closed_at=NOW() WHERE scope=$1",
        holding.b3_scope,
    )
    exit_code, section = await observe(configured, credential_file, setup)
    assert exit_code == 0 and section["passed"] and section["readable"]


async def test_gate_refuses_when_its_database_evidence_is_unavailable(root, tmp_path):
    service, client, configured, member, installed, smtp, transport, dns = root
    credential_file = tmp_path / "customer-database-url"
    credential_file.write_text(configured.database_url, encoding="utf-8")
    credential_file.chmod(0o400)
    setup = fixture_principal(member.team_id, "setup")

    exit_code, section = await observe(
        configured, credential_file, setup, schema="missing_gate_evidence"
    )
    assert exit_code == 1 and not section["passed"] and not section["readable"]
    assert [finding["code"] for finding in section["findings"]] == ["evidence_unavailable"]
