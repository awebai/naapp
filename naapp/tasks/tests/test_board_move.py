"""The one-off board move (spec section 8, steps 3 to 6) against a real board.

The source is a team's board on the aweb server release production runs,
written through its own routes; the export is the operator's psql query run
against that server's database; the target is a real Tasks database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from aweb.app_manifests import FetchedManifest
from aweb.installations import install_verified_manifest_on, uninstall_installation_on
from aweb_naapp.manifest import load_manifest
from aweb_naapp.outbox import RetryPolicy
from aweb_naapp.runtime.event_emit import AppEvents
from awid.team_ids import team_slug
from awid_service.testing import REGISTRY_URL, SERVICE_TOKEN, dns_naming
from pgdbm import AsyncMigrationManager

from aweb_tasks.app import MIGRATIONS_PATH, create_app
from aweb_tasks.board_move import __main__ as cli
from aweb_tasks.board_move import move
from aweb_tasks.config import TasksSettings
from aweb_tasks.store import TaskStore

from .deployment import TEST_APP_ID, served_manifest, write_key_files
from .old_board import DOMAIN, NAMESPACE_KEY, OldBoard, old_board
from .test_store import principal

ORIGIN = "https://tasks.example.test"


@pytest.fixture
async def tasks_db(test_db_factory: Any) -> Any:
    return await test_db_factory.create_db(suffix="board_move")


@pytest.fixture
async def board(coordination: Any, tasks_db: Any) -> OldBoard:
    return await old_board(coordination, tasks_db, ["alice", "bob"])


async def export(board: OldBoard, tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "export.json"
    result = await board.export(path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(path.read_text(encoding="utf-8"))


async def test_export_carries_the_whole_team_board(board: OldBoard, tmp_path: Path) -> None:
    epic = await board.create("alice", title="Move the board", task_type="epic", labels=["e2"])
    child = await board.create(
        "alice", title="Write the script", description="Steps 3 to 6", notes="one-off",
        parent_task_id=epic["task_id"], assignee_alias="bob", priority=1,
    )
    await board.comment("bob", child["task_ref"], "Taking it")
    await board.depend("alice", child["task_ref"], epic["task_ref"])
    await board.update("bob", child["task_ref"], status="in_progress")
    await board.reserve("bob", "naapp/tasks", ttl_seconds=600, reason="writing")

    exported = await export(board, tmp_path)

    assert exported["format"] == "aweb-board-export/1"
    assert exported["team_id"] == board.team_id
    tasks = {task["task_ref_suffix"]: task for task in exported["tasks"]}
    slug = team_slug(board.team_id)
    assert set(tasks) == {
        epic["task_ref"].removeprefix(f"{slug}-"), child["task_ref"].removeprefix(f"{slug}-"),
    }
    row = next(task for task in exported["tasks"] if task["task_id"] == child["task_id"])
    assert row["title"] == "Write the script"
    assert row["description"] == "Steps 3 to 6"
    assert row["notes"] == "one-off"
    assert row["status"] == "in_progress"
    assert row["parent_task_id"] == epic["task_id"]
    assert row["assignee_alias"] == "bob"
    assert row["created_by_alias"] == "alice"
    assert [c["body"] for c in exported["comments"]] == ["Taking it"]
    assert exported["comments"][0]["author_alias"] == "bob"
    assert exported["dependencies"] == [{"task_id": child["task_id"], "depends_on_id": epic["task_id"]}]
    assert exported["task_counter"] == max(t["task_number"] for t in exported["tasks"]) + 1
    [claim] = exported["claims"]
    assert (claim["task_ref"], claim["alias"]) == (child["task_ref"], "bob")
    [reservation] = exported["reservations"]
    assert (reservation["resource_key"], reservation["holder_alias"], reservation["live"]) == (
        "naapp/tasks", "bob", True,
    )
    assert reservation["metadata_json"] == {"reason": "writing"}
    assert exported["task_app_requests"] == []
    assert exported["task_answers"] == []
    assert exported["task_answer_stage_nonces"] == []


async def test_export_reads_only_the_named_team(
    board: OldBoard, coordination: Any, tasks_db: Any, tmp_path: Path
) -> None:
    neighbour = await old_board(coordination, tasks_db, ["carol"])
    ours = await board.create("alice", title="Ours")
    theirs = await neighbour.create("carol", title="Theirs")
    await neighbour.comment("carol", theirs["task_ref"], "Not yours")
    await neighbour.update("carol", theirs["task_ref"], status="in_progress")
    await neighbour.reserve("carol", "shared/name", ttl_seconds=600, reason="theirs")

    exported = await export(board, tmp_path)

    assert [task["task_id"] for task in exported["tasks"]] == [ours["task_id"]]
    assert exported["comments"] == []
    assert exported["claims"] == []
    assert exported["reservations"] == []


async def test_export_stops_on_a_schema_it_was_not_written_for(board: OldBoard, tmp_path: Path) -> None:
    await board.create("alice", title="Ours")
    await board.db.execute("ALTER TABLE aweb.task_claims ADD COLUMN expires_at TIMESTAMPTZ")

    path = tmp_path / "export.json"
    result = await board.export(path)

    assert result.returncode != 0
    assert "board export: aweb.task_claims has the columns" in result.stderr
    assert not path.exists() or path.read_text(encoding="utf-8") == ""


async def test_export_reads_the_schema_under_any_database_collation(board: OldBoard, tmp_path: Path) -> None:
    """information_schema names sort in the C collation whatever the database's
    default; a production database's default orders underscores differently."""
    await board.create("alice", title="Ours")
    database = f"board_move_collation_{uuid4().hex[:10]}"
    admin = board.postgres_env("postgres")
    subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
         f"CREATE DATABASE {database} TEMPLATE template0 LOCALE_PROVIDER icu"
         " ICU_LOCALE 'en-US-u-ka-shifted' LOCALE 'en_US.UTF-8'"],
        env=admin, check=True,
    )
    try:
        schema = subprocess.run(
            ["pg_dump", "--schema-only", "--no-owner", "--no-acl", "--schema=aweb"],
            env=board.postgres_env(await board.database_name()), capture_output=True, check=True,
        ).stdout
        subprocess.run(
            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1"], input=schema,
            env=board.postgres_env(database), capture_output=True, check=True,
        )

        result = await board.export(tmp_path / "export.json", database=database)

        assert result.returncode == 0, result.stderr
    finally:
        subprocess.run(["psql", "-X", "-q", "-c", f"DROP DATABASE {database}"], env=admin, check=True)


