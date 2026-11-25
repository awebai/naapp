"""Authentication for Secrets authority records (design §14.3).

A tag binds one record to its authenticated scope and publication revision.
The tag alone cannot establish that the record is current.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from aweb_naapp.key_file import KeyFileError, read_key_file
from aweb_naapp.runtime.installation import AdmittedInstallation
from awid.signing import canonical_json_bytes
from pgdbm.core import TransactionManager

_TAG_DOMAIN = b"aweb-secrets/authority-record/v1\x00"
_KEY_HEX = re.compile(rb"[0-9a-fA-F]{64}\n?")


@dataclass(frozen=True)
class AuthorityRecord:
    scope: AdmittedInstallation
    kind: str
    identity: tuple[str, ...]
    revision: int
    body: Mapping[str, object]

    def _payload(self) -> bytes:
        return canonical_json_bytes(
            {
                "app_id": self.scope.app_id,
                "team_id": self.scope.team_id,
                "installation_id": self.scope.installation_id,
                "generation": self.scope.generation,
                "kind": self.kind,
                "identity": list(self.identity),
                "revision": self.revision,
                "body": dict(self.body),
            }
        )


@dataclass(frozen=True)
class AuthorityIntegrityKey:
    _material: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self._material) is not bytes or len(self._material) != 32:
            raise ValueError("an authority integrity key must be 32 immutable bytes")

    @classmethod
    def from_file(cls, path: Path) -> AuthorityIntegrityKey:
        try:
            content = read_key_file(path)
        except (KeyFileError, OSError):
            raise ValueError("authority integrity key file unavailable or unsafe") from None
        if not _KEY_HEX.fullmatch(content):
            raise ValueError("an authority integrity key file must hold 64 hex characters")
        return cls(bytes.fromhex(content.rstrip(b"\n").decode("ascii")))

    def authenticate(self, record: AuthorityRecord) -> bytes:
        return hmac.digest(self._material, _TAG_DOMAIN + record._payload(), "sha256")

    def matches(self, record: AuthorityRecord, tag: bytes | None) -> bool:
        return tag is not None and hmac.compare_digest(self.authenticate(record), tag)


class AuthorityEvidenceInvalid(ValueError):
    """No valid authentication for a required authority record."""

    code = "authority_integrity_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


async def invalidate_authority(
    tx: TransactionManager,
    installation_id: str,
    team_id: str,
    kind: str,
    identity: tuple[str, ...],
) -> None:
    """Withdraw current evidence in the narrowing transaction, without a key."""
    await tx.execute(
        """
        DELETE FROM {{tables.authority_records}}
        WHERE installation_id = $1 AND team_id = $2 AND kind = $3 AND identity = $4
        """,
        installation_id,
        team_id,
        kind,
        list(identity),
    )


@dataclass(frozen=True)
class AuthorityRecords:
    key: AuthorityIntegrityKey
    scope: AdmittedInstallation

    async def publish(
        self,
        tx: TransactionManager,
        kind: str,
        identity: tuple[str, ...],
        body: Mapping[str, object],
        *,
        revision: int,
    ) -> None:
        """Authenticate the publisher's validated input in its write transaction."""
        record = AuthorityRecord(self.scope, kind, identity, revision, body)
        await tx.execute(
            """
            INSERT INTO {{tables.authority_records}}
                (installation_id, team_id, kind, identity, revision, mac)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (installation_id, team_id, kind, identity)
            DO UPDATE SET revision = EXCLUDED.revision, mac = EXCLUDED.mac
            """,
            self.scope.installation_id,
            self.scope.team_id,
            kind,
            list(identity),
            revision,
            self.key.authenticate(record),
        )

    async def require(
        self,
        tx: TransactionManager,
        kind: str,
        identity: tuple[str, ...],
        body: Mapping[str, object],
    ) -> None:
        """Verify the exact scoped fields the consumer is about to use."""
        evidence = await tx.fetch_one(
            """
            SELECT revision, mac FROM {{tables.authority_records}}
            WHERE installation_id = $1 AND team_id = $2 AND kind = $3 AND identity = $4
            """,
            self.scope.installation_id,
            self.scope.team_id,
            kind,
            list(identity),
        )
        if evidence is None or not self.key.matches(
            AuthorityRecord(self.scope, kind, identity, evidence["revision"], body),
            evidence["mac"],
        ):
            raise AuthorityEvidenceInvalid()
