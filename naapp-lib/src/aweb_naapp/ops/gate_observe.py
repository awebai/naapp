"""Report app authority retained at a customer handover from a read-only snapshot."""

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Literal, TypedDict

import asyncpg

from aweb_naapp.hooks.envelope import TEAM_ID_PATTERN

type App = Literal["secrets", "logs"]

_SCHEMA = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}")
_PRINCIPAL = re.compile(r"did:(?:aw|key):[a-zA-Z0-9._:%-]+")


class Finding(TypedDict):
    code: str
    subject: str | None
    detail: dict[str, str]


class Section(TypedDict):
    passed: bool
    readable: bool
    findings: list[Finding]
    source: str


async def _observe(
    app: App,
    *,
    dsn: str,
    schema: str,
    team_id: str,
    setup: frozenset[str],
    source: str,
) -> Section:
    connection = await asyncpg.connect(
        dsn,
        timeout=5,
        command_timeout=5,
        server_settings={"search_path": f'pg_catalog, "{schema}"'},
    )
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            roles = await connection.fetch(
                "SELECT principal_did, role FROM role_assignments"
                " WHERE team_id=$1 ORDER BY role, principal_did",
                team_id,
            )
            holdings = await connection.fetch(
                "SELECT b3_scope, team_id, principal_did, role, ended_in FROM role_holdings"
            )
            contacts = (
                await connection.fetch(
                    "SELECT scope, team_id, principal_did FROM approvers ORDER BY scope"
                )
                if app == "secrets"
                else []
            )
            sessions = await connection.fetch(
                "SELECT scope, team_id, created_at, expires_at FROM console_sessions"
                " WHERE closed_at IS NULL AND expires_at > transaction_timestamp()"
                " ORDER BY created_at, scope"
            )
    finally:
        await connection.close(timeout=5)

    findings: list[Finding] = []
    blockers: list[Finding] = []
    for row in roles:
        principal = row["principal_did"]
        detail = {"role": row["role"]}
        findings.append({"code": "role_assignment", "subject": principal, "detail": detail})
        if principal in setup:
            blockers.append({"code": "setup_role_retained", "subject": principal, "detail": detail})

    scopes = {
        row["b3_scope"]: (row["team_id"], row["principal_did"], row["role"]) for row in holdings
    }
    for row in contacts:
        principal = row["principal_did"]
        scopes[row["scope"]] = (row["team_id"], principal, "contact_approver")
        if row["team_id"] == team_id:
            if principal in setup:
                blockers.append(
                    {"code": "setup_approver_retained", "subject": principal, "detail": {}}
                )
            elif principal is None:
                blockers.append(
                    {"code": "approver_authority_unknown", "subject": None, "detail": {}}
                )

    if app == "secrets":
        owners = {row["principal_did"] for row in roles if row["role"] == "secrets.owner"}
        if not any(
            row["team_id"] == team_id
            and row["role"] == "secrets.owner"
            and row["principal_did"] in owners - setup
            and row["ended_in"] is None
            for row in holdings
        ):
            blockers.append({"code": "customer_owner_missing", "subject": None, "detail": {}})

    for row in sessions:
        session_team = row["team_id"]
        scope_team, principal, role = scopes.get(row["scope"], (None, None, "unknown"))
        if team_id not in (session_team, scope_team) and (session_team or scope_team):
            continue
        if session_team and scope_team and session_team != scope_team:
            principal = None
        detail = {
            "role": role,
            "created_at": row["created_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat(),
        }
        findings.append({"code": "active_session", "subject": principal, "detail": detail})
        if principal is None:
            blockers.append(
                {"code": "session_authority_unknown", "subject": None, "detail": detail}
            )
        elif principal in setup:
            blockers.append(
                {"code": "setup_session_retained", "subject": principal, "detail": detail}
            )

    return {
        "passed": not blockers,
        "readable": True,
        "findings": findings + blockers,
        "source": source,
    }


def main(app: App) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--setup-principal", action="append", required=True)
    parser.add_argument("--schema", default="public")
    parser.add_argument(
        "--database-url-file",
        type=Path,
        default=Path(f"/run/secrets/{app}/database-url"),
    )
    args = parser.parse_args()

    source = f"{app} PostgreSQL role_assignments, role_holdings and console_sessions"
    if app == "secrets":
        source += ", and approvers"
    section: Section
    try:
        setup = frozenset(args.setup_principal)
        if (
            not TEAM_ID_PATTERN.fullmatch(args.team_id)
            or not all(_PRINCIPAL.fullmatch(principal) for principal in setup)
            or not _SCHEMA.fullmatch(args.schema)
        ):
            raise ValueError
        dsn = args.database_url_file.read_text(encoding="utf-8").strip()
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError
        section = asyncio.run(
            _observe(
                app, dsn=dsn, schema=args.schema, team_id=args.team_id, setup=setup, source=source
            )
        )
    except (OSError, ValueError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
        section = {
            "passed": False,
            "readable": False,
            "findings": [{"code": "evidence_unavailable", "subject": None, "detail": {}}],
            "source": source,
        }
    print(json.dumps(section))
    return 0 if section["readable"] else 1