async def stop_codes(board: OldBoard, tmp_path: Path) -> dict[str, list[str]]:
    return {stop.code: list(stop.subjects) for stop in move.stops(move.Export(await export(board, tmp_path)))}


async def workspace_of(board: OldBoard, alias: str) -> Any:
    return await board.db.fetch_value(
        "SELECT workspace_id FROM aweb.workspaces WHERE team_id = $1 AND alias = $2 AND deleted_at IS NULL",
        board.team_id, alias,
    )


async def test_a_board_the_routes_wrote_has_no_stop(board: OldBoard, tmp_path: Path) -> None:
    epic = await board.create("alice", title="Epic", task_type="epic")
    chore = await board.create("alice", title="Tidy", task_type="chore", parent_task_id=epic["task_id"])
    await board.update("bob", chore["task_ref"], status="in_progress")
    await board.update("alice", epic["task_ref"], status="closed")
    gone = await board.create("alice", title="Gone")
    await board.delete("alice", gone["task_ref"])
    await board.reserve("bob", "naapp/tasks", ttl_seconds=600, reason="writing")

    assert await stop_codes(board, tmp_path) == {}


async def test_export_stops_on_two_live_claims_on_one_task(board: OldBoard, tmp_path: Path) -> None:
    task = await board.create("alice", title="Contested")
    await board.update("alice", task["task_ref"], status="in_progress")
    # A second claim from another workspace, as an earlier race left it.
    await board.db.execute(
        "INSERT INTO aweb.task_claims (team_id, workspace_id, alias, task_ref) VALUES ($1, $2, 'bob', $3)",
        board.team_id, await workspace_of(board, "bob"), task["task_ref"],
    )

    assert await stop_codes(board, tmp_path) == {"more_than_one_live_claim": [task["task_ref"]]}


