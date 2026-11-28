"""Reads the board export (step 3) and judges whether it may be moved.

An export is the JSON document ``export.sql`` writes. A stop names a state the
move must not decide by itself: it needs an explicit disposition, with its
reason, before the board is moved.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
from aweb_naapp.runtime.authorization import GrantUnavailable
from aweb_naapp.runtime.team_grants import TeamGrantSource
from awid.team_ids import team_slug
from pgdbm import AsyncDatabaseManager, AsyncMigrationManager, DatabaseConfig
from pgdbm.core import TransactionManager
from pgdbm.errors import QueryError

from aweb_tasks.app import MIGRATIONS_PATH

EXPORT_FORMAT = "aweb-board-export/1"
# The old store's types the app has, and the one naapps ruled into "task".
KNOWN_TYPES = {"task", "bug", "feature", "epic", "chore"}


class ExportError(ValueError):
    """The file is not an export this script reads."""


class MoveRefused(Exception):
    """A step that would leave the move in a state the procedure does not allow."""


@dataclass(frozen=True)
class Refusal:
    """One reason the writer check refuses: a code, and the principal, the role it
    reaches and the object involved, where the code has them."""

    code: str
    principal: str | None
    role: str | None
    object: str | None

    def line(self) -> str:
        return "|".join(part or "" for part in (self.code, self.principal, self.role, self.object))


class CheckRefused(MoveRefused):
    """The writer check found a writer the move cannot rule out."""

    def __init__(self, refusals: tuple[Refusal, ...]) -> None:
        self.refusals = refusals
        super().__init__("\n".join(refusal.line() for refusal in refusals))


@dataclass(frozen=True)
class Stop:
    code: str
    reason: str
    subjects: tuple[str, ...]


class Export:
    """One team's frozen board, as the export query read it, named by the
    sha256 of the text it was loaded from."""

    def __init__(self, document: dict[str, Any], *, sha256: str) -> None:
        if document.get("format") != EXPORT_FORMAT:
            raise ExportError(f"not an {EXPORT_FORMAT} document")
        self.document = document
        self.sha256 = sha256
        self.team_id: str = document["team_id"]
        self.tasks: list[dict[str, Any]] = document["tasks"]
        self.comments: list[dict[str, Any]] = document["comments"]
        self.dependencies: list[dict[str, Any]] = document["dependencies"]
        self.claims: list[dict[str, Any]] = document["claims"]
        self.reservations: list[dict[str, Any]] = document["reservations"]
        self._by_id = {task["task_id"]: task for task in self.tasks}

    def ref(self, task: dict[str, Any]) -> str:
        """The task's reference as the server forms it: team slug, then its suffix."""
        return f"{team_slug(self.team_id)}-{task['task_ref_suffix']}"

    def task(self, task_id: str) -> dict[str, Any]:
        return self._by_id[task_id]

    @property
    def kept(self) -> list[dict[str, Any]]:
        """The tasks the move carries: every task not deleted, by number."""
        return [task for task in self.tasks if task["deleted_at"] is None]


def _stop(code: str, reason: str, subjects: list[str]) -> list[Stop]:
    return [Stop(code, reason, tuple(sorted(set(subjects))))] if subjects else []


