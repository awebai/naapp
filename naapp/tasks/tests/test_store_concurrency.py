"""Concurrent transitions on one lease, through the store on real PostgreSQL.

Over HTTP, naapp-lib's installation fence admits one of a team's requests at a
time while another's transaction is open, so racing requests never reach the
store together. These tests race two stores directly. A transaction holds the
lease row until both are inside their own transactions and waiting on it, then
lets them through together: exactly one wins.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from aweb_naapp.outbox import RetryPolicy
from aweb_naapp.runtime.event_emit import AppEvents
from aweb_naapp.runtime.team_auth import Principal
from pgdbm import AsyncMigrationManager

from aweb_tasks.app import MIGRATIONS_PATH
from aweb_tasks.store import TaskRefused, TaskStore

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


def store(db: Any, alias: str) -> TaskStore:
    events = AppEvents(app_id="tasks-test", origin=None, key=None, policy=RetryPolicy(stalled_after=timedelta(hours=1)))
    return TaskStore(db, installation_id=INSTALLATION, principal=principal(alias), events=events, app_id="tasks-test")


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