async def test_export_stops_on_a_claim_whose_task_is_gone(board: OldBoard, tmp_path: Path) -> None:
    gone = await board.create("alice", title="Gone")
    await board.delete("alice", gone["task_ref"])
    await board.db.execute(
        "INSERT INTO aweb.task_claims (team_id, workspace_id, alias, task_ref) VALUES ($1, $2, 'bob', $3)",
        board.team_id, await workspace_of(board, "bob"), gone["task_ref"],
    )

    assert await stop_codes(board, tmp_path) == {"claim_without_task": [gone["task_ref"]]}


async def test_export_stops_on_app_request_evidence(board: OldBoard, tmp_path: Path) -> None:
    task = await board.create("alice", title="Asked by an app")
    await board.db.execute(
        "INSERT INTO aweb.task_app_requests (task_id, team_id, app_id, installation_id,"
        " installation_generation, originator_did, external_request_id, task_body_sha256,"
        " task_authorization, receipt) VALUES ($1, $2, 'folio', 'inst-1', 1, 'did:key:z6Mk', 'r-1',"
        " repeat('a', 64), '{}'::jsonb, '{}'::jsonb)",
        task["task_id"], board.team_id,
    )
    receipt = await board.db.fetch_value(
        "INSERT INTO aweb.task_answers (task_id, question_rev, task_rev, answer_key, answer_sha256,"
        " answered_by_did, certificate_id) VALUES ($1, 1, 1, 'k', repeat('b', 64), 'did:key:z6Mk', 'c_1')"
        " RETURNING receipt_id",
        task["task_id"],
    )
    await board.db.execute(
        "INSERT INTO aweb.task_answer_stage_nonces (team_id, app_id, installation_id, nonce, receipt_id)"
        " VALUES ($1, 'folio', 'inst-1', repeat('c', 32), $2)",
        board.team_id, receipt,
    )

    assert await stop_codes(board, tmp_path) == {
        "app_request": [task["task_ref"]],
        "app_request_receipt": [str(receipt)],
        "app_request_stage": [str(receipt)],
    }


async def test_export_stops_on_a_live_task_that_names_a_deleted_one(board: OldBoard, tmp_path: Path) -> None:
    parent = await board.create("alice", title="Parent")
    child = await board.create("alice", title="Child", parent_task_id=parent["task_id"])
    waited_on = await board.create("alice", title="Waited on")
    waiting = await board.create("alice", title="Waiting")
    await board.depend("alice", waiting["task_ref"], waited_on["task_ref"])
    await board.delete("alice", parent["task_ref"])
    await board.delete("alice", waited_on["task_ref"])

    assert await stop_codes(board, tmp_path) == {
        "names_a_deleted_task": [child["task_ref"], waiting["task_ref"]],
    }


async def test_export_stops_on_a_type_the_app_does_not_know(board: OldBoard, tmp_path: Path) -> None:
    task = await board.create("alice", title="Spike")
    # Only a server that allowed other types could have left one.
    await board.db.execute("ALTER TABLE aweb.tasks DROP CONSTRAINT tasks_task_type_check")
    await board.db.execute("UPDATE aweb.tasks SET task_type = 'spike' WHERE task_id = $1", task["task_id"])

    assert await stop_codes(board, tmp_path) == {"unknown_type": [task["task_ref"]]}


async def test_export_stops_on_values_the_app_refuses(board: OldBoard, tmp_path: Path) -> None:
    blank = await board.create("alice", title=" ")
    commented = await board.create("alice", title="Commented")
    await board.comment("alice", commented["task_ref"], " ")
    closed = await board.create("alice", title="Closed")
    await board.update("alice", closed["task_ref"], status="closed")
    await board.db.execute("UPDATE aweb.tasks SET closed_at = NULL WHERE task_id = $1", closed["task_id"])

    assert await stop_codes(board, tmp_path) == {
        "blank_title": [blank["task_ref"]],
        "blank_comment": [commented["task_ref"]],
        "closed_without_close_time": [closed["task_ref"]],
    }


async def test_export_stops_on_a_counter_behind_its_numbers(board: OldBoard, tmp_path: Path) -> None:
    await board.create("alice", title="One")
    await board.create("alice", title="Two")
    await board.db.execute("UPDATE aweb.task_counters SET next_number = 2 WHERE team_id = $1", board.team_id)

    assert await stop_codes(board, tmp_path) == {"counter_behind_numbers": ["2"]}