def stops(export: Export) -> list[Stop]:
    """Every state on the export that needs a disposition before the move."""
    kept = export.kept
    kept_refs = {export.ref(task) for task in kept}
    deleted = {task["task_id"] for task in export.tasks if task["deleted_at"] is not None}
    claims_per_ref = Counter(claim["task_ref"] for claim in export.claims)
    task_ids_with_comment = {c["task_id"] for c in export.comments if not c["body"].strip()}
    numbers = [task["task_number"] for task in export.tasks]
    counter = export.document["task_counter"]
    return [
        *_stop(
            "more_than_one_live_claim", "a task has more than one live claim",
            [ref for ref, count in claims_per_ref.items() if count > 1],
        ),
        *_stop(
            "claim_without_task", "a claim names no task that is not deleted",
            [ref for ref in claims_per_ref if ref not in kept_refs],
        ),
        *_stop(
            "app_request", "a task carries an app request",
            [export.ref(export.task(r["task_id"])) for r in export.document["task_app_requests"]]
            + [export.ref(t) for t in export.tasks
               if t["app_request"] is not None or t["cancellation"] is not None],
        ),
        *_stop(
            "app_request_receipt", "an app request answer receipt exists",
            [r["receipt_id"] for r in export.document["task_answers"]],
        ),
        *_stop(
            "app_request_stage", "an app request answer stage record exists",
            [r["receipt_id"] for r in export.document["task_answer_stage_nonces"]],
        ),
        *_stop(
            "names_a_deleted_task", "a task that is moved has a deleted parent or waits on a deleted task",
            [export.ref(task) for task in kept if task["parent_task_id"] in deleted]
            + [export.ref(export.task(d["task_id"])) for d in export.dependencies
               if d["task_id"] not in deleted and d["depends_on_id"] in deleted],
        ),
        *_stop(
            "unknown_type", "a task has a type the app does not have",
            [export.ref(task) for task in kept if task["task_type"] not in KNOWN_TYPES],
        ),
        *_stop(
            "blank_title", "a task's title is blank, which the app refuses",
            [export.ref(task) for task in kept if not task["title"].strip()],
        ),
        *_stop(
            "blank_comment", "a comment's body is blank, which the app refuses",
            [export.ref(task) for task in kept if task["task_id"] in task_ids_with_comment],
        ),
        *_stop(
            "closed_without_close_time",
            "a task's status and its close time disagree, which the app refuses",
            [export.ref(task) for task in kept
             if (task["status"] == "closed") != (task["closed_at"] is not None)],
        ),
        *_stop(
            "counter_behind_numbers", "the saved task counter would number a task again",
            [str(counter)] if numbers and (counter is None or counter <= max(numbers)) else [],
        ),
    ]


# --- the destination (step 1) ---------------------------------------------------

READ_ONLY = frozenset({"tasks.read"})


@dataclass(frozen=True)
class Destination:
    """The installation the exported team's live grant names, granting only
    tasks.read, as the deployment's own grant-read key reads it."""

    team_id: str
    app_id: str
    installation_id: str
    generation: int


async def destination(grants: TeamGrantSource, *, team_id: str, app_id: str) -> Destination:
    """The team's active installation of this deployment, while it is read-only."""
    try:
        status = await grants.status_for(team_id)
    except GrantUnavailable as unavailable:
        raise MoveRefused(f"the installation authority is unavailable: {unavailable}") from unavailable
    if (status is None or status.state != "active"
            or status.installation_id is None or status.generation is None):
        raise MoveRefused(f"{team_id} has no active installation of {app_id}")
    if status.granted_scopes != READ_ONLY:
        raise MoveRefused(
            f"the installation for {team_id} grants {sorted(status.granted_scopes)}, not only tasks.read"
        )
    return Destination(
        team_id=team_id, app_id=app_id,
        installation_id=status.installation_id, generation=status.generation,
    )


# --- the moved dataset (step 4) -----------------------------------------------

