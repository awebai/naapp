"""secrets' records in its database: secrets, policies, grants and grant templates.

Every write runs in the caller's transaction, so a confirmed change set commits
all of its records or none. Every read is scoped by installation, so a record of
one installation is never found from another, and a reinstalled team (a new
installation ID) finds none of the old grants.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from aweb_naapp.egress import InternalDestinations
from pgdbm.core import TransactionManager

from aweb_secrets import run_pin_store
from aweb_secrets.authority_integrity import (
    AuthorityEvidenceInvalid,
    AuthorityRecords,
    invalidate_authority,
)
from aweb_secrets.destination import DestinationRecord
from aweb_secrets.grant import GrantRecord, GrantTemplate
from aweb_secrets.operation import OperationTemplate
from aweb_secrets.policy import Policy
from aweb_secrets.revisions import bump_config_revision
from aweb_secrets.secret import SecretRecord


class RecordMissing(LookupError):
    """The record to change does not exist in this installation."""


class RecordRefused(ValueError):
    """The record cannot be stored as it stands; ``code`` is the refusal code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StoredSecret:
    record: SecretRecord
    state: str
    active_version: int | None


async def require_secret_definition(
    tx: TransactionManager, authority: AuthorityRecords, stored: StoredSecret, *, ref: str
) -> None:
    if stored.record.ref != ref:
        raise AuthorityEvidenceInvalid()
    await authority.require(tx, "secret", (stored.record.secret_id,), stored.record.after)


async def require_secret_authority(
    tx: TransactionManager, authority: AuthorityRecords, stored: StoredSecret, *, ref: str
) -> None:
    await require_secret_definition(tx, authority, stored, ref=ref)
    secret_id = stored.record.secret_id
    await authority.require(tx, "secret_state", (secret_id,), {"state": stored.state})
    await authority.require(tx, "secret_version", (secret_id,), {"version": stored.active_version})


async def create_secret(
    tx: TransactionManager, installation_id: str, team_id: str, record: SecretRecord
) -> None:
    await tx.execute(
        """
        INSERT INTO {{tables.secrets}} (secret_id, installation_id, team_id, ref, record)
        VALUES ($1, $2, $3, $4, $5)
        """,
        record.secret_id,
        installation_id,
        team_id,
        record.ref,
        json.dumps(record.after),
    )


async def update_secret(
    tx: TransactionManager, installation_id: str, team_id: str, record: SecretRecord
) -> None:
    """Replace a secret's record; its state and active version are untouched."""
    updated = await tx.fetch_value(
        """
        UPDATE {{tables.secrets}} SET ref = $4, record = $5, updated_at = NOW()
        WHERE secret_id = $1 AND installation_id = $2 AND team_id = $3
        RETURNING secret_id
        """,
        record.secret_id,
        installation_id,
        team_id,
        record.ref,
        json.dumps(record.after),
    )
    if updated is None:
        raise RecordMissing("no such secret in this installation")


async def secret_by_ref(
    tx: TransactionManager, installation_id: str, team_id: str, ref: str, *, lock: bool = True
) -> StoredSecret | None:
    """Read a secret by ref, normally locked until the transaction ends.

    ``lock=False`` discovers the ID before ordered ancestry locks; that result
    grants no authority and must be re-read under those locks.
    """
    row = await tx.fetch_one(
        """
        SELECT record, state, active_version FROM {{tables.secrets}}
        WHERE installation_id = $1 AND team_id = $2 AND ref = $3
        """
        + (" FOR SHARE" if lock else ""),
        installation_id,
        team_id,
        ref,
    )
    if row is None:
        return None
    return StoredSecret(
        record=SecretRecord.from_after(json.loads(row["record"])),
        state=row["state"],
        active_version=row["active_version"],
    )