@dataclass
class Deployment:
    """The Tasks deployment the move targets: its settings, and the live grant
    source its own grant-read key reads the team's installation through."""

    settings: TasksSettings
    grants: Any
    app_id: str
    manifest: bytes
    server_db: Any

    async def install(self, team_id: str, scopes: list[str], *, expected_generation: int | None = None) -> dict:
        manifest = load_manifest(self.manifest)
        fetched = FetchedManifest(
            origin=ORIGIN, url=ORIGIN + "/.well-known/aweb-app.json",
            digest="sha256:" + hashlib.sha256(self.manifest).hexdigest(), data=self.manifest,
            manifest=manifest,
        )
        async with self.server_db.transaction() as tx:
            return await install_verified_manifest_on(
                tx, team_id=team_id, fetched=fetched, granted_scopes=scopes,
                expected_generation=expected_generation,
            )

    async def destination(self, team_id: str) -> move.Destination:
        return await move.destination(self.grants, team_id=team_id, app_id=self.app_id)


@pytest.fixture
async def live(tasks_db: Any) -> Any:
    """The installation's live Tasks tables, as the app's own migrations build them."""
    await AsyncMigrationManager(
        tasks_db, migrations_path=str(MIGRATIONS_PATH), module_name="tasks"
    ).apply_pending_migrations()
    return tasks_db


@pytest.fixture
async def deployment(coordination: Any, live: Any, tmp_path: Path) -> AsyncIterator[Deployment]:
    key_files = write_key_files(tmp_path)
    settings = TasksSettings(
        _env_file=None, database_url=live.config.get_dsn(), db_schema=live.schema,
        app_id=TEST_APP_ID, public_origin=ORIGIN, awid_registry_url=REGISTRY_URL,
        awid_service_token=SERVICE_TOKEN, app_key_files=key_files,
    )
    app = create_app(
        settings, awid_transport=coordination.registry.transport,
        awid_txt_resolver=dns_naming(DOMAIN, NAMESPACE_KEY),
        team_server_transport=coordination.server.transport,
    )
    runtime = app.state.runtime
    async with app.router.lifespan_context(app):
        yield Deployment(
            settings=settings, grants=runtime.installation_grants(), app_id=runtime.manifest.app_id,
            manifest=served_manifest(key_files, origin=ORIGIN),
            server_db=coordination.server.db.get_manager("aweb"),
        )


@pytest.fixture
async def installed(board: OldBoard, deployment: Deployment) -> dict:
    """Step 1: the app installed on the exported team, granting only tasks.read."""
    return await deployment.install(board.team_id, ["tasks.read"])


async def test_the_destination_is_the_exported_teams_read_only_installation(
    board: OldBoard, deployment: Deployment, installed: dict
) -> None:
    destination = await deployment.destination(board.team_id)

    assert destination == move.Destination(
        team_id=board.team_id, app_id=deployment.app_id,
        installation_id=installed["installation_id"], generation=installed["generation"],
    )


async def test_the_destination_refuses_a_team_the_installation_is_not_for(
    board: OldBoard, coordination: Any, tasks_db: Any, deployment: Deployment, installed: dict
) -> None:
    neighbour = await old_board(coordination, tasks_db, ["carol"])

    with pytest.raises(move.MoveRefused, match="no active installation"):
        await deployment.destination(neighbour.team_id)


async def test_the_destination_refuses_an_uninstalled_installation(
    board: OldBoard, deployment: Deployment, installed: dict
) -> None:
    async with deployment.server_db.transaction() as tx:
        await uninstall_installation_on(
            tx, team_id=board.team_id, app_id=deployment.app_id, expected_generation=installed["generation"],
        )

    with pytest.raises(move.MoveRefused, match="no active installation"):
        await deployment.destination(board.team_id)


async def test_the_destination_refuses_an_installation_that_grants_writes(
    board: OldBoard, deployment: Deployment
) -> None:
    await deployment.install(board.team_id, ["tasks.read", "tasks.write", "tasks.override"])

    with pytest.raises(move.MoveRefused, match="not only tasks.read"):
        await deployment.destination(board.team_id)


INSTALLATION = "installation-mapping"


def destination_of(exported: move.Export) -> move.Destination:
    """A destination value for the pure mapping; staging and opening read the live grant."""
    return move.Destination(
        team_id=exported.team_id, app_id=TEST_APP_ID, installation_id=INSTALLATION, generation=1,
    )


def target(live: Any) -> move.Target:
    return move.Target(dsn=live.config.get_dsn(), live_schema=live.schema)