# The app's tables in the order they are written and listed, each with its
# columns and their types, and the key that orders its rows.
TABLES: dict[str, tuple[tuple[tuple[str, str], ...], str]] = {
    "installations": ((("installation_id", "text"), ("next_number", "integer")), "installation_id"),
    "tasks": ((
        ("installation_id", "text"), ("number", "integer"), ("title", "text"), ("body", "text"),
        ("type", "text"), ("priority", "smallint"), ("status", "text"), ("labels", "text[]"),
        ("parent", "integer"), ("assignee", "text"), ("creator", "jsonb"), ("closer", "jsonb"),
        ("created_at", "timestamptz"), ("updated_at", "timestamptz"), ("closed_at", "timestamptz"),
    ), "number"),
    "claims": ((
        ("installation_id", "text"), ("number", "integer"), ("holder_team", "text"),
        ("holder_identity", "text"), ("holder_alias", "text"), ("revision", "bigint"),
        ("expires_at", "timestamptz"),
    ), "number"),
    "locks": ((
        ("installation_id", "text"), ("resource", "text"), ("holder_team", "text"),
        ("holder_identity", "text"), ("holder_alias", "text"), ("reason", "text"),
        ("revision", "bigint"), ("expires_at", "timestamptz"),
    ), "resource"),
    "comments": ((
        ("installation_id", "text"), ("number", "integer"), ("author", "jsonb"), ("body", "text"),
        ("created_at", "timestamptz"),
    ), "comment_id"),
    "dependencies": ((
        ("installation_id", "text"), ("number", "integer"), ("waits_on", "integer"),
    ), "number, waits_on"),
    "events": ((
        ("installation_id", "text"), ("number", "integer"), ("resource", "text"), ("kind", "text"),
        ("cause", "text"), ("actor", "jsonb"), ("detail", "jsonb"), ("occurred_at", "timestamptz"),
    ), "event_id"),
}
CAUSE = "board-move"
STATUS = {"open": "open", "in_progress": "open", "closed": "closed"}
TYPE = {"task": "task", "bug": "bug", "feature": "feature", "epic": "epic", "chore": "task"}


