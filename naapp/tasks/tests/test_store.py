"""The store on real PostgreSQL: concurrent transitions on one lease, and the
app events each change enqueues.

Over HTTP, naapp-lib's installation fence admits one of a team's requests at a
time while another's transaction is open, so racing requests never reach the
store together. These tests race two stores directly. A transaction holds the
lease row until both are inside their own transactions and waiting on it, then
lets them through together: exactly one wins.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aweb_naapp.manifest import load_manifest
from aweb_naapp.outbox import RetryPolicy, apply_outbox_migrations
from aweb_naapp.runtime.event_emit import AppEvents
from aweb_naapp.runtime.team_auth import Principal
from pgdbm import AsyncMigrationManager

from aweb_tasks.app import MIGRATIONS_PATH
from aweb_tasks.config import TasksSettings
from aweb_tasks.store import TaskRefused, TaskStore

from .deployment import TEST_APP_ID, served_manifest, write_key_files

INSTALLATION = "installation-1"


def principal(alias: str) -> Principal:
    return Principal(
        team_id="team:example.test", did_key=f"did:key:z6Mk{alias}", did_aw=None, address=None,
        alias=alias, certificate_id=f"cert-{alias}", team_did_key="did:key:z6Mkteam",
    )


@pytest.fixture
async def db(test_db_factory: Any) -> Any:
    database = await test_db_factory.create_db(suffix="tasks_store")
    await AsyncMigrationManager(
        database, migrations_path=str(MIGRATIONS_PATH), module_name="tasks"
    ).apply_pending_migrations()
    return database


def store(db: Any, alias: str, clock: Any = None) -> TaskStore:
    events = AppEvents(app_id="tasks-test", origin=None, key=None, policy=RetryPolicy(stalled_after=timedelta(hours=1)))
    return TaskStore(
        db, installation_id=INSTALLATION, principal=principal(alias), events=events, app_id="tasks-test",
        **({} if clock is None else {"clock": clock}),
    )


async def waiting_on_locks(db: Any, count: int) -> None:
    """Until ``count`` backends of this database wait on a lock; at most 10 s."""
    for _ in range(200):
        waiting = await db.fetch_value(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND wait_event_type = 'Lock'"
        )
        if waiting >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{count} transitions never waited on the lease row")


async def race(db: Any, held_row: str, attempts: list[Any]) -> list[Any]:
    async with db.transaction() as tx:
        await tx.execute(held_row)
        racing = [asyncio.ensure_future(attempt) for attempt in attempts]
        await waiting_on_locks(db, len(attempts))
    return await asyncio.gather(*racing, return_exceptions=True)


def outcome(result: Any) -> str:
    if isinstance(result, TaskRefused):
        return str(result.refusal.code)
    assert not isinstance(result, BaseException), result
    return "won"


async def test_two_claims_at_once_give_one_claim(db: Any) -> None:
    await store(db, "alice").create(title="Contested", body="", type="task", priority=2, labels=[], parent=None, assignee=None)
    results = await race(
        db, "SELECT 1 FROM {{tables.claims}} WHERE number = 1 FOR UPDATE",
        [store(db, "alice").claim(1), store(db, "bob").claim(1)],
    )
    outcomes = sorted(outcome(r) for r in results)
    assert outcomes[1] == "won" and outcomes[0] in {"claimed_by_other", "lease_changed"}, outcomes
    shown = await store(db, "alice").show(1)
    assert shown["claim"]["revision"] == 1


async def test_two_locks_at_once_give_one_lock(db: Any) -> None:
    await store(db, "alice").lock("repo/main", None)
    await store(db, "alice").unlock("repo/main", None)
    results = await race(
        db, "SELECT 1 FROM {{tables.locks}} WHERE resource = 'repo/main' FOR UPDATE",
        [store(db, "alice").lock("repo/main", "a"), store(db, "bob").lock("repo/main", "b")],
    )
    outcomes = sorted(outcome(r) for r in results)
    assert outcomes[1] == "won" and outcomes[0] in {"locked_by_other", "lease_changed"}, outcomes
    (held,) = await store(db, "alice").locks()
    assert held["revision"] == 3


async def test_a_lease_is_judged_at_the_time_after_its_lock_wait(db: Any) -> None:
    now = [datetime(2026, 11, 27, 12, tzinfo=UTC)]

    def clock() -> datetime:
        return now[0]

    await store(db, "alice", clock).create(title="Work", body="", type="task", priority=2, labels=[], parent=None, assignee=None)
    await store(db, "alice", clock).claim(1)
    async with db.transaction() as tx:
        await tx.execute("SELECT 1 FROM {{tables.claims}} WHERE number = 1 FOR UPDATE")
        waiting = asyncio.ensure_future(store(db, "bob", clock).claim(1))
        await waiting_on_locks(db, 1)
        # Alice's claim expires while Bob waits for the row.
        now[0] += timedelta(hours=24)
    claimed = await waiting
    assert claimed["claim"]["holder_alias"] == "bob"


def emitting(tmp_path: Path) -> AppEvents:
    """The app's emits as a deployment with an app-event key configures them."""
    key_files = write_key_files(tmp_path)
    settings = TasksSettings(
        _env_file=None, database_url="postgresql://localhost/unused", app_id=TEST_APP_ID,
        public_origin="https://tasks.example.test", app_key_files=key_files,
        app_events_origin="https://aweb.example.test",
    )
    return AppEvents.from_settings(settings, load_manifest(served_manifest(key_files, origin=settings.public_origin)))


async def test_each_status_claim_and_lock_change_enqueues_its_event(db: Any, tmp_path: Path) -> None:
    await apply_outbox_migrations(db)
    events = emitting(tmp_path)

    def acting(alias: str) -> TaskStore:
        return TaskStore(db, installation_id=INSTALLATION, principal=principal(alias), events=events, app_id=TEST_APP_ID)

    alice, bob = acting("alice"), acting("bob")
    await alice.create(title="Work", body="", type="task", priority=2, labels=[], parent=None, assignee=None)
    await alice.claim(1)
    await alice.renew_claim(1, None)  # a renewal changes no holder: no event
    await alice.comment(1, "noted")  # the holder's comment renews it too: revision 3
    await bob.takeover(1, 3)
    await bob.lock("repo/main", "merging")
    await bob.renew_lock("repo/main", None)
    await bob.unlock("repo/main", None)
    await bob.close(1, with_children=False)
    await alice.reopen(1)
    rows = await db.fetch_all("SELECT team_id, kind, payload FROM {{tables.outbox_events}} ORDER BY position")
    assert {row["team_id"] for row in rows} == {"team:example.test"}
    bodies = [json.loads(bytes(row["payload"])) for row in rows]
    assert [(b["type"], b["resource_ref"], b["payload"]) for b in bodies] == [
        (f"{TEST_APP_ID}/task.status", "1", {"status": "open"}),
        (f"{TEST_APP_ID}/task.claim", "1", {"revision": "1"}),
        (f"{TEST_APP_ID}/task.claim", "1", {"revision": "4"}),
        (f"{TEST_APP_ID}/lock", "repo/main", {"revision": "1"}),
        (f"{TEST_APP_ID}/lock", "repo/main", {"revision": "3"}),
        (f"{TEST_APP_ID}/task.claim", "1", {"revision": "5"}),
        (f"{TEST_APP_ID}/task.status", "1", {"status": "closed"}),
        (f"{TEST_APP_ID}/task.status", "1", {"status": "open"}),
    ]
    assert {b["delivery_intent"] for b in bodies} == {"wake"}