def app_store(live: Any, destination: move.Destination, alias: str) -> TaskStore:
    """The app's own store on the moved installation, acting for one member."""
    events = AppEvents(app_id=destination.app_id, origin=None, key=None,
                       policy=RetryPolicy(stalled_after=timedelta(hours=1)))
    return TaskStore(live, installation_id=destination.installation_id, principal=principal(alias),
                     events=events, app_id=destination.app_id)


async def staging_exists(live: Any) -> bool:
    return bool(await live.fetch_value(
        "SELECT 1 FROM pg_namespace WHERE nspname = $1", target(live).staging_schema
    ))


async def full_board(board: OldBoard) -> dict[str, dict[str, Any]]:
    """A board with every case the mapping rules on."""
    epic = await board.create("alice", title="Move the board", task_type="epic", labels=["e2", "move"])
    script = await board.create(
        "alice", title="Write the script", description="Steps 3 to 6", notes="one-off",
        parent_task_id=epic["task_id"], assignee_alias="bob", priority=1,
    )
    chore = await board.create("bob", title="Tidy up", task_type="chore")
    # A parent numbered after its child.
    await board.update("alice", epic["task_ref"], parent_task_id=chore["task_id"])
    await board.comment("bob", script["task_ref"], "Taking it")
    await board.comment("alice", script["task_ref"], "Thanks")
    await board.depend("alice", script["task_ref"], chore["task_ref"])
    await board.update("bob", script["task_ref"], status="in_progress")
    done = await board.create("alice", title="Done already")
    await board.update("bob", done["task_ref"], status="closed")
    gone = await board.create("alice", title="Gone")
    await board.delete("alice", gone["task_ref"])
    await board.reserve("bob", "naapp/tasks", ttl_seconds=600, reason="writing")
    await board.reserve("alice", "old/lock", ttl_seconds=600, reason="stale")
    await board.db.execute(
        "UPDATE aweb.reservations SET expires_at = now() - interval '1 minute'"
        " WHERE team_id = $1 AND resource_key = 'old/lock'",
        board.team_id,
    )
    return {"epic": epic, "script": script, "chore": chore, "done": done, "gone": gone}


async def test_the_dataset_follows_each_mapping_rule(board: OldBoard, tmp_path: Path) -> None:
    tasks = await full_board(board)
    exported = move.Export(await export(board, tmp_path))
    by_id = {task["task_id"]: task for task in exported.tasks}
    number = {name: task["task_number"] for name, task in tasks.items()}

    data = move.dataset(exported, destination_of(exported))
    rows = data.rows
    staged = {task["number"]: task for task in rows["tasks"]}
    imported = {e["number"]: e["detail"] for e in rows["events"] if e["kind"] == "imported"}

    # Numbers and the saved counter kept; the deleted task is not moved, but its number stays used.
    assert rows["installations"] == [{"installation_id": INSTALLATION, "next_number": exported.document["task_counter"]}]
    assert sorted(staged) == sorted(number[name] for name in ("epic", "script", "chore", "done"))
    assert rows["installations"][0]["next_number"] > number["gone"]
    # Old id and ref only in the import entry.
    for name in ("epic", "script", "chore", "done"):
        assert imported[number[name]]["task_id"] == tasks[name]["task_id"]
        assert imported[number[name]]["ref"] == tasks[name]["task_ref"]
    script = staged[number["script"]]
    old_script = by_id[tasks["script"]["task_id"]]
    assert (script["status"], imported[number["script"]]["status"]) == ("open", "in_progress")
    assert script["body"] == "Steps 3 to 6"
    assert imported[number["script"]]["notes"] == "one-off"
    assert script["assignee"] == "bob"
    assert imported[number["script"]]["assignee_did"] == old_script["assignee_did"]
    assert script["creator"] == {"alias": "alice", "did": old_script["created_by_did"]}
    assert script["parent"] == number["epic"]
    assert staged[number["epic"]]["parent"] == number["chore"]
    chore = staged[number["chore"]]
    assert (chore["type"], imported[number["chore"]]["type"]) == ("task", "chore")
    assert by_id[tasks["chore"]["task_id"]]["updated_at"] is None
    assert chore["updated_at"] == chore["created_at"]
    assert imported[number["chore"]]["updated_at"] is None
    done = by_id[tasks["done"]["task_id"]]
    assert staged[number["done"]]["closer"] == {"alias": done["closed_by_alias"], "did": done["closed_by_did"]}
    assert all("status" not in imported[n] for n in (number["epic"], number["chore"], number["done"]))
    # No claim and no lock is live; each live one is history.
    assert all(c["holder_identity"] is None and c["revision"] == 0 for c in rows["claims"])
    assert rows["locks"] == []
    [claimed] = [e for e in rows["events"] if e["kind"] == "claim_not_proven"]
    [old_claim] = exported.claims
    assert claimed["number"] == number["script"]
    assert claimed["detail"]["summary"] == (
        f"claimed by bob from workspace {old_claim['workspace_id']}, holder not proven"
    )
    [locked] = [e for e in rows["events"] if e["kind"] == "lock_not_proven"]
    assert (locked["resource"], locked["detail"]["metadata_json"]) == ("naapp/tasks", {"reason": "writing"})
    assert {e["actor"]["app"] for e in rows["events"]} == {TEST_APP_ID}
    assert {e["cause"] for e in rows["events"]} == {"board-move"}
    assert [(c["author"], c["body"]) for c in rows["comments"]] == [
        ({"alias": "bob"}, "Taking it"), ({"alias": "alice"}, "Thanks"),
    ]
    assert rows["dependencies"] == [
        {"installation_id": INSTALLATION, "number": number["script"], "waits_on": number["chore"]},
    ]
    # What the app does not hold is in the listing's source facts.
    kinds = [record["kind"] for record in data.source]
    assert kinds == ["destination", "export", "root_task_counter", "deleted_task", "expired_lock"]
    assert data.source[0] == {"kind": "destination", **dataclasses.asdict(destination_of(exported))}
    assert data.source[2]["next_number"] == exported.document["root_task_counter"]
    assert data.source[3]["ref"] == tasks["gone"]["task_ref"]
    assert data.source[4]["resource_key"] == "old/lock"


