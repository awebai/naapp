"""One installation's tasks, claims and locks in PostgreSQL.

Every operation runs in one transaction. It locks the rows it judges, always in
the same order (the installation row, then task and claim rows by ascending
number, then lock rows), reads the time after those lock waits, asks
:mod:`aweb_tasks.decisions` for the verdict and applies it in the same
transaction, with one event row per change and the app events the change
emits. Reading a lease's liveness writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aweb_naapp.runtime.event_emit import AppEvents
from aweb_naapp.runtime.team_auth import Principal
from pgdbm.core import TransactionManager

from aweb_tasks import decisions
from aweb_tasks.decisions import (
    Closing,
    Holder,
    Lease,
    LeaseKind,
    Refusal,
    SeenClaim,
    Task,
    TaskStatus,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskRefused(Exception):
    """A decision's refusal, raised out of the transaction so nothing commits."""

    def __init__(self, refusal: Refusal) -> None:
        self.refusal = refusal
        super().__init__(refusal.message)


def _settle[T](verdict: T | Refusal) -> T:
    if isinstance(verdict, Refusal):
        raise TaskRefused(verdict)
    return verdict


def _check(refusal: Refusal | None) -> None:
    if refusal is not None:
        raise TaskRefused(refusal)


def actor_of(principal: Principal) -> dict[str, Any]:
    """The actor as the request verification established it: the verified
    principal, its team, the certificate presented and the alias as supplied."""
    return {
        "team_id": principal.team_id,
        "did_key": principal.did_key,
        "did_aw": principal.did_aw if principal.did_aw_registry_verified else None,
        "certificate_id": principal.certificate_id,
        "alias": principal.alias,
    }


def _lease(row: Any) -> Lease:
    holder = None
    if row["holder_identity"] is not None:
        holder = Holder(team_id=row["holder_team"], identity=row["holder_identity"])
    return Lease(holder=holder, revision=row["revision"], expires_at=row["expires_at"])


def _holder_json(lease: Lease, alias: str | None, now: datetime) -> dict[str, Any] | None:
    """A lease as answered: only a live one has a holder."""
    live = lease.live_holder(now)
    if live is None:
        return None
    assert lease.expires_at is not None
    return {
        "holder": live.identity,
        "holder_alias": alias,
        "revision": lease.revision,
        "expires_at": lease.expires_at.isoformat(),
    }


@dataclass(frozen=True)
class Fields:
    """The task fields an update may set; None leaves a field as it is."""

    title: str | None = None
    body: str | None = None
    type: str | None = None
    priority: int | None = None
    labels: Sequence[str] | None = None
    parent: int | None = None
    assignee: str | None = None
    status: TaskStatus | None = None