@dataclass
class Dataset:
    """The rows of one installation's dataset, table by table, and the source
    facts the app does not hold, which only the staging schema keeps."""

    rows: dict[str, list[dict[str, Any]]]
    source: list[dict[str, Any]]


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def dataset(export: Export, destination: Destination) -> Dataset:
    """The export as the rows of the exported team's installation: numbers and
    the saved counter kept, actors exactly as stored, no lease live, and every
    original the app has no field for in the task's import entry."""
    if destination.team_id != export.team_id:
        raise MoveRefused(f"the export is {export.team_id}'s, the destination {destination.team_id}'s")
    refused = stops(export)
    if refused:
        raise MoveRefused("the export has stops: " + ", ".join(stop.code for stop in refused))
    installation_id = destination.installation_id
    exported_at = _time(export.document["exported_at"])
    actor = {"app": destination.app_id}
    number_of = {task["task_id"]: task["task_number"] for task in export.kept}

    def event(kind: str, detail: dict[str, Any], *, number: int | None = None,
              resource: str | None = None) -> dict[str, Any]:
        return {
            "installation_id": installation_id, "number": number, "resource": resource,
            "kind": kind, "cause": CAUSE, "actor": actor, "detail": detail,
            "occurred_at": exported_at,
        }

    tasks, claims, events = [], [], []
    claims_by_ref: dict[str, list[dict[str, Any]]] = {}
    for claim in export.claims:
        claims_by_ref.setdefault(claim["task_ref"], []).append(claim)
    for task in export.kept:
        number, ref = task["task_number"], export.ref(task)
        closed = task["status"] == "closed"
        tasks.append({
            "installation_id": installation_id, "number": number, "title": task["title"],
            "body": task["description"], "type": TYPE[task["task_type"]],
            "priority": task["priority"], "status": STATUS[task["status"]],
            "labels": task["labels"],
            "parent": None if task["parent_task_id"] is None else number_of[task["parent_task_id"]],
            "assignee": task["assignee_alias"],
            "creator": {"alias": task["created_by_alias"], "did": task["created_by_did"]},
            "closer": {"alias": task["closed_by_alias"], "did": task["closed_by_did"]} if closed else None,
            "created_at": _time(task["created_at"]),
            "updated_at": _time(task["updated_at"] or task["created_at"]),
            "closed_at": _time(task["closed_at"]),
        })
        claims.append({
            "installation_id": installation_id, "number": number, "holder_team": None,
            "holder_identity": None, "holder_alias": None, "revision": 0, "expires_at": None,
        })
        detail: dict[str, Any] = {"task_id": task["task_id"], "ref": ref}
        if STATUS[task["status"]] != task["status"]:
            detail["status"] = task["status"]
        if TYPE[task["task_type"]] != task["task_type"]:
            detail["type"] = task["task_type"]
        if task["notes"]:
            detail["notes"] = task["notes"]
        if task["assignee_did"] is not None:
            detail["assignee_did"] = task["assignee_did"]
        if task["updated_at"] is None:
            detail["updated_at"] = None
        events.append(event("imported", detail, number=number))
        for claim in claims_by_ref.get(ref, []):
            events.append(event("claim_not_proven", {
                "summary": f"claimed by {claim['alias']} from workspace {claim['workspace_id']},"
                           " holder not proven",
                "claim_id": claim["id"], "alias": claim["alias"],
                "workspace_id": claim["workspace_id"], "human_name": claim["human_name"],
                "apex_task_ref": claim["apex_task_ref"], "claimed_at": claim["claimed_at"],
            }, number=number))
    for reservation in export.reservations:
        if reservation["live"]:
            events.append(event("lock_not_proven", {
                "summary": f"locked by {reservation['holder_alias']} from agent"
                           f" {reservation['holder_agent_id']}, holder not proven",
                **{key: reservation[key] for key in (
                    "holder_alias", "holder_agent_id", "acquired_at", "expires_at", "metadata_json",
                )},
            }, resource=reservation["resource_key"]))
    kept_ids = set(number_of)
    return Dataset(
        rows={
            "installations": [{"installation_id": installation_id,
                               "next_number": export.document["task_counter"]}],
            "tasks": tasks,
            "claims": claims,
            "locks": [],
            "comments": [
                {"installation_id": installation_id, "number": number_of[c["task_id"]],
                 "author": {"alias": c["author_alias"]}, "body": c["body"],
                 "created_at": _time(c["created_at"])}
                for c in export.comments if c["task_id"] in kept_ids
            ],
            "dependencies": sorted((
                {"installation_id": installation_id, "number": number_of[d["task_id"]],
                 "waits_on": number_of[d["depends_on_id"]]}
                for d in export.dependencies
                if d["task_id"] in kept_ids and d["depends_on_id"] in kept_ids
            ), key=lambda d: (d["number"], d["waits_on"])),
            "events": events,
        },
        source=[
            {"kind": "destination", **dataclasses.asdict(destination)},
            {"kind": "export", "team_id": export.team_id, "sha256": export.sha256,
             "exported_at": export.document["exported_at"],
             "last_write_at": export.document["last_write_at"]},
            {"kind": "root_task_counter", "next_number": export.document["root_task_counter"]},
            *({"kind": "deleted_task", "task_id": t["task_id"], "ref": export.ref(t),
               "number": t["task_number"], "deleted_at": t["deleted_at"]}
              for t in export.tasks if t["deleted_at"] is not None),
            *({"kind": "expired_lock", **r} for r in export.reservations if not r["live"]),
        ],
    )


# --- the canonical listing (step 5) -------------------------------------------

def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not listed")


def _line(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=_plain).encode() + b"\n"


def dataset_part(full: bytes) -> bytes:
    """A full listing's dataset rows: every line before its first source fact."""
    lines = full.splitlines(keepends=True)
    first_source = next(
        (i for i, line in enumerate(lines) if json.loads(line).get("table") == "source"), len(lines)
    )
    return b"".join(lines[:first_source])


def listing(data: Dataset, *, source: bool = True) -> bytes:
    """One line per row, table by table in a fixed order, then the ref-to-number
    map and, for an export or a staging schema, the source facts."""
    lines = [_line({"table": table, **row}) for table in TABLES for row in data.rows[table]]
    lines += [
        _line({"table": "ref", "ref": e["detail"]["ref"], "number": e["number"]})
        for e in data.rows["events"] if e["kind"] == "imported"
    ]
    if source:
        lines += [_line({"table": "source", **record}) for record in data.source]
    return b"".join(lines)