async def test_the_dataset_is_only_for_the_exported_teams_destination(
    board: OldBoard, tmp_path: Path
) -> None:
    await board.create("alice", title="Ours")
    exported = move.Export(await export(board, tmp_path))
    elsewhere = dataclasses.replace(destination_of(exported), team_id="other-team:board.example")

    with pytest.raises(move.MoveRefused, match="the destination other-team"):
        move.dataset(exported, elsewhere)


async def staged(board: OldBoard, live: Any, deployment: Deployment, tmp_path: Path,
                 ) -> tuple[move.Export, move.Destination, bytes]:
    """Steps 3 to 5: the export, staged for the team's installation, and its listing."""
    exported = move.Export(await export(board, tmp_path))
    destination = await deployment.destination(board.team_id)
    await move.stage(target(live), exported, destination)
    return exported, destination, await move.staged_listing(target(live))


async def test_the_staged_board_lists_exactly_as_its_export(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await full_board(board)

    exported, destination, from_staging = await staged(board, live, deployment, tmp_path)

    assert from_staging == move.listing(move.dataset(exported, destination))
    assert destination.installation_id == installed["installation_id"]
    # Staging is out of reach of the installation's members: the live tables hold none of it.
    for table in ("installations", "tasks", "claims", "locks", "comments", "dependencies", "events"):
        assert await live.fetch_value(f"SELECT count(*) FROM {{{{tables.{table}}}}}") == 0, table


async def test_a_staged_row_that_differs_from_the_export_shows_in_the_listing(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    tasks = await full_board(board)
    exported, destination, _ = await staged(board, live, deployment, tmp_path)

    await live.execute(
        f'UPDATE "{target(live).staging_schema}".tasks SET title = $1 WHERE number = $2',
        "Written by someone else", tasks["script"]["task_number"],
    )

    assert await move.staged_listing(target(live)) != move.listing(move.dataset(exported, destination))


async def test_stage_refuses_an_export_with_stops(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title=" ")

    with pytest.raises(move.MoveRefused, match="blank_title"):
        await staged(board, live, deployment, tmp_path)
    assert not await staging_exists(live)


async def test_a_retry_stages_only_after_the_last_stage_is_discarded(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="One")
    await staged(board, live, deployment, tmp_path)
    await board.create("alice", title="Two")

    with pytest.raises(move.MoveRefused, match="exists"):
        await staged(board, live, deployment, tmp_path)
    assert await move.discard(target(live)) is True
    exported, destination, from_staging = await staged(board, live, deployment, tmp_path)

    assert from_staging == move.listing(move.dataset(exported, destination))


async def test_open_makes_the_approved_staging_the_installation_dataset(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    tasks = await full_board(board)
    exported, destination, from_staging = await staged(board, live, deployment, tmp_path)

    await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(from_staging))

    data = move.dataset(exported, destination)
    assert await move.live_listing(target(live), destination) == move.listing(data, source=False)
    assert not await staging_exists(live)
    # The app itself reads and writes the moved board.
    alice = app_store(live, destination, "alice")
    script = tasks["script"]["task_number"]
    assert (await alice.show(script))["title"] == "Write the script"
    history = await alice.history(script)
    assert [entry["kind"] for entry in history] == ["imported", "claim_not_proven"]
    # The script waits on the chore and its old claim is not live; the rest is ready.
    assert [t["number"] for t in await alice.ready()] == sorted(
        [tasks["epic"]["task_number"], tasks["chore"]["task_number"]]
    )
    created = await alice.create(
        title="First in the app", body="", type="task", priority=2, labels=[], parent=None, assignee=None,
    )
    assert created["number"] == exported.document["task_counter"]
    claimed = await alice.claim(script)
    assert claimed["claim"]["holder_alias"] == "alice"
    # The rollback boundary: after the app's first write the dataset no longer lists as staged.
    assert await move.live_listing(target(live), destination) != move.listing(data, source=False)


@asynccontextmanager
async def tasks_service(deployment: Deployment, coordination: Any) -> AsyncIterator[httpx.AsyncClient]:
    """One process of the Tasks service, from start to suspension."""
    app = create_app(deployment.settings, **app_options(coordination))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            yield client


async def create_task(client: httpx.AsyncClient, member: Any, title: str) -> httpx.Response:
    body = json.dumps({"title": title}).encode()
    headers = member.headers(aud=ORIGIN, method="POST", path="/v1/tasks", body=body)
    return await client.post("/v1/tasks", content=body, headers={**headers, "Content-Type": "application/json"})


async def test_a_rolled_back_installation_refuses_every_write_once_resumed(
    board: OldBoard, live: Any, deployment: Deployment, coordination: Any, installed: dict,
    tmp_path: Path,
) -> None:
    await board.create("alice", title="Old")
    _, destination, approved = await staged(board, live, deployment, tmp_path)
    await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved))
    opened = await deployment.install(
        board.team_id, ["tasks.read", "tasks.write", "tasks.override"],
        expected_generation=installed["generation"],
    )
    async with tasks_service(deployment, coordination) as service:
        assert (await create_task(service, board.members["alice"], "Written after opening")).status_code == 200

    # Rollback, while the service is suspended: the grant goes back to tasks.read.
    await deployment.install(board.team_id, ["tasks.read"], expected_generation=opened["generation"])

    async with tasks_service(deployment, coordination) as resumed:
        refused = await create_task(resumed, board.members["alice"], "Written after the rollback")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "scope_denied")


