"""The one-off board move (spec section 8, steps 3 to 6) against a real board.

The source is a team's board on the aweb server release production runs,
written through its own routes; the export is the operator's psql query run
against that server's database; the target is a real Tasks database.
"""

from __future__ import annotations

import asyncio
import base64
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

import asyncpg
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
from .test_store import principal, waiting_on_locks

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


LOAD_SQL = Path(move.__file__).with_name("load_export.sql")
FETCH_SQL = Path(move.__file__).with_name("fetch_listing.sql")


def loaded_text(path: Path) -> str:
    """The export file's text as the loader keeps it: without the final newline."""
    return path.read_text(encoding="utf-8").removesuffix("\n")


def from_file(path: Path) -> move.Export:
    text = loaded_text(path)
    return move.Export(json.loads(text), sha256=hashlib.sha256(text.encode()).hexdigest())


async def export_of(board: OldBoard, tmp_path: Path) -> move.Export:
    """Step 3 on the operator's host: the export, as the loader would name it."""
    path = tmp_path / "export.json"
    result = await board.export(path)
    assert result.returncode == 0, result.stderr
    return from_file(path)


def psql(db: Any, sql: Path, **variables: str) -> subprocess.CompletedProcess[str]:
    """One of the move's reviewed psql files, run by the operator against the Tasks database."""
    config = db.config
    env = {
        **os.environ, "PGHOST": config.host, "PGPORT": str(config.port), "PGUSER": config.user,
        "PGPASSWORD": config.password, "PGDATABASE": config.database,
    }
    defined = [arg for name, value in variables.items() for arg in ("-v", f"{name}={value}")]
    return subprocess.run(
        ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", *defined, "-f", str(sql)],
        env=env, capture_output=True, text=True, check=False,
    )


async def loaded(board: OldBoard, db: Any, tmp_path: Path) -> move.Export:
    """Step 3: the export on the operator's host, loaded into the staging schema."""
    path = tmp_path / "export.json"
    result = await board.export(path)
    assert result.returncode == 0, result.stderr
    load = await asyncio.to_thread(psql, db, LOAD_SQL, schema=target(db).staging_schema, file=str(path))
    assert load.returncode == 0, load.stderr
    export = await move.read_export(target(db))
    assert load.stdout == f"loaded export sha256 {export.sha256}\n"
    return export


async def stop_codes(board: OldBoard, tmp_path: Path) -> dict[str, list[str]]:
    return {stop.code: list(stop.subjects) for stop in move.stops(await export_of(board, tmp_path))}


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
    exported = await export_of(board, tmp_path)
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
    exported = await export_of(board, tmp_path)
    elsewhere = dataclasses.replace(destination_of(exported), team_id="other-team:board.example")

    with pytest.raises(move.MoveRefused, match="the destination other-team"):
        move.dataset(exported, elsewhere)


async def staged(board: OldBoard, live: Any, deployment: Deployment, tmp_path: Path,
                 ) -> tuple[move.Export, move.Destination, bytes]:
    """Steps 3 to 5: the export, staged for the team's installation, and its listing."""
    exported = await loaded(board, live, tmp_path)
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
    # The loaded export stays; nothing is staged.
    assert (await move.read_export(target(live))).tasks[0]["title"] == " "
    with pytest.raises(move.MoveRefused, match="is not staged"):
        await move.staged_listing(target(live))


async def test_a_retry_stages_only_from_a_new_load_after_a_discard(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="One")
    _, destination, _ = await staged(board, live, deployment, tmp_path)
    await board.create("alice", title="Two")

    with pytest.raises(move.MoveRefused, match="already staged"):
        await move.stage(target(live), await export_of(board, tmp_path), destination)
    assert await move.discard(target(live)) is True
    exported, destination, from_staging = await staged(board, live, deployment, tmp_path)

    assert from_staging == move.listing(move.dataset(exported, destination))
    assert [task["title"] for task in exported.tasks] == ["One", "Two"]