class TaskStore:
    """The tasks of one installation, acted on by one verified principal."""

    def __init__(
        self,
        db: Any,
        *,
        installation_id: str,
        principal: Principal,
        events: AppEvents,
        app_id: str,
        clock: Clock = utc_now,
    ) -> None:
        self.db = db
        self.installation_id = installation_id
        self.principal = principal
        self.caller = decisions.holder_of(principal)
        self.actor = actor_of(principal)
        self.events = events
        self.app_id = app_id
        self.clock = clock

    # --- locking and facts ---------------------------------------------------

    async def _lock_installation(self, tx: TransactionManager) -> int:
        await tx.execute(
            "INSERT INTO {{tables.installations}} (installation_id, next_number) VALUES ($1, 1)"
            " ON CONFLICT (installation_id) DO NOTHING",
            self.installation_id,
        )
        return await tx.fetch_value(
            "SELECT next_number FROM {{tables.installations}} WHERE installation_id = $1 FOR UPDATE",
            self.installation_id,
        )

    async def _lock_tasks(self, tx: TransactionManager, numbers: Sequence[int]) -> dict[int, Any]:
        """Task rows with their claim rows, locked by ascending number."""
        rows = await tx.fetch_all(
            "SELECT t.*, c.holder_team, c.holder_identity, c.holder_alias, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1 AND t.number = ANY($2::int[])"
            " ORDER BY t.number FOR UPDATE",
            self.installation_id, sorted(set(numbers)),
        )
        return {row["number"]: row for row in rows}

    def _task(self, row: Any | None) -> Task | None:
        if row is None:
            return None
        return Task(
            number=row["number"], installation_id=row["installation_id"],
            status=TaskStatus(row["status"]), claim=_lease(row),
        )

    async def _descendants(self, tx: TransactionManager, number: int) -> list[int]:
        rows = await tx.fetch_all(
            "WITH RECURSIVE d(number) AS ("
            " SELECT number FROM {{tables.tasks}} WHERE installation_id = $1 AND parent = $2"
            " UNION SELECT t.number FROM {{tables.tasks}} t JOIN d ON t.parent = d.number"
            " WHERE t.installation_id = $1)"
            " SELECT number FROM d ORDER BY number",
            self.installation_id, number,
        )
        return [row["number"] for row in rows]

    async def _record(
        self, tx: TransactionManager, kind: str, cause: str, now: datetime, *,
        number: int | None = None, resource: str | None = None, detail: dict[str, Any] | None = None,
    ) -> None:
        await tx.execute(
            "INSERT INTO {{tables.events}} (installation_id, number, resource, kind, cause, actor, detail, occurred_at)"
            " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)",
            self.installation_id, number, resource, kind, cause,
            json.dumps(self.actor), json.dumps(detail or {}), now,
        )

    async def _emit(self, tx: TransactionManager, event: str, resource_ref: str, payload: dict[str, str]) -> None:
        await self.events.emit(
            tx, team_id=self.principal.team_id, event_type=f"{self.app_id}/{event}",
            resource_ref=resource_ref, delivery_intent="wake", payload=payload,
        )

    async def _write_claim(
        self, tx: TransactionManager, number: int, lease: Lease, alias: str | None
    ) -> None:
        await tx.execute(
            "UPDATE {{tables.claims}} SET holder_team = $3, holder_identity = $4, holder_alias = $5,"
            " revision = $6, expires_at = $7 WHERE installation_id = $1 AND number = $2",
            self.installation_id, number,
            None if lease.holder is None else lease.holder.team_id,
            None if lease.holder is None else lease.holder.identity,
            None if lease.holder is None else alias, lease.revision, lease.expires_at,
        )

    async def _renew_on_write(self, tx: TransactionManager, row: Any, now: datetime, cause: str) -> None:
        """The holder's write to its task renews its claim, recorded as a change
        of its own; anyone else's write leaves the claim as it is."""
        lease = _lease(row)
        renewed = decisions.renew_on_write(lease, self.caller, now)
        if renewed != lease:
            await self._write_claim(tx, row["number"], renewed, row["holder_alias"])
            await self._record(tx, "claim_renewed", cause, now, number=row["number"], detail={
                "revision": renewed.revision,
            })

    async def _apply_closing(self, tx: TransactionManager, closing: Closing, cause: str, now: datetime) -> None:
        for ended in closing.ended_claims:
            await self._write_claim(tx, ended.number, ended.lease, None)
            await self._record(tx, "claim_ended", cause, now, number=ended.number, detail={
                "previous_holder": ended.previous.identity, "revision": ended.lease.revision,
            })
            await self._emit(tx, "task.claim", str(ended.number), {"revision": str(ended.lease.revision)})
        for number in closing.closed:
            await tx.execute(
                "UPDATE {{tables.tasks}} SET status = 'closed', closer = $3::jsonb, closed_at = $4, updated_at = $4"
                " WHERE installation_id = $1 AND number = $2",
                self.installation_id, number, json.dumps(self.actor), now,
            )
            await self._record(tx, "closed", cause, now, number=number)
            await self._emit(tx, "task.status", str(number), {"status": "closed"})

    # --- reads ---------------------------------------------------------------

    async def _answer(self, tx: Any, number: int, now: datetime) -> dict[str, Any]:
        row = await tx.fetch_one(
            "SELECT t.*, c.holder_team, c.holder_identity, c.holder_alias, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1 AND t.number = $2",
            self.installation_id, number,
        )
        _check(decisions.inside(self.installation_id, self._task(row)))
        waits_on = await tx.fetch_all(
            "SELECT waits_on FROM {{tables.dependencies}} WHERE installation_id = $1 AND number = $2"
            " ORDER BY waits_on",
            self.installation_id, number,
        )
        return {**self._summary(row, now), "waits_on": [r["waits_on"] for r in waits_on]}

    def _summary(self, row: Any, now: datetime) -> dict[str, Any]:
        return {
            "number": row["number"],
            "title": row["title"],
            "body": row["body"],
            "type": row["type"],
            "priority": row["priority"],
            "status": row["status"],
            "labels": list(row["labels"]),
            "parent": row["parent"],
            "assignee": row["assignee"],
            "creator": _json(row["creator"]),
            "closer": _json(row["closer"]),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "closed_at": None if row["closed_at"] is None else row["closed_at"].isoformat(),
            "claim": _holder_json(_lease(row), row["holder_alias"], now),
        }

    async def show(self, number: int) -> dict[str, Any]:
        return await self._answer(self.db, number, self.clock())

    async def list_tasks(
        self, *, status: TaskStatus | None = None, label: str | None = None, assignee: str | None = None,
        parent: int | None = None, type: str | None = None, priority: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT t.*, c.holder_team, c.holder_identity, c.holder_alias, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1"
            " AND ($2::text IS NULL OR t.status = $2) AND ($3::text IS NULL OR $3 = ANY(t.labels))"
            " AND ($4::text IS NULL OR t.assignee = $4) AND ($5::int IS NULL OR t.parent = $5)"
            " AND ($6::text IS NULL OR t.type = $6) AND ($7::int IS NULL OR t.priority = $7)"
            " ORDER BY t.number",
            self.installation_id, None if status is None else str(status), label, assignee, parent, type, priority,
        )
        now = self.clock()
        return [self._summary(row, now) for row in rows]

    async def ready(self) -> list[dict[str, Any]]:
        """Open tasks with nothing open to wait on and no live claim."""
        now = self.clock()
        rows = await self.db.fetch_all(
            "SELECT t.*, c.holder_team, c.holder_identity, c.holder_alias, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1 AND t.status = 'open'"
            " AND (c.holder_identity IS NULL OR c.expires_at <= $2)"
            " AND NOT EXISTS (SELECT 1 FROM {{tables.dependencies}} d JOIN {{tables.tasks}} w"
            "  ON w.installation_id = d.installation_id AND w.number = d.waits_on"
            "  WHERE d.installation_id = t.installation_id AND d.number = t.number AND w.status <> 'closed')"
            " ORDER BY t.priority, t.number",
            self.installation_id, now,
        )
        return [self._summary(row, now) for row in rows]

    async def active(self, *, holder: str | None = None) -> list[dict[str, Any]]:
        """Tasks with a live claim, optionally one holder's."""
        now = self.clock()
        rows = await self.db.fetch_all(
            "SELECT t.*, c.holder_team, c.holder_identity, c.holder_alias, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1 AND c.holder_identity IS NOT NULL AND c.expires_at > $2"
            " AND ($3::text IS NULL OR c.holder_identity = $3)"
            " ORDER BY t.number",
            self.installation_id, now, holder,
        )
        return [self._summary(row, now) for row in rows]

    async def blocked(self) -> list[dict[str, Any]]:
        """Tasks marked blocked, and open tasks waiting on a task that is not closed."""
        now = self.clock()
        rows = await self.db.fetch_all(
            "SELECT t.*, c.holder_team, c.holder_identity, c.holder_alias, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1 AND (t.status = 'blocked' OR (t.status = 'open'"
            " AND EXISTS (SELECT 1 FROM {{tables.dependencies}} d JOIN {{tables.tasks}} w"
            "  ON w.installation_id = d.installation_id AND w.number = d.waits_on"
            "  WHERE d.installation_id = t.installation_id AND d.number = t.number AND w.status <> 'closed')))"
            " ORDER BY t.number",
            self.installation_id,
        )
        return [self._summary(row, now) for row in rows]

    async def comments(self, number: int) -> list[dict[str, Any]]:
        await self._exists(number)
        rows = await self.db.fetch_all(
            "SELECT comment_id, author, body, created_at FROM {{tables.comments}}"
            " WHERE installation_id = $1 AND number = $2 ORDER BY comment_id",
            self.installation_id, number,
        )
        return [
            {"author": _json(r["author"]), "body": r["body"], "created_at": r["created_at"].isoformat()}
            for r in rows
        ]

    async def history(self, number: int) -> list[dict[str, Any]]:
        await self._exists(number)
        rows = await self.db.fetch_all(
            "SELECT kind, cause, actor, detail, occurred_at FROM {{tables.events}}"
            " WHERE installation_id = $1 AND number = $2 ORDER BY event_id",
            self.installation_id, number,
        )
        return [
            {"kind": r["kind"], "cause": r["cause"], "actor": _json(r["actor"]),
             "detail": _json(r["detail"]), "occurred_at": r["occurred_at"].isoformat()}
            for r in rows
        ]

    async def locks(self, *, holder: str | None = None) -> list[dict[str, Any]]:
        now = self.clock()
        rows = await self.db.fetch_all(
            "SELECT * FROM {{tables.locks}} WHERE installation_id = $1"
            " AND holder_identity IS NOT NULL AND expires_at > $2 AND ($3::text IS NULL OR holder_identity = $3)"
            " ORDER BY resource",
            self.installation_id, now, holder,
        )
        return [{"resource": r["resource"], "reason": r["reason"], **(_holder_json(_lease(r), r["holder_alias"], now) or {})}
                for r in rows]

    async def _exists(self, number: int) -> None:
        row = await self.db.fetch_one(
            "SELECT t.installation_id, t.number, t.status, c.holder_team, c.holder_identity, c.revision, c.expires_at"
            " FROM {{tables.tasks}} t JOIN {{tables.claims}} c USING (installation_id, number)"
            " WHERE t.installation_id = $1 AND t.number = $2",
            self.installation_id, number,
        )
        _check(decisions.inside(self.installation_id, self._task(row)))

    # --- task writes ---------------------------------------------------------

    async def create(
        self, *, title: str, body: str, type: str, priority: int, labels: Sequence[str],
        parent: int | None, assignee: str | None,
    ) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            number = await self._lock_installation(tx)
            if parent is not None:
                rows = await self._lock_tasks(tx, [parent])
                _check(decisions.inside(self.installation_id, self._task(rows.get(parent))))
            now = self.clock()
            await tx.execute(
                "UPDATE {{tables.installations}} SET next_number = $2 WHERE installation_id = $1",
                self.installation_id, number + 1,
            )
            await tx.execute(
                "INSERT INTO {{tables.tasks}} (installation_id, number, title, body, type, priority, status,"
                " labels, parent, assignee, creator, created_at, updated_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, 'open', $7, $8, $9, $10::jsonb, $11, $11)",
                self.installation_id, number, title, body, type, priority, list(labels), parent, assignee,
                json.dumps(self.actor), now,
            )
            await tx.execute(
                "INSERT INTO {{tables.claims}} (installation_id, number, revision) VALUES ($1, $2, 0)",
                self.installation_id, number,
            )
            await self._record(tx, "created", "create", now, number=number, detail={"title": title})
            await self._emit(tx, "task.status", str(number), {"status": "open"})
            return await self._answer(tx, number, now)

    async def update(self, number: int, fields: Fields) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            # A parent or a close walks the installation's structure.
            structural = fields.parent is not None or fields.status is TaskStatus.CLOSED
            if structural:
                await self._lock_installation(tx)
            descendants = await self._descendants(tx, number) if fields.status is TaskStatus.CLOSED else []
            wanted = [number, *descendants]
            if fields.parent is not None:
                wanted.append(fields.parent)
            rows = await self._lock_tasks(tx, wanted)
            now = self.clock()
            task = self._task(rows.get(number))
            _check(decisions.inside(self.installation_id, task))
            assert task is not None
            if fields.parent is not None:
                parent_of = {
                    r["number"]: r["parent"] for r in await tx.fetch_all(
                        "SELECT number, parent FROM {{tables.tasks}} WHERE installation_id = $1",
                        self.installation_id,
                    )
                }
                _check(decisions.set_parent(
                    self.installation_id, task, self._task(rows.get(fields.parent)), parent_of
                ))
            closing = decisions.NOTHING_CLOSED
            if fields.status is not None:
                closing = _settle(decisions.update_status(
                    self.installation_id, task, fields.status,
                    [t for d in descendants if (t := self._task(rows.get(d))) is not None],
                    self.caller, now,
                ))
            row = rows[number]
            changed = {
                name: value for name, value in (
                    ("title", fields.title), ("body", fields.body), ("type", fields.type),
                    ("priority", fields.priority), ("labels", None if fields.labels is None else list(fields.labels)),
                    ("parent", fields.parent), ("assignee", fields.assignee),
                ) if value is not None
            }
            for name, value in changed.items():
                await tx.execute(
                    f"UPDATE {{{{tables.tasks}}}} SET {name} = $3, updated_at = $4"
                    " WHERE installation_id = $1 AND number = $2",
                    self.installation_id, number, value, now,
                )
            if changed:
                await self._record(tx, "updated", "update", now, number=number, detail=changed)
            reopened = (
                fields.status is not None and fields.status is not TaskStatus.CLOSED and fields.status != task.status
            )
            if reopened:
                assert fields.status is not None
                await self._set_open_status(tx, number, fields.status, now, cause="update")
            # An update that changes nothing is no write. A close releases the
            # holder's claim; renewing it first would spend its revision twice.
            if (changed or reopened) and number not in closing.closed:
                await self._renew_on_write(tx, row, now, "update")
            await self._apply_closing(tx, closing, "update", now)
            return await self._answer(tx, number, now)

    async def _set_open_status(self, tx: TransactionManager, number: int, status: TaskStatus, now: datetime, *, cause: str) -> None:
        """A status other than closed: open or blocked, also out of closed (a reopen)."""
        await tx.execute(
            "UPDATE {{tables.tasks}} SET status = $3, closer = NULL, closed_at = NULL, updated_at = $4"
            " WHERE installation_id = $1 AND number = $2",
            self.installation_id, number, str(status), now,
        )
        await self._record(tx, "status", cause, now, number=number, detail={"status": str(status)})
        await self._emit(tx, "task.status", str(number), {"status": str(status)})

    async def close(
        self, number: int, *, with_children: bool, overrides: bool = False, presented_revision: int | None = None
    ) -> dict[str, Any]:
        cause = "close-claimed" if overrides else "close"
        # What the caller's override is judged against: each descendant's claim as
        # read before the lock wait.
        seen: dict[int, SeenClaim] = {}
        if overrides and with_children:
            before = self.clock()
            for r in await self.db.fetch_all(
                "WITH RECURSIVE d(number) AS ("
                " SELECT number FROM {{tables.tasks}} WHERE installation_id = $1 AND parent = $2"
                " UNION SELECT t.number FROM {{tables.tasks}} t JOIN d ON t.parent = d.number"
                " WHERE t.installation_id = $1)"
                " SELECT c.* FROM {{tables.claims}} c JOIN d USING (number) WHERE c.installation_id = $1",
                self.installation_id, number,
            ):
                lease = _lease(r)
                seen[r["number"]] = SeenClaim(revision=lease.revision, holder=lease.live_holder(before))
        async with self.db.transaction() as tx:
            await self._lock_installation(tx)
            descendants = await self._descendants(tx, number)
            rows = await self._lock_tasks(tx, [number, *descendants])
            now = self.clock()
            task = self._task(rows.get(number))
            facts = [t for d in descendants if (t := self._task(rows.get(d))) is not None]
            if overrides:
                closing = _settle(decisions.close_claimed(
                    self.installation_id, task, facts, self.caller, now,
                    presented_revision=presented_revision, seen_claims=seen, with_children=with_children,
                ))
            else:
                closing = _settle(decisions.close(
                    self.installation_id, task, facts, self.caller, now, with_children=with_children,
                ))
            await self._apply_closing(tx, closing, cause, now)
            return await self._answer(tx, number, now)

    async def reopen(self, number: int) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            rows = await self._lock_tasks(tx, [number])
            now = self.clock()
            _check(decisions.reopen(self.installation_id, self._task(rows.get(number))))
            await self._set_open_status(tx, number, TaskStatus.OPEN, now, cause="reopen")
            return await self._answer(tx, number, now)

    async def comment(self, number: int, body: str) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            rows = await self._lock_tasks(tx, [number])
            now = self.clock()
            row = rows.get(number)
            _check(decisions.inside(self.installation_id, self._task(row)))
            await tx.execute(
                "INSERT INTO {{tables.comments}} (installation_id, number, author, body, created_at)"
                " VALUES ($1, $2, $3::jsonb, $4, $5)",
                self.installation_id, number, json.dumps(self.actor), body, now,
            )
            await self._record(tx, "commented", "comment-add", now, number=number)
            await self._renew_on_write(tx, row, now, "comment-add")
            return {"author": self.actor, "body": body, "created_at": now.isoformat()}

    async def add_dependency(self, number: int, waits_on: int) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            await self._lock_installation(tx)
            rows = await self._lock_tasks(tx, [number, waits_on])
            now = self.clock()
            edges: dict[int, set[int]] = {}
            for r in await tx.fetch_all(
                "SELECT number, waits_on FROM {{tables.dependencies}} WHERE installation_id = $1",
                self.installation_id,
            ):
                edges.setdefault(r["number"], set()).add(r["waits_on"])
            _check(decisions.add_dependency(
                self.installation_id, self._task(rows.get(number)), self._task(rows.get(waits_on)), edges
            ))
            added = await tx.fetch_value(
                "INSERT INTO {{tables.dependencies}} (installation_id, number, waits_on) VALUES ($1, $2, $3)"
                " ON CONFLICT DO NOTHING RETURNING 1",
                self.installation_id, number, waits_on,
            )
            if added is not None:
                await self._record(tx, "dependency_added", "dep-add", now, number=number, detail={"waits_on": waits_on})
                await self._renew_on_write(tx, rows[number], now, "dep-add")
            return await self._answer(tx, number, now)

    async def remove_dependency(self, number: int, waits_on: int) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            await self._lock_installation(tx)
            rows = await self._lock_tasks(tx, [number, waits_on])
            now = self.clock()
            _check(decisions.remove_dependency(
                self.installation_id, self._task(rows.get(number)), self._task(rows.get(waits_on))
            ))
            removed = await tx.fetch_value(
                "DELETE FROM {{tables.dependencies}} WHERE installation_id = $1 AND number = $2 AND waits_on = $3"
                " RETURNING 1",
                self.installation_id, number, waits_on,
            )
            if removed is None:
                # The edge itself is missing: there is no dependency to remove.
                raise TaskRefused(Refusal(code=decisions.Code.NOT_FOUND, message="No such dependency"))
            await self._record(tx, "dependency_removed", "dep-remove", now, number=number, detail={"waits_on": waits_on})
            await self._renew_on_write(tx, rows[number], now, "dep-remove")
            return await self._answer(tx, number, now)

    # --- claims --------------------------------------------------------------

    async def _seen_claim_revision(self, number: int) -> int | None:
        """The claim's revision before the lock wait, which the claim presents."""
        row = await self.db.fetch_one(
            "SELECT revision FROM {{tables.claims}} WHERE installation_id = $1 AND number = $2",
            self.installation_id, number,
        )
        return None if row is None else row["revision"]

    async def claim(self, number: int) -> dict[str, Any]:
        seen = await self._seen_claim_revision(number)
        async with self.db.transaction() as tx:
            rows = await self._lock_tasks(tx, [number])
            now = self.clock()
            lease = _settle(decisions.claim(
                self.installation_id, self._task(rows.get(number)), self.caller, now, presented_revision=seen,
            ))
            await self._claim_changed(tx, number, rows[number], lease, "claimed", "claim", now)
            return await self._answer(tx, number, now)

    async def renew_claim(self, number: int, presented_revision: int | None) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            rows = await self._lock_tasks(tx, [number])
            now = self.clock()
            row = self._present(rows.get(number))
            lease = _settle(decisions.renew(
                LeaseKind.CLAIM, _lease(row), self.caller, now, presented_revision=presented_revision
            ))
            await self._write_claim(tx, number, lease, row["holder_alias"])
            await self._record(tx, "claim_renewed", "claim-renew", now, number=number, detail={"revision": lease.revision})
            return await self._answer(tx, number, now)

    async def release(self, number: int, presented_revision: int | None) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            rows = await self._lock_tasks(tx, [number])
            now = self.clock()
            row = self._present(rows.get(number))
            lease = _settle(decisions.release(
                LeaseKind.CLAIM, _lease(row), self.caller, now, presented_revision=presented_revision
            ))
            await self._claim_changed(tx, number, row, lease, "released", "release", now)
            return await self._answer(tx, number, now)

    async def takeover(self, number: int, presented_revision: int) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            rows = await self._lock_tasks(tx, [number])
            now = self.clock()
            task = self._task(rows.get(number))
            lease = _settle(decisions.takeover(
                self.installation_id, task, self.caller, now, presented_revision=presented_revision
            ))
            assert task is not None and task.claim.holder is not None
            await self._claim_changed(
                tx, number, rows[number], lease, "taken_over", "takeover", now,
                detail={"previous_holder": task.claim.holder.identity},
            )
            return await self._answer(tx, number, now)

    def _present(self, row: Any | None) -> Any:
        _check(decisions.inside(self.installation_id, self._task(row)))
        return row

    async def _claim_changed(
        self, tx: TransactionManager, number: int, row: Any, lease: Lease, kind: str, cause: str, now: datetime,
        *, detail: dict[str, Any] | None = None,
    ) -> None:
        previous = _lease(row)
        alias = self.principal.alias if lease.holder is not None else None
        if lease.holder is not None and previous.live_holder(now) == lease.holder:
            alias = row["holder_alias"]
        await self._write_claim(tx, number, lease, alias)
        await self._record(tx, kind, cause, now, number=number, detail={"revision": lease.revision, **(detail or {})})
        if previous.live_holder(now) != lease.holder:
            await self._emit(tx, "task.claim", str(number), {"revision": str(lease.revision)})

    # --- locks ---------------------------------------------------------------

    async def _lock_resource(self, tx: TransactionManager, resource: str) -> Any:
        await self._lock_installation_row_exists(tx)
        await tx.execute(
            "INSERT INTO {{tables.locks}} (installation_id, resource, revision) VALUES ($1, $2, 0)"
            " ON CONFLICT (installation_id, resource) DO NOTHING",
            self.installation_id, resource,
        )
        return await tx.fetch_one(
            "SELECT * FROM {{tables.locks}} WHERE installation_id = $1 AND resource = $2 FOR UPDATE",
            self.installation_id, resource,
        )

    async def _lock_installation_row_exists(self, tx: TransactionManager) -> None:
        await tx.execute(
            "INSERT INTO {{tables.installations}} (installation_id, next_number) VALUES ($1, 1)"
            " ON CONFLICT (installation_id) DO NOTHING",
            self.installation_id,
        )

    async def _write_lock(self, tx: TransactionManager, resource: str, lease: Lease, alias: str | None, reason: str | None) -> None:
        await tx.execute(
            "UPDATE {{tables.locks}} SET holder_team = $3, holder_identity = $4, holder_alias = $5,"
            " reason = $6, revision = $7, expires_at = $8 WHERE installation_id = $1 AND resource = $2",
            self.installation_id, resource,
            None if lease.holder is None else lease.holder.team_id,
            None if lease.holder is None else lease.holder.identity,
            None if lease.holder is None else alias,
            None if lease.holder is None else reason, lease.revision, lease.expires_at,
        )

    async def _lock_answer(self, tx: Any, resource: str, now: datetime) -> dict[str, Any]:
        r = await tx.fetch_one(
            "SELECT * FROM {{tables.locks}} WHERE installation_id = $1 AND resource = $2",
            self.installation_id, resource,
        )
        return {"resource": resource, "reason": r["reason"], "revision": r["revision"],
                "lock": _holder_json(_lease(r), r["holder_alias"], now)}

    async def _lock_changed(
        self, tx: TransactionManager, resource: str, row: Any, lease: Lease, kind: str, cause: str,
        now: datetime, *, reason: str | None, detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = _lease(row)
        kept = lease.holder is not None and previous.live_holder(now) == lease.holder
        await self._write_lock(
            tx, resource, lease, row["holder_alias"] if kept else self.principal.alias,
            reason if not kept or reason is not None else row["reason"],
        )
        await self._record(tx, kind, cause, now, resource=resource, detail={"revision": lease.revision, **(detail or {})})
        if previous.live_holder(now) != lease.holder:
            await self._emit(tx, "lock", resource, {"revision": str(lease.revision)})
        return await self._lock_answer(tx, resource, now)

    async def lock(self, resource: str, reason: str | None) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT revision FROM {{tables.locks}} WHERE installation_id = $1 AND resource = $2",
            self.installation_id, resource,
        )
        seen = None if row is None else row["revision"]
        async with self.db.transaction() as tx:
            row = await self._lock_resource(tx, resource)
            now = self.clock()
            lease = _settle(decisions.lock(_lease(row), self.caller, now, presented_revision=seen))
            return await self._lock_changed(tx, resource, row, lease, "locked", "lock", now, reason=reason)

    async def renew_lock(self, resource: str, presented_revision: int | None) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            row = await self._lock_resource(tx, resource)
            now = self.clock()
            lease = _settle(decisions.renew(
                LeaseKind.LOCK, _lease(row), self.caller, now, presented_revision=presented_revision
            ))
            return await self._lock_changed(tx, resource, row, lease, "lock_renewed", "lock-renew", now, reason=None)

    async def unlock(self, resource: str, presented_revision: int | None) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            row = await self._lock_resource(tx, resource)
            now = self.clock()
            lease = _settle(decisions.release(
                LeaseKind.LOCK, _lease(row), self.caller, now, presented_revision=presented_revision
            ))
            return await self._lock_changed(tx, resource, row, lease, "unlocked", "unlock", now, reason=None)

    async def break_lock(self, resource: str, presented_revision: int) -> dict[str, Any]:
        async with self.db.transaction() as tx:
            row = await self._lock_resource(tx, resource)
            now = self.clock()
            previous = _lease(row)
            lease = _settle(decisions.break_lock(previous, self.caller, now, presented_revision=presented_revision))
            assert previous.holder is not None
            return await self._lock_changed(
                tx, resource, row, lease, "broken", "break", now, reason=None,
                detail={"previous_holder": previous.holder.identity},
            )

    async def release_all(self) -> dict[str, Any]:
        """Every claim and lock the caller holds, released, explicitly."""
        async with self.db.transaction() as tx:
            claim_rows = await tx.fetch_all(
                "SELECT * FROM {{tables.claims}} WHERE installation_id = $1 AND holder_team = $2"
                " AND holder_identity = $3 ORDER BY number FOR UPDATE",
                self.installation_id, self.caller.team_id, self.caller.identity,
            )
            lock_rows = await tx.fetch_all(
                "SELECT * FROM {{tables.locks}} WHERE installation_id = $1 AND holder_team = $2"
                " AND holder_identity = $3 ORDER BY resource FOR UPDATE",
                self.installation_id, self.caller.team_id, self.caller.identity,
            )
            now = self.clock()
            claims = decisions.release_all({r["number"]: _lease(r) for r in claim_rows}, self.caller, now)
            locks = decisions.release_all({r["resource"]: _lease(r) for r in lock_rows}, self.caller, now)
            for number, lease in claims.items():
                await self._write_claim(tx, number, lease, None)
                await self._record(tx, "released", "release", now, number=number, detail={"revision": lease.revision})
                await self._emit(tx, "task.claim", str(number), {"revision": str(lease.revision)})
            for resource, lease in locks.items():
                await self._write_lock(tx, resource, lease, None, None)
                await self._record(tx, "unlocked", "release", now, resource=resource, detail={"revision": lease.revision})
                await self._emit(tx, "lock", resource, {"revision": str(lease.revision)})
            return {"released_claims": sorted(claims), "released_locks": sorted(locks)}


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value
