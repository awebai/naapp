"""The startup check verifies configured member certificates against real AWID."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aweb_naapp_testing import Member
from awid.team_certificate import decode_team_certificate
from awid_service.testing import Registry
from nacl.signing import SigningKey

from aweb_naapp.runtime.team_auth import AWIDMemberCredential, AWIDTeamCache

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3] / "docs/vectors/team-certificate-verification-v1.json"
    ).read_text()
)
TEAM_ID = VECTORS["team_id"]
CASES = {case["name"]: case for case in VECTORS["cases"]}
TEAM_KEY = SigningKey(bytes.fromhex(VECTORS["keys"]["team"]["seed_hex"]))
MEMBER_KEY = SigningKey(bytes.fromhex(VECTORS["keys"]["member"]["seed_hex"]))


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("signed_by_another_key", "bad_signature"),
        ("revoked", "revoked"),
        ("unregistered", "unregistered"),
        ("version_2", "unsupported_version"),
    ],
)
async def test_startup_check_refuses_unusable_member_certificate(
    registry: Registry, name: str, reason: str
) -> None:
    case = CASES[name]
    # A public team lets AWID return its history even when the app's
    # presented credential cannot authorize a private read.
    await registry.add_team(TEAM_ID, case["team_did_key"], visibility="public")
    for certificate_id in case["registered_certificate_ids"]:
        member = Member(
            team_id=TEAM_ID,
            team_key=TEAM_KEY,
            member_key=MEMBER_KEY,
            certificate_id=certificate_id,
            alias="member",
        )
        await registry.add_certificate(member.certificate(), team_key=TEAM_KEY)
    for certificate_id in case["revoked_certificate_ids"]:
        await registry.revoke(certificate_id, team_id=TEAM_ID)

    credential = AWIDMemberCredential(
        signing_key=bytes(MEMBER_KEY),
        certificate=decode_team_certificate(case["certificate_header"]),
    )
    cache = AWIDTeamCache(
        registry_url=registry.registry_url,
        ttl_seconds=60,
        member_credentials={TEAM_ID: credential},
        transport=registry.transport,
    )
    with pytest.raises(RuntimeError) as refused:
        await cache.check_member_credentials()
    message = str(refused.value)
    assert f"awid_member_credentials[{TEAM_ID}]" in message
    assert message.endswith(f"({reason})")