async def test_an_export_changed_after_loading_is_refused_under_its_hash(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    await board.create("alice", title="As exported")
    unchanged = await loaded(board, live, tmp_path)
    assert unchanged.tasks[0]["title"] == "As exported"
    await live.execute(
        f'UPDATE "{target(live).staging_schema}".board_move_export'
        " SET document = replace(document, 'As exported', 'Changed after')"
    )

    with pytest.raises(move.MoveRefused, match="does not match its sha256"):
        await move.read_export(target(live))


async def test_the_loader_refuses_a_staging_schema_that_exists(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    await board.create("alice", title="One")
    first = await loaded(board, live, tmp_path)
    await board.create("alice", title="Two")
    path = tmp_path / "second.json"
    assert (await board.export(path)).returncode == 0

    load = await asyncio.to_thread(psql, live, LOAD_SQL, schema=target(live).staging_schema, file=str(path))

    assert load.returncode != 0
    assert "already exists" in load.stderr
    assert (await move.read_export(target(live))).sha256 == first.sha256


async def test_a_board_larger_than_ours_loads_and_stages_whole(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    """Our board has about 700 tasks and more than 4,000 comments."""
    await board.db.execute(
        "INSERT INTO aweb.tasks (team_id, task_number, task_ref_suffix, title, description, created_by_alias)"
        " SELECT $1, n, 'big' || n, 'Task ' || n, repeat('d', 4000), 'alice' FROM generate_series(1, 750) n",
        board.team_id,
    )
    await board.db.execute(
        "INSERT INTO aweb.task_comments (task_id, team_id, author_alias, body)"
        " SELECT task_id, team_id, 'bob', repeat('c', 600) || k FROM aweb.tasks, generate_series(1, 6) k"
        " WHERE team_id = $1",
        board.team_id,
    )
    await board.db.execute(
        "INSERT INTO aweb.task_counters (team_id, next_number) VALUES ($1, 751)", board.team_id,
    )

    exported, destination, from_staging = await staged(board, live, deployment, tmp_path)

    assert (len(exported.tasks), len(exported.comments)) == (750, 4500)
    assert (tmp_path / "export.json").stat().st_size > 5_000_000
    assert exported.sha256 == hashlib.sha256(loaded_text(tmp_path / "export.json").encode()).hexdigest()
    assert from_staging == move.listing(move.dataset(exported, destination))


async def test_open_makes_the_approved_staging_the_installation_dataset(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    tasks = await full_board(board)
    exported, destination, from_staging = await staged(board, live, deployment, tmp_path)

    await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(from_staging), current=lambda: deployment.destination(board.team_id))

    data = move.dataset(exported, destination)
    assert await move.live_listing(target(live), destination.installation_id) == move.listing(data, source=False)
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
    assert await move.live_listing(target(live), destination.installation_id) != move.listing(data, source=False)


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
    await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved), current=lambda: deployment.destination(board.team_id))
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


async def opened(board: OldBoard, live: Any, deployment: Deployment, tmp_path: Path) -> move.Dataset:
    """Steps 3 to 6: the board moved into the team's installation."""
    exported, destination, approved = await staged(board, live, deployment, tmp_path)
    await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved),
                           current=lambda: deployment.destination(board.team_id))
    return move.dataset(exported, destination)


async def test_the_rollback_compare_lists_the_live_dataset_with_no_writer_left(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await full_board(board)
    data = await opened(board, live, deployment, tmp_path)

    compared = await move.live_listing(target(live), installed["installation_id"])

    assert compared == move.listing(data, source=False)


async def test_the_rollback_compare_refuses_while_another_session_is_in_a_transaction(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="Old")
    await opened(board, live, deployment, tmp_path)

    async with live.transaction() as other:
        # Inside a transaction that has not touched the dataset tables yet.
        await other.fetch_value("SELECT 1")
        with pytest.raises(move.MoveRefused, match="other sessions are inside a transaction"):
            await move.live_listing(target(live), installed["installation_id"])


async def test_the_rollback_compare_refuses_while_a_writer_holds_the_dataset(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="Old")
    await opened(board, live, deployment, tmp_path)

    async with live.transaction() as writer:
        await writer.execute(
            "UPDATE {{tables.tasks}} SET title = 'Being written' WHERE installation_id = $1",
            installed["installation_id"],
        )
        with pytest.raises(move.MoveRefused, match="lock timeout"):
            await move.live_listing(target(live), installed["installation_id"])


@pytest.mark.parametrize(("membership", "refusal"), [
    # No statistics privilege at all.
    ("", "cannot see other sessions"),
    # A member that does not inherit the privilege sees other users' xact_start as NULL.
    ("NOINHERIT", "cannot see other sessions"),
    # An inheriting member sees the other login's transaction.
    ("INHERIT", "other sessions are inside a transaction"),
])
async def test_the_rollback_compare_refuses_unless_it_sees_another_logins_transaction(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path,
    membership: str, refusal: str,
) -> None:
    await board.create("alice", title="Old")
    await opened(board, live, deployment, tmp_path)
    database = await live.fetch_value("SELECT current_database()")
    config = live.config
    roles = []

    async def login(options: str = "") -> str:
        role, password = f"board_move_probe_{uuid4().hex[:10]}", uuid4().hex
        await live.execute(f"CREATE ROLE {role} LOGIN {options} PASSWORD '{password}'")
        roles.append(role)
        # pgdbm's connect runs CREATE SCHEMA IF NOT EXISTS, which needs CREATE.
        await live.execute(f'GRANT CONNECT, CREATE ON DATABASE "{database}" TO {role}')
        await live.execute(f'GRANT USAGE ON SCHEMA "{live.schema}" TO {role}')
        await live.execute(f'GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA "{live.schema}" TO {role}')
        return f"postgresql://{role}:{password}@{config.host}:{config.port}/{database}"

    try:
        observer = await login(membership)
        if membership:
            await live.execute(f"GRANT pg_read_all_stats TO {roles[0]}")
        other = await asyncpg.connect(await login())
        try:
            # Another login, inside a transaction that has not touched the dataset tables.
            pending = other.transaction()
            await pending.start()
            await other.fetchval("SELECT 1")

            with pytest.raises(move.MoveRefused, match=refusal):
                await move.live_listing(move.Target(dsn=observer, live_schema=live.schema), installed["installation_id"])
            await pending.rollback()
        finally:
            await other.close()
    finally:
        for role in roles:
            await live.execute(f"DROP OWNED BY {role}")
            await live.execute(f"DROP ROLE {role}")


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
        await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved), current=lambda: deployment.destination(board.team_id))

    assert await move.staged_listing(target(live)) == changed
    assert await move.live_listing(target(live), destination.installation_id) == b""