async def test_open_refuses_staging_that_changed_after_its_listing_was_approved(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    tasks = await full_board(board)
    _, destination, approved = await staged(board, live, deployment, tmp_path)
    await live.execute(
        f'UPDATE "{target(live).staging_schema}".tasks SET title = $1 WHERE number = $2',
        "Written by someone else", tasks["script"]["task_number"],
    )
    changed = await move.staged_listing(target(live))

    with pytest.raises(move.MoveRefused, match="does not list as the approved listing"):
        await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved))

    assert await move.staged_listing(target(live)) == changed
    assert await move.live_listing(target(live), destination) == b""


async def test_open_refuses_a_stage_replaced_after_its_listing_was_approved(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="One")
    _, destination, approved = await staged(board, live, deployment, tmp_path)
    await move.discard(target(live))
    await board.create("alice", title="Two")
    _, _, replacement = await staged(board, live, deployment, tmp_path)

    with pytest.raises(move.MoveRefused, match="does not list as the approved listing"):
        await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved))

    assert await move.staged_listing(target(live)) == replacement
    assert await move.live_listing(target(live), destination) == b""


async def test_open_refuses_when_the_installation_changed_after_staging(
    board: OldBoard, live: Any, deployment: Deployment, coordination: Any, installed: dict,
    tmp_path: Path,
) -> None:
    await board.create("alice", title="One")
    exported, staged_for, approved = await staged(board, live, deployment, tmp_path)
    reinstalled = await deployment.install(
        board.team_id, ["tasks.read"], expected_generation=installed["generation"]
    )
    # Each command reads the grant afresh, as a new process does.
    async with cli.destination_of(exported, deployment.settings, app_options(coordination)) as now:
        assert now.generation == reinstalled["generation"] != staged_for.generation

        with pytest.raises(move.MoveRefused, match="was staged for"):
            await move.open_staged(target(live), now, listing_sha256=move.listing_digest(approved))

    assert await move.staged_listing(target(live)) == approved


