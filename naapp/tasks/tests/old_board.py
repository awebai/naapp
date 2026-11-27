"""A team's board on the current aweb server, the source of the one-off board move.

The board is written through the server's own task, claim and reservation
routes, signed by real member certificates, on the server release production
runs. A state those routes cannot reach (two claims on one task, app request
evidence, a value the app refuses) is written into the same database by SQL,
as an older server or a race left it.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from aweb_naapp_testing import Member
from nacl.signing import SigningKey

DOMAIN = "board.example"
NAMESPACE_KEY = SigningKey.generate()
EXPORT_SQL = Path(__file__).resolve().parents[1] / "board_move" / "export.sql"


@dataclass
class OldBoard:
    coordination: Any
    members: dict[str, Member]
    # Where psql reaches the server's database: the test server's host and credentials.
    connection: Any

    @property
    def team_id(self) -> str:
        return next(iter(self.members.values())).team_id

    @property
    def db(self) -> Any:
        return self.coordination.server.db.get_manager("aweb")

    async def call(self, alias: str, method: str, path: str, body: Any = None) -> Any:
        member = self.members[alias]
        server = self.coordination.server
        data = b"" if body is None else json.dumps(body).encode()
        headers = member.headers(aud=server.url, method=method, path=path, body=data)
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with server.client() as client:
            response = await client.request(method, path, content=data, headers=headers)
        assert response.status_code == 200, f"{method} {path}: {response.status_code} {response.text}"
        return response.json()

    async def create(self, alias: str, **fields: Any) -> dict[str, Any]:
        return await self.call(alias, "POST", "/v1/tasks", fields)

    async def update(self, alias: str, ref: str, **fields: Any) -> dict[str, Any]:
        return await self.call(alias, "PATCH", f"/v1/tasks/{ref}", fields)

    async def comment(self, alias: str, ref: str, body: str) -> dict[str, Any]:
        return await self.call(alias, "POST", f"/v1/tasks/{ref}/comments", {"body": body})

    async def depend(self, alias: str, ref: str, on: str) -> dict[str, Any]:
        return await self.call(alias, "POST", f"/v1/tasks/{ref}/deps", {"depends_on": on})

    async def delete(self, alias: str, ref: str) -> dict[str, Any]:
        return await self.call(alias, "DELETE", f"/v1/tasks/{ref}")

    async def reserve(self, alias: str, resource: str, *, ttl_seconds: int, reason: str) -> dict[str, Any]:
        return await self.call(alias, "POST", "/v1/reservations", {
            "resource_key": resource, "ttl_seconds": ttl_seconds, "metadata": {"reason": reason},
        })

    async def database_name(self) -> str:
        return await self.db.fetch_value("SELECT current_database()")

    def postgres_env(self, database: str) -> dict[str, str]:
        config = self.connection.config
        return {
            **os.environ,
            "PGHOST": config.host, "PGPORT": str(config.port),
            "PGUSER": config.user, "PGPASSWORD": config.password, "PGDATABASE": database,
        }

    async def export(self, path: Path, *, database: str | None = None) -> subprocess.CompletedProcess[str]:
        """Runs the operator's export exactly as the board move's README gives it,
        against the server's database or another that holds its schema."""
        env = self.postgres_env(database or await self.database_name())
        return subprocess.run(
            ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-v", f"team_id={self.team_id}",
             "-f", str(EXPORT_SQL), "-o", str(path)],
            env=env, capture_output=True, text=True, check=False,
        )


async def old_board(coordination: Any, connection: Any, aliases: list[str]) -> OldBoard:
    """A new team on the server with these members, each connected (one workspace each)."""
    team_id = f"board-{uuid4().hex[:10]}:{DOMAIN}"
    first = Member(team_id=team_id, certificate_id="c_1", alias=aliases[0])
    members = {aliases[0]: first} | {
        alias: Member(team_id=team_id, team_key=first.team_key, certificate_id=f"c_{i}", alias=alias)
        for i, alias in enumerate(aliases[1:], start=2)
    }
    for member in members.values():
        await coordination.add_member(member, namespace_controller=NAMESPACE_KEY)
    await coordination.publish_server(first)
    for member in members.values():
        await coordination.connect(member)
    return OldBoard(coordination=coordination, members=members, connection=connection)