async def test_open_refuses_a_stage_replaced_after_its_listing_was_approved(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="One")
    _, destination, approved = await staged(board, live, deployment, tmp_path)
    await move.discard(target(live))
    await board.create("alice", title="Two")
    _, _, replacement = await staged(board, live, deployment, tmp_path)

    with pytest.raises(move.MoveRefused, match="does not list as the approved listing"):
        await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved), current=lambda: deployment.destination(board.team_id))

    assert await move.staged_listing(target(live)) == replacement
    assert await move.live_listing(target(live), destination.installation_id) == b""


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
    async with cli.destination_of(exported.team_id, deployment.settings, app_options(coordination)) as now:
        assert now.generation == reinstalled["generation"] != staged_for.generation

        async def current() -> move.Destination:
            return now

        with pytest.raises(move.MoveRefused, match="was staged for"):
            await move.open_staged(target(live), now, listing_sha256=move.listing_digest(approved), current=current)

    assert await move.staged_listing(target(live)) == approved


@pytest.mark.parametrize(("scopes", "refusal"), [
    (["tasks.read"], "changed while open waited"),
    (["tasks.read", "tasks.write", "tasks.override"], "not only tasks.read"),
])
async def test_open_refuses_an_installation_changed_while_it_waited_for_the_staging_locks(
    board: OldBoard, live: Any, deployment: Deployment, coordination: Any, installed: dict,
    tmp_path: Path, scopes: list[str], refusal: str,
) -> None:
    await board.create("alice", title="One")
    exported, destination, approved = await staged(board, live, deployment, tmp_path)

    async def current() -> move.Destination:
        async with cli.destination_of(exported.team_id, deployment.settings, app_options(coordination)) as now:
            return now

    async with live.transaction() as writer:
        # A writer on the staging tables: open waits for it.
        await writer.execute(f'LOCK TABLE "{target(live).staging_schema}".tasks IN ROW EXCLUSIVE MODE')
        opening = asyncio.ensure_future(move.open_staged(
            target(live), destination, listing_sha256=move.listing_digest(approved), current=current,
        ))
        await waiting_on_locks(live, 1)
        await deployment.install(board.team_id, scopes, expected_generation=installed["generation"])

    with pytest.raises(move.MoveRefused, match=refusal):
        await opening
    assert await move.staged_listing(target(live)) == approved
    assert await move.live_listing(target(live), destination.installation_id) == b""


async def test_open_refuses_an_installation_that_already_has_rows(
    board: OldBoard, live: Any, deployment: Deployment, installed: dict, tmp_path: Path
) -> None:
    await board.create("alice", title="Old")
    _, destination, approved = await staged(board, live, deployment, tmp_path)
    await app_store(live, destination, "alice").create(
        title="Written in the app first", body="", type="task", priority=2, labels=[], parent=None,
        assignee=None,
    )
    before = await move.live_listing(target(live), destination.installation_id)

    with pytest.raises(move.MoveRefused, match="already has"):
        await move.open_staged(target(live), destination, listing_sha256=move.listing_digest(approved), current=lambda: deployment.destination(board.team_id))

    assert await move.live_listing(target(live), destination.installation_id) == before
    assert await move.staged_listing(target(live)) == approved