async def test_open_refuses_an_installation_that_already_has_rows(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="Old")
    _, destination, approved = await staged(board, live, deployment, tmp_path)
    await app_store(live, destination, "alice").create(
        title="Written in the app first", body="", type="task", priority=2, labels=[], parent=None,
        assignee=None,
    )
    before = await move.live_listing(target(live), destination)

    with pytest.raises(move.MoveRefused, match="already has"):
        await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved))

    assert await move.live_listing(target(live), destination) == before
    assert await move.staged_listing(target(live)) == approved


def app_options(coordination: Any) -> dict[str, Any]:
    """How the app reaches the in-process registry and server."""
    return {
        "awid_transport": coordination.registry.transport,
        "awid_txt_resolver": dns_naming(DOMAIN, NAMESPACE_KEY),
        "team_server_transport": coordination.server.transport,
    }


async def command(deployment: Deployment, coordination: Any, *args: str) -> bytes:
    """One operator command, in-process, with the deployment's settings; the app
    reaches the in-process registry and server through their transports."""
    out = io.BytesIO()
    status = await cli.run(
        list(args), settings=deployment.settings, out=out, app_options=app_options(coordination),
    )
    assert status == 0
    return out.getvalue()


async def test_the_operator_moves_the_board_with_the_commands(
    board: OldBoard, live: Any, deployment: Deployment, coordination: Any, installed: dict,
    tmp_path: Path,
) -> None:
    await full_board(board)
    path = tmp_path / "export.json"
    assert (await board.export(path)).returncode == 0

    assert await command(deployment, coordination, "check", str(path)) == b"no stops\n"
    from_export = await command(deployment, coordination, "list-export", str(path))
    await command(deployment, coordination, "stage", str(path))
    from_staging = await command(deployment, coordination, "list-staged")
    assert from_staging == from_export
    digest = hashlib.sha256(from_staging).hexdigest()
    await command(deployment, coordination, "open", str(path), "--listing-sha256", digest)
    dataset_only = await command(deployment, coordination, "list-export", str(path), "--dataset-only")
    from_live = await command(deployment, coordination, "list-live", str(path))
    assert from_live == dataset_only
    assert from_live and len(from_live) < len(from_export)
    assert installed["installation_id"].encode() in from_export


def run(live: Any, *args: str) -> subprocess.CompletedProcess[bytes]:
    """An operator command as a process, with the deployment's database settings."""
    env = {**os.environ, "TASKS_DATABASE_URL": live.config.get_dsn(), "TASKS_DB_SCHEMA": live.schema}
    return subprocess.run(
        [sys.executable, "-m", "aweb_tasks.board_move", *args],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, check=False,
    )


async def test_the_commands_exit_with_the_stop_or_the_refusal(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    task = await board.create("alice", title=" ")
    path = tmp_path / "export.json"
    assert (await board.export(path)).returncode == 0

    checked = await asyncio.to_thread(run, live, "check", str(path))
    assert checked.returncode == 2
    assert json.loads(checked.stdout) == {
        "code": "blank_title", "reason": "a task's title is blank, which the app refuses",
        "subjects": [task["task_ref"]],
    }
    listed = await asyncio.to_thread(run, live, "list-staged")
    assert (listed.returncode, listed.stderr) == (
        3, f"refused: {target(live).staging_schema} does not exist\n".encode(),
    )
    assert not await staging_exists(live)
    discarded = await asyncio.to_thread(run, live, "discard")
    assert (discarded.returncode, discarded.stdout) == (
        0, f"{target(live).staging_schema} did not exist\n".encode(),
    )