# --- the Tasks database ---------------------------------------------------------

SCHEMA_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,50}$")
SOURCE_TABLE = "board_move_source"
EXPORT_TABLE = "board_move_export"
LISTINGS_TABLE = "board_move_listings"


@dataclass(frozen=True)
class Target:
    """The Tasks database: its live schema, and beside it the staging schema."""

    dsn: str
    live_schema: str

    def __post_init__(self) -> None:
        if not SCHEMA_NAME.match(self.live_schema):
            raise ValueError(f"not a schema name: {self.live_schema!r}")

    @property
    def staging_schema(self) -> str:
        return f"{self.live_schema}_board_move"


@asynccontextmanager
async def _connected(dsn: str, schema: str) -> AsyncIterator[AsyncDatabaseManager]:
    # One statement or transaction at a time: one connection.
    db = AsyncDatabaseManager(
        DatabaseConfig(connection_string=dsn, schema=schema, min_connections=1, max_connections=1)
    )
    await db.connect()
    try:
        yield db
    finally:
        await db.disconnect()


async def _schema_exists(db: AsyncDatabaseManager | TransactionManager, schema: str) -> bool:
    return bool(await db.fetch_value("SELECT 1 FROM pg_namespace WHERE nspname = $1", schema))


def _record(row: Any, table: str) -> dict[str, Any]:
    record = {}
    for column, kind in TABLES[table][0]:
        value = row[column]
        if kind == "jsonb" and isinstance(value, str):
            value = json.loads(value)
        elif kind == "text[]" and value is not None:
            value = list(value)
        record[column] = value
    return record


def _table(name: str, schema: str | None) -> str:
    """A table of the connection's schema, or of the named one."""
    return f"{{{{tables.{name}}}}}" if schema is None else f"{{{{tables.{schema}.{name}}}}}"


async def _read(db: Any, installation_id: str, *, source: bool, schema: str | None = None) -> Dataset:
    rows = {}
    for table, (columns, order) in TABLES.items():
        names = ", ".join(column for column, _ in columns)
        fetched = await db.fetch_all(
            f"SELECT {names} FROM {_table(table, schema)} WHERE installation_id = $1 ORDER BY {order}",
            installation_id,
        )
        rows[table] = [_record(row, table) for row in fetched]
    records = []
    if source:
        fetched = await db.fetch_all(f"SELECT record FROM {_table(SOURCE_TABLE, schema)} ORDER BY seq")
        records = [json.loads(row["record"]) for row in fetched]
    return Dataset(rows=rows, source=records)


async def _write(tx: Any, data: Dataset) -> None:
    for table, (columns, _) in TABLES.items():
        names = ", ".join(column for column, _ in columns)
        typed = ", ".join(f"{column} {kind}" for column, kind in columns)
        await tx.execute(
            f"INSERT INTO {{{{tables.{table}}}}} ({names}) SELECT {names}"
            f" FROM ROWS FROM (jsonb_to_recordset($1::jsonb) AS ({typed})) WITH ORDINALITY AS r({names}, seq)"
            " ORDER BY seq",
            json.dumps(data.rows[table], default=_plain),
        )
    await tx.execute(
        f"INSERT INTO {{{{tables.{SOURCE_TABLE}}}}} (seq, record)"
        " SELECT seq, record FROM jsonb_array_elements($1::jsonb) WITH ORDINALITY AS r(record, seq)",
        json.dumps(data.source),
    )