def app_options(coordination: Any) -> dict[str, Any]:
    """How the app reaches the in-process registry and server."""
    return {
        "awid_transport": coordination.registry.transport,
        "awid_txt_resolver": dns_naming(DOMAIN, NAMESPACE_KEY),
        "team_server_transport": coordination.server.transport,
    }


async def command(deployment: Deployment, coordination: Any, *args: str) -> str:
    """One operator job, in-process, with the deployment's settings; the app
    reaches the in-process registry and server through their transports."""
    out = io.StringIO()
    status = await cli.run(
        list(args), settings=deployment.settings, out=out, app_options=app_options(coordination),
    )
    assert status == 0
    return out.getvalue()


async def fetched(live: Any, name: str) -> bytes:
    """A stored listing, fetched by the operator with the reviewed psql file."""
    result = await asyncio.to_thread(psql, live, FETCH_SQL, schema=target(live).staging_schema, name=name)
    assert result.returncode == 0, result.stderr
    return base64.b64decode(result.stdout)


async def test_the_operator_moves_the_board_with_the_jobs(
    board: OldBoard, live: Any, deployment: Deployment, coordination: Any, installed: dict,
    tmp_path: Path,
) -> None:
    await full_board(board)
    exported = await loaded(board, live, tmp_path)

    checked = await command(deployment, coordination, "check")
    assert checked == f"no stops in export sha256 {exported.sha256}\n"
    export_line = await command(deployment, coordination, "list-export")
    staged_line = await command(deployment, coordination, "stage")
    assert staged_line == (
        f"staged {installed['installation_id']} generation {installed['generation']}"
        f" in {target(live).staging_schema}\n"
    )
    staging_lines = (await command(deployment, coordination, "list-staged")).splitlines()

    # The verifier compares what the operator fetched with the digests in the jobs' logs.
    from_export, from_staging = await fetched(live, "export"), await fetched(live, "staged")
    assert from_export == from_staging
    assert export_line == f"export {move.digest_line(from_export)}\n"
    assert staging_lines[0] == f"staged {move.digest_line(from_staging)}"
    assert installed["installation_id"].encode() in from_staging
    assert exported.sha256.encode() in from_staging
    # The rollback baseline, derived from the approved listing: its first lines.
    dataset_lines = int(staging_lines[1].rsplit(" ", 1)[1])
    baseline = b"".join(from_staging.splitlines(keepends=True)[:dataset_lines])
    assert staging_lines[1] == f"dataset {move.digest_line(baseline)}"
    assert all(b'"table":"source"' in line for line in from_staging.splitlines()[dataset_lines:])
    digest = hashlib.sha256(from_staging).hexdigest()

    await command(deployment, coordination, "open", "--listing-sha256", digest)

    live_line = await command(deployment, coordination, "list-live", "--installation-id", installed["installation_id"])
    assert live_line == f"{move.digest_line(baseline)}\n"


def run(live: Any, *args: str) -> subprocess.CompletedProcess[bytes]:
    """An operator command as a process, with the deployment's database settings."""
    env = {**os.environ, "TASKS_DATABASE_URL": live.config.get_dsn(), "TASKS_DB_SCHEMA": live.schema}
    return subprocess.run(
        [sys.executable, "-m", "aweb_tasks.board_move", *args],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, check=False,
    )


async def test_the_jobs_exit_with_the_stop_or_the_refusal(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    task = await board.create("alice", title=" ")
    await loaded(board, live, tmp_path)

    checked = await asyncio.to_thread(run, live, "check")
    assert checked.returncode == 2
    assert json.loads(checked.stdout) == {
        "code": "blank_title", "reason": "a task's title is blank, which the app refuses",
        "subjects": [task["task_ref"]],
    }
    listed = await asyncio.to_thread(run, live, "list-staged")
    assert (listed.returncode, listed.stderr) == (
        3, f"refused: {target(live).staging_schema} is not staged\n".encode(),
    )
    discarded = await asyncio.to_thread(run, live, "discard")
    assert (discarded.returncode, discarded.stdout) == (
        0, f"{target(live).staging_schema} dropped\n".encode(),
    )
    missing = await asyncio.to_thread(run, live, "check")
    assert (missing.returncode, missing.stderr) == (
        3, f"refused: {target(live).staging_schema} does not exist: load the export first\n".encode(),
    )
    assert not await staging_exists(live)
