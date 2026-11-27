from dataclasses import replace

import pytest
from aweb_naapp.runtime.installation import AdmittedInstallation

from aweb_secrets.authority_integrity import AuthorityIntegrityKey, AuthorityRecord
from tests.test_records_store import db as db

SCOPE = AdmittedInstallation(
    app_id="test-secrets", team_id="backend:example.com", installation_id="inst_test", generation=3
)
RECORD = AuthorityRecord(
    scope=SCOPE,
    kind="destination",
    identity=("sec_test", "dst_test"),
    revision=5,
    body={"path": "/v1/messages", "methods": ["GET"], "port": 443},
)


def test_a_record_matches_its_mac_independent_of_mapping_order():
    key = AuthorityIntegrityKey(b"\x01" * 32)
    tag = key.authenticate(RECORD)
    reordered = replace(RECORD, body={"port": 443, "methods": ["GET"], "path": "/v1/messages"})
    assert key.matches(reordered, tag)


@pytest.mark.parametrize(
    "different",
    [
        replace(RECORD, scope=replace(SCOPE, app_id="other")),
        replace(RECORD, scope=replace(SCOPE, team_id="backend:other.example")),
        replace(RECORD, scope=replace(SCOPE, installation_id="inst_other")),
        replace(RECORD, scope=replace(SCOPE, generation=4)),
        replace(RECORD, kind="grant"),
        replace(RECORD, identity=("sec_test", "dst_other")),
        replace(RECORD, revision=4),
        replace(RECORD, body={**RECORD.body, "methods": ["POST"]}),
    ],
)
def test_authority_evidence_cannot_be_reused_for_another_record(different):
    key = AuthorityIntegrityKey(b"\x01" * 32)
    assert not key.matches(different, key.authenticate(RECORD))


def test_wrong_or_missing_authority_evidence_does_not_match():
    key = AuthorityIntegrityKey(b"\x01" * 32)
    other = AuthorityIntegrityKey(b"\x02" * 32)
    assert not key.matches(RECORD, other.authenticate(RECORD))
    assert not key.matches(RECORD, None)
    assert not key.matches(RECORD, b"")


def test_authority_key_loads_only_the_configured_protected_file(tmp_path):
    file = tmp_path / "authority-key"
    with pytest.raises(ValueError, match="unavailable"):
        AuthorityIntegrityKey.from_file(file)
    file.write_bytes(b"01" * 32 + b"\n")
    file.chmod(0o400)
    assert AuthorityIntegrityKey.from_file(file).matches(
        RECORD, AuthorityIntegrityKey(b"\x01" * 32).authenticate(RECORD)
    )
    file.chmod(0o660)
    with pytest.raises(ValueError, match="unsafe"):
        AuthorityIntegrityKey.from_file(file)


def test_malformed_authority_key_fails_without_echoing_its_contents(tmp_path):
    file = tmp_path / "authority-key"
    file.write_bytes(b"synthetic-key-value")
    file.chmod(0o400)
    with pytest.raises(ValueError, match="64 hex") as error:
        AuthorityIntegrityKey.from_file(file)
    assert "synthetic-key-value" not in str(error.value)


async def test_publication_and_verification_share_the_records_transaction(db):
    from aweb_secrets.authority_integrity import AuthorityEvidenceInvalid, AuthorityRecords

    key = AuthorityIntegrityKey(b"\x01" * 32)
    records = AuthorityRecords(key, SCOPE)
    async with db.transaction() as tx:
        with pytest.raises(AuthorityEvidenceInvalid):
            await records.require(tx, RECORD.kind, RECORD.identity, RECORD.body)
        await records.publish(
            tx, RECORD.kind, RECORD.identity, RECORD.body, revision=RECORD.revision
        )
        await records.require(tx, RECORD.kind, RECORD.identity, RECORD.body)

    async with db.transaction() as tx:
        await records.require(tx, RECORD.kind, RECORD.identity, RECORD.body)
        with pytest.raises(AuthorityEvidenceInvalid):
            await records.require(tx, RECORD.kind, RECORD.identity, {**RECORD.body, "port": 8443})
        with pytest.raises(AuthorityEvidenceInvalid):
            await AuthorityRecords(key, replace(SCOPE, generation=4)).require(
                tx, RECORD.kind, RECORD.identity, RECORD.body
            )
        with pytest.raises(RuntimeError, match="cancel publication"):
            async with tx.transaction() as attempt:
                await records.publish(
                    attempt,
                    RECORD.kind,
                    RECORD.identity,
                    {**RECORD.body, "port": 8443},
                    revision=RECORD.revision + 1,
                )
                raise RuntimeError("cancel publication")
        await records.require(tx, RECORD.kind, RECORD.identity, RECORD.body)