async def set_active_version(
    tx: TransactionManager, installation_id: str, team_id: str, secret_id: str, version: int
) -> None:
    """Point a secret at the custody version the broker opens, when its change set is confirmed."""
    updated = await tx.fetch_value(
        """
        UPDATE {{tables.secrets}} SET active_version = $4, updated_at = NOW()
        WHERE installation_id = $1 AND team_id = $2 AND secret_id = $3
        RETURNING secret_id
        """,
        installation_id,
        team_id,
        secret_id,
        version,
    )
    if updated is None:
        raise RecordMissing("no such secret in this installation")

    await run_pin_store.discard_secret(
        tx,
        installation_id=installation_id,
        team_id=team_id,
        secret_id=secret_id,
        keep_version=version,
    )


async def disable_secret(
    tx: TransactionManager, installation_id: str, team_id: str, secret_id: str
) -> None:
    """Disable a secret and withdraw its active-state evidence under the caller's lock."""
    await tx.execute(
        "UPDATE {{tables.secrets}} SET state='disabled', updated_at=NOW()"
        " WHERE installation_id=$1 AND team_id=$2 AND secret_id=$3 AND state<>'disabled'",
        installation_id,
        team_id,
        secret_id,
    )
    await invalidate_authority(tx, installation_id, team_id, "secret_state", (secret_id,))


async def create_policy(
    tx: TransactionManager, installation_id: str, policy: Policy, *, team_id: str
) -> None:
    """Store ``policy``; storing the same digest again is the same policy and changes nothing."""
    await tx.execute(
        """
        INSERT INTO {{tables.policies}} (installation_id, digest, body, team_id) VALUES ($1, $2, $3, $4)
        ON CONFLICT (installation_id, team_id, digest) DO NOTHING
        """,
        installation_id,
        policy.digest,
        json.dumps(dict(policy.body)),
        team_id,
    )


async def policy(
    tx: TransactionManager,
    installation_id: str,
    digest: str,
    *,
    team_id: str,
    authority: AuthorityRecords | None = None,
) -> Policy | None:
    body = await tx.fetch_value(
        "SELECT body FROM {{tables.policies}} WHERE installation_id = $1 AND digest = $2 AND team_id = $3",
        installation_id,
        digest,
        team_id,
    )
    if body is None:
        return None
    value = json.loads(body)
    if authority is not None:
        await authority.require(tx, "policy", (digest,), value)
    return Policy.from_body(value)