async def read_export(target: Target) -> Export:
    """The export the operator loaded into the staging schema."""
    async with _connected(target.dsn, target.live_schema) as live:
        if not await _schema_exists(live, target.staging_schema):
            raise MoveRefused(f"{target.staging_schema} does not exist: load the export first")
        rows = await live.fetch_all(
            f"SELECT sha256, document FROM {_table(EXPORT_TABLE, target.staging_schema)}"
        )
    if len(rows) != 1:
        raise MoveRefused(f"{target.staging_schema} holds {len(rows)} exports, not one")
    text, recorded = rows[0]["document"], rows[0]["sha256"]
    if hashlib.sha256(text.encode()).hexdigest() != recorded:
        raise MoveRefused(f"the loaded export does not match its sha256 {recorded}")
    return Export(json.loads(text), sha256=recorded)


async def stage(target: Target, export: Export, destination: Destination) -> None:
    """Writes the export's dataset into the staging schema the export was
    loaded into, built by the app's own migrations; no member request reads or
    writes it. A failed stage discards the schema, export included."""
    data = dataset(export, destination)
    async with _connected(target.dsn, target.live_schema) as live:
        if not await _schema_exists(live, target.staging_schema):
            raise MoveRefused(f"{target.staging_schema} does not exist: load the export first")
        if await live.fetch_value("SELECT to_regclass($1)", f"{target.staging_schema}.installations"):
            raise MoveRefused(
                f"{target.staging_schema} is already staged: discard it, then load and stage a new export"
            )
    try:
        async with _connected(target.dsn, target.staging_schema) as staging:
            await AsyncMigrationManager(
                staging, migrations_path=str(MIGRATIONS_PATH), module_name="tasks"
            ).apply_pending_migrations()
            async with staging.transaction() as tx:
                await tx.execute(
                    f"CREATE TABLE {{{{tables.{SOURCE_TABLE}}}}}"
                    " (seq INTEGER PRIMARY KEY, record JSONB NOT NULL)"
                )
                await _write(tx, data)
    except BaseException:
        await discard(target)
        raise


async def _staged(db: Any, *, schema: str | None = None) -> Dataset:
    """The staging schema's one installation, with its source facts."""
    installations = await db.fetch_all(f"SELECT installation_id FROM {_table('installations', schema)}")
    if len(installations) != 1:
        raise MoveRefused(f"the staging schema holds {len(installations)} installations, not one")
    return await _read(db, installations[0]["installation_id"], source=True, schema=schema)


async def staged_listing(target: Target) -> bytes:
    # Through the live schema: connecting to a missing schema would create it.
    async with _connected(target.dsn, target.live_schema) as live:
        if not await _schema_exists(live, target.staging_schema):
            raise MoveRefused(f"{target.staging_schema} does not exist")
        if not await live.fetch_value("SELECT to_regclass($1)", f"{target.staging_schema}.installations"):
            raise MoveRefused(f"{target.staging_schema} is not staged")
        return listing(await _staged(live, schema=target.staging_schema))


def listing_digest(data: bytes) -> str:
    """The SHA-256 of a listing, as `shasum -a 256` prints it."""
    return hashlib.sha256(data).hexdigest()


def digest_line(data: bytes) -> str:
    """What a job's log says of a listing: enough to bind the stored bytes."""
    return f"sha256 {listing_digest(data)} bytes {len(data)} lines {data.count(b'\n')}"


async def store_listing(target: Target, name: str, data: bytes) -> str:
    """Keeps a listing in the staging schema, for the operator to fetch, and
    returns its digest line."""
    async with _connected(target.dsn, target.live_schema) as live:
        if not await _schema_exists(live, target.staging_schema):
            raise MoveRefused(f"{target.staging_schema} does not exist")
        table = _table(LISTINGS_TABLE, target.staging_schema)
        await live.execute(
            f"CREATE TABLE IF NOT EXISTS {table}"
            " (name TEXT PRIMARY KEY, listing BYTEA NOT NULL, sha256 TEXT NOT NULL)"
        )
        await live.execute(
            f"INSERT INTO {table} (name, listing, sha256) VALUES ($1, $2, $3)"
            " ON CONFLICT (name) DO UPDATE SET listing = EXCLUDED.listing, sha256 = EXCLUDED.sha256",
            name, data, listing_digest(data),
        )
    return digest_line(data)