async def create_grant(
    tx: TransactionManager, installation_id: str, team_id: str, grant: GrantRecord
) -> None:
    await tx.execute(
        """
        INSERT INTO {{tables.grants}} (
            grant_id, installation_id, team_id, secret_id, subject_principal, subject_role,
            capability, open_request, policy_digest, record
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        grant.grant_id,
        installation_id,
        team_id,
        grant.secret_id,
        grant.subject_principal,
        grant.subject_role,
        grant.capability,
        grant.open_request,
        grant.policy_digest,
        json.dumps(grant.after),
    )
    for operation_id, digest in grant.operations:
        await tx.execute(
            "INSERT INTO {{tables.grant_operations}} (grant_id, operation_id, digest) VALUES ($1, $2, $3)",
            grant.grant_id,
            operation_id,
            digest,
        )
    for job_id, digest in grant.jobs:
        await tx.execute(
            "INSERT INTO {{tables.grant_jobs}} (grant_id, job_id, digest) VALUES ($1, $2, $3)",
            grant.grant_id,
            job_id,
            digest,
        )


async def revoke_grant(tx: TransactionManager, installation_id: str, grant_id: str) -> None:
    """Revoke a grant at once, a narrowing act that moves the installation to its next
    configuration revision; revoking a revoked grant changes nothing."""
    revoked = await tx.fetch_value(
        """
        UPDATE {{tables.grants}} SET revoked_at = NOW()
        WHERE installation_id = $1 AND grant_id = $2 AND revoked_at IS NULL
        RETURNING team_id
        """,
        installation_id,
        grant_id,
    )
    if revoked is not None:
        await invalidate_authority(tx, installation_id, revoked, "grant", (grant_id,))
        await bump_config_revision(tx, installation_id, team_id=revoked)


async def active_grants(
    tx: TransactionManager,
    installation_id: str,
    secret_id: str,
    *,
    principal_did: str,
    roles: Sequence[str],
    lock: bool = True,
    authority: AuthorityRecords | None = None,
) -> list[GrantRecord]:
    """The unrevoked grants on a secret to this principal or to any of its roles.

    The grants are locked until the transaction ends, so a revocation that
    commits first is seen and one that commits later waits. With ``lock=False``
    this is only candidate discovery: the caller must lock the sorted union of
    candidate/source grants and check revocation before relying on the result.
    """
    rows = await tx.fetch_all(
        """
        SELECT grant_id, team_id, record FROM {{tables.grants}}
        WHERE installation_id = $1 AND secret_id = $2 AND revoked_at IS NULL
          AND (subject_principal = $3 OR subject_role = ANY($4::text[]))
        ORDER BY grant_id
        """
        + (" FOR SHARE" if lock else ""),
        installation_id,
        secret_id,
        principal_did,
        list(roles),
    )
    grants = []
    for row in rows:
        value = json.loads(row["record"])
        if authority is not None:
            if row["team_id"] != authority.scope.team_id:
                raise AuthorityEvidenceInvalid()
            await authority.require(tx, "grant", (row["grant_id"],), value)
        grant = GrantRecord.from_after(value)
        if authority is not None and (
            grant.grant_id != row["grant_id"]
            or grant.secret_id != secret_id
            or not (grant.subject_principal == principal_did or grant.subject_role in roles)
        ):
            raise AuthorityEvidenceInvalid()
        grants.append(grant)
    return grants


async def set_grant_template(
    tx: TransactionManager, installation_id: str, template: GrantTemplate, *, team_id: str
) -> None:
    await tx.execute(
        """
        INSERT INTO {{tables.grant_templates}} (installation_id, grant_template_id, record, team_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (installation_id, team_id, grant_template_id)
        DO UPDATE SET record = EXCLUDED.record, updated_at = NOW()
        """,
        installation_id,
        template.grant_template_id,
        json.dumps(template.after),
        team_id,
    )


async def grant_template(
    tx: TransactionManager, installation_id: str, grant_template_id: str, *, team_id: str
) -> GrantTemplate | None:
    record = await tx.fetch_value(
        """
        SELECT record FROM {{tables.grant_templates}}
        WHERE installation_id = $1 AND grant_template_id = $2 AND team_id = $3
        """,
        installation_id,
        grant_template_id,
        team_id,
    )
    return None if record is None else GrantTemplate.from_after(json.loads(record))


async def create_destination(
    tx: TransactionManager, installation_id: str, team_id: str, record: DestinationRecord
) -> None:
    """Store a destination already checked under the deploy's internal-destinations setting."""
    await tx.execute(
        """
        INSERT INTO {{tables.destinations}}
            (destination_id, installation_id, team_id, secret_id, record)
        VALUES ($1, $2, $3, $4, $5)
        """,
        record.destination_id,
        installation_id,
        team_id,
        record.secret_id,
        json.dumps(dict(record.after)),
    )


async def destinations(
    tx: TransactionManager,
    installation_id: str,
    secret_id: str,
    *,
    authority: AuthorityRecords | None = None,
) -> list[DestinationRecord]:
    """A secret's destinations, by id.

    They are read as stored; the egress guard checks the deploy's
    internal-destinations setting again when it matches one at dispatch.
    """
    rows = await tx.fetch_all(
        """
        SELECT destination_id, team_id, record FROM {{tables.destinations}}
        WHERE installation_id = $1 AND secret_id = $2
        ORDER BY destination_id
        """,
        installation_id,
        secret_id,
    )
    destinations = []
    for row in rows:
        value = json.loads(row["record"])
        if authority is not None:
            if row["team_id"] != authority.scope.team_id:
                raise AuthorityEvidenceInvalid()
            await authority.require(tx, "destination", (row["destination_id"],), value)
        destination = DestinationRecord.from_after(
            value, internal_destinations=InternalDestinations.ALLOWED
        )
        if authority is not None and destination.secret_id != secret_id:
            raise AuthorityEvidenceInvalid()
        destinations.append(destination)
    return destinations


async def remove_destination(
    tx: TransactionManager, installation_id: str, destination_id: str
) -> None:
    """Remove a destination, a narrowing act that moves the installation to its next
    configuration revision; removing an absent destination changes nothing."""
    removed = await tx.fetch_value(
        """
        DELETE FROM {{tables.destinations}}
        WHERE installation_id = $1 AND destination_id = $2
        RETURNING team_id
        """,
        installation_id,
        destination_id,
    )
    if removed is not None:
        await invalidate_authority(tx, installation_id, removed, "destination", (destination_id,))
        await bump_config_revision(tx, installation_id, team_id=removed)


async def create_operation(
    tx: TransactionManager, installation_id: str, team_id: str, template: OperationTemplate
) -> None:
    """Store a confirmed template and make it the operation's current one.

    A sensitive secret admits named operations only with a response
    projection (§7.6), so an unprojected template on one is refused.
    """
    sensitivity = await tx.fetch_value(
        """
        SELECT record->>'sensitivity' FROM {{tables.secrets}}
        WHERE installation_id = $1 AND team_id = $2 AND secret_id = $3
        FOR SHARE
        """,
        installation_id,
        team_id,
        template.secret_id,
    )
    if sensitivity == "sensitive" and template.projection is None:
        raise RecordRefused(
            "an operation on a sensitive secret needs a response projection",
            code="operation_not_allowed",
        )
    await tx.execute(
        """
        INSERT INTO {{tables.operation_templates}}
            (installation_id, team_id, secret_id, operation_id, digest, record)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
        """,
        installation_id,
        team_id,
        template.secret_id,
        template.operation_id,
        template.digest,
        json.dumps(dict(template.after)),
    )
    await tx.execute(
        """
        INSERT INTO {{tables.current_operations}}
            (installation_id, secret_id, operation_id, digest)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (installation_id, secret_id, operation_id) DO UPDATE SET digest = EXCLUDED.digest
        """,
        installation_id,
        template.secret_id,
        template.operation_id,
        template.digest,
    )


async def current_operation(
    tx: TransactionManager,
    installation_id: str,
    secret_id: str,
    operation_id: str,
    *,
    lock: bool = True,
    authority: AuthorityRecords | None = None,
) -> OperationTemplate | None:
    """CURRENT, normally locked against replacement until the transaction ends.

    ``lock=False`` is inert configured-access discovery, never send authority.
    """
    row = await tx.fetch_one(
        """
        SELECT t.record, t.team_id, c.digest FROM {{tables.current_operations}} c
        JOIN {{tables.operation_templates}} t USING (installation_id, secret_id, operation_id, digest)
        WHERE c.installation_id = $1 AND c.secret_id = $2 AND c.operation_id = $3
        """
        + (" FOR SHARE OF c" if lock else ""),
        installation_id,
        secret_id,
        operation_id,
    )
    if row is None:
        return None
    value = json.loads(row["record"])
    if authority is not None:
        if row["team_id"] != authority.scope.team_id:
            raise AuthorityEvidenceInvalid()
        await authority.require(tx, "operation", (secret_id, operation_id), value)
    operation = OperationTemplate.from_after(value)
    if authority is not None and (
        operation.digest != row["digest"]
        or operation.secret_id != secret_id
        or operation.operation_id != operation_id
    ):
        raise AuthorityEvidenceInvalid()
    return operation