async def discard(target: Target) -> bool:
    """Drops the staging schema; whether there was one."""
    async with _connected(target.dsn, target.live_schema) as live:
        existed = await _schema_exists(live, target.staging_schema)
        await live.execute(f'DROP SCHEMA IF EXISTS "{target.staging_schema}" CASCADE')
        return existed


NO_WRITERS_SQL = Path(__file__).with_name("no_writers.sql")
# The dataset's shape and every principal's write authority: one read-only
# statement whose rows are refusals. The operator's preflight runs this file.
WRITER_AUTHORITY_SQL = Path(__file__).with_name("writer_authority.sql")


async def live_listing(target: Target, installation_id: str) -> bytes:
    """The installation's live dataset, listed as its staging was, once
    ``no_writers.sql`` has shown that no other session is writing.

    It reads no grant and starts no app runtime. The installation id comes
    from the approved listing, whose every dataset row carries it, so another
    installation's listing can never equal it: a wrong id can only fail the
    comparison."""
    async with _connected(target.dsn, target.live_schema) as live:
        async with live.transaction() as tx:
            try:
                await tx.execute(NO_WRITERS_SQL.read_text(encoding="utf-8"))
            except (QueryError, asyncpg.PostgresError) as failed:
                raise MoveRefused(str(failed.__cause__ or failed)) from failed
            return listing(await _read(tx, installation_id, source=False))


async def open_staged(
    target: Target, destination: Destination, *, listing_sha256: str,
    current: Callable[[], Awaitable[Destination]],
) -> None:
    """Copies the staged rows into the live tables and drops the staging schema,
    in one transaction: only the staging contents whose listing the verifier
    approved, staged for this destination, into an installation with no live rows.

    ``current`` reads the team's grant afresh. It is read again once the
    staging locks are held, so an installation change made while open waited
    for them refuses it. A change after that read and before the commit is
    outside this transaction: the operator makes no installation change while
    open runs."""
    staging = target.staging_schema
    async with _connected(target.dsn, target.live_schema) as live:
        async with live.transaction() as tx:
            if not await _schema_exists(tx, staging):
                raise MoveRefused(f"{staging} does not exist")
            # Nothing changes the staged rows between the digest and the copy.
            for table in (*TABLES, SOURCE_TABLE):
                await tx.execute(f"LOCK TABLE {{{{tables.{staging}.{table}}}}} IN EXCLUSIVE MODE")
            now = await current()
            if now != destination:
                raise MoveRefused(f"the installation changed while open waited: {now}, not {destination}")
            staged = await _staged(tx, schema=staging)
            if listing_digest(listing(staged)) != listing_sha256:
                raise MoveRefused(f"{staging} does not list as the approved listing {listing_sha256}")
            if staged.source[0] != {"kind": "destination", **dataclasses.asdict(destination)}:
                raise MoveRefused(f"{staging} was staged for {staged.source[0]}, not {destination}")
            for table in TABLES:
                if await tx.fetch_value(
                    f"SELECT 1 FROM {{{{tables.{table}}}}} WHERE installation_id = $1 LIMIT 1",
                    destination.installation_id,
                ):
                    raise MoveRefused(f"the installation already has live {table}")
            for table, (columns, order) in TABLES.items():
                names = ", ".join(column for column, _ in columns)
                await tx.execute(
                    f"INSERT INTO {{{{tables.{table}}}}} ({names}) SELECT {names}"
                    f" FROM {{{{tables.{staging}.{table}}}}} WHERE installation_id = $1 ORDER BY {order}",
                    destination.installation_id,
                )
            await tx.execute(f'DROP SCHEMA "{staging}" CASCADE')

