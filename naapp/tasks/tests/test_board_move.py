"""The one-off board move (spec section 8, steps 3 to 6) against a real board.

The source is a team's board on the aweb server release production runs,
written through its own routes; the export is the operator's psql query run
against that server's database; the target is a real Tasks database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from awid.team_ids import team_slug
from pgdbm import AsyncMigrationManager

from aweb_tasks.app import MIGRATIONS_PATH
from board_move import move

from .old_board import OldBoard, old_board
from .test_store import store


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


INSTALLATION = "installation-1"
APP_ID = "tasks"


@pytest.fixture
async def live(tasks_db: Any) -> Any:
    """The installation's live Tasks tables, as the app's own migrations build them."""
    await AsyncMigrationManager(
        tasks_db, migrations_path=str(MIGRATIONS_PATH), module_name="tasks"
    ).apply_pending_migrations()
    return tasks_db


def target(live: Any) -> move.Target:
    return move.Target(dsn=live.config.get_dsn(), live_schema=live.schema)


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


async def test_the_staged_board_lists_exactly_as_its_export(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    await full_board(board)
    exported = move.Export(await export(board, tmp_path))

    await move.stage(target(live), exported, installation_id=INSTALLATION, app_id=APP_ID)

    expected = move.listing(move.dataset(exported, installation_id=INSTALLATION, app_id=APP_ID))
    assert await move.staged_listing(target(live), INSTALLATION) == expected
    # Staging is out of reach of the installation's members: the live tables hold none of it.
    for table in ("installations", "tasks", "claims", "locks", "comments", "dependencies", "events"):
        assert await live.fetch_value(f"SELECT count(*) FROM {{{{tables.{table}}}}}") == 0, table


async def test_a_staged_row_that_differs_from_the_export_shows_in_the_listing(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    tasks = await full_board(board)
    exported = move.Export(await export(board, tmp_path))
    await move.stage(target(live), exported, installation_id=INSTALLATION, app_id=APP_ID)
    expected = move.listing(move.dataset(exported, installation_id=INSTALLATION, app_id=APP_ID))

    await live.execute(
        f'UPDATE "{target(live).staging_schema}".tasks SET title = $1 WHERE number = $2',
        "Written by someone else", tasks["script"]["task_number"],
    )

    assert await move.staged_listing(target(live), INSTALLATION) != expected


async def test_the_dataset_follows_each_mapping_rule(board: OldBoard, tmp_path: Path) -> None:
    tasks = await full_board(board)
    exported = move.Export(await export(board, tmp_path))
    by_id = {task["task_id"]: task for task in exported.tasks}
    number = {name: task["task_number"] for name, task in tasks.items()}

    data = move.dataset(exported, installation_id=INSTALLATION, app_id=APP_ID)
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
    assert {e["actor"]["app"] for e in rows["events"]} == {APP_ID}
    assert {e["cause"] for e in rows["events"]} == {"board-move"}
    assert [(c["author"], c["body"]) for c in rows["comments"]] == [
        ({"alias": "bob"}, "Taking it"), ({"alias": "alice"}, "Thanks"),
    ]
    assert rows["dependencies"] == [
        {"installation_id": INSTALLATION, "number": number["script"], "waits_on": number["chore"]},
    ]
    # What the app does not hold is in the listing's source facts.
    kinds = [record["kind"] for record in data.source]
    assert kinds == ["export", "root_task_counter", "deleted_task", "expired_lock"]
    assert data.source[1]["next_number"] == exported.document["root_task_counter"]
    assert data.source[2]["ref"] == tasks["gone"]["task_ref"]
    assert data.source[3]["resource_key"] == "old/lock"


async def test_stage_refuses_an_export_with_stops(board: OldBoard, live: Any, tmp_path: Path) -> None:
    await board.create("alice", title=" ")
    exported = move.Export(await export(board, tmp_path))

    with pytest.raises(move.MoveRefused, match="blank_title"):
        await move.stage(target(live), exported, installation_id=INSTALLATION, app_id=APP_ID)
    assert not await live.fetch_value(
        "SELECT 1 FROM pg_namespace WHERE nspname = $1", target(live).staging_schema
    )


async def test_a_retry_stages_only_after_the_last_stage_is_discarded(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    await board.create("alice", title="One")
    first = move.Export(await export(board, tmp_path))
    await move.stage(target(live), first, installation_id=INSTALLATION, app_id=APP_ID)
    await board.create("alice", title="Two")
    second = move.Export(await export(board, tmp_path))

    with pytest.raises(move.MoveRefused, match="exists"):
        await move.stage(target(live), second, installation_id=INSTALLATION, app_id=APP_ID)
    assert await move.discard(target(live)) is True
    await move.stage(target(live), second, installation_id=INSTALLATION, app_id=APP_ID)

    assert await move.staged_listing(target(live), INSTALLATION) == move.listing(
        move.dataset(second, installation_id=INSTALLATION, app_id=APP_ID)
    )


async def test_open_makes_the_staged_board_the_installation_dataset(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    tasks = await full_board(board)
    exported = move.Export(await export(board, tmp_path))
    data = move.dataset(exported, installation_id=INSTALLATION, app_id=APP_ID)
    await move.stage(target(live), exported, installation_id=INSTALLATION, app_id=APP_ID)

    await move.open_staged(target(live), INSTALLATION)

    assert await move.live_listing(target(live), INSTALLATION) == move.listing(data, source=False)
    assert not await live.fetch_value(
        "SELECT 1 FROM pg_namespace WHERE nspname = $1", target(live).staging_schema
    )
    # The app itself reads and writes the moved board.
    alice = store(live, "alice")
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
    assert await move.live_listing(target(live), INSTALLATION) != move.listing(data, source=False)


async def test_open_refuses_an_installation_that_already_has_rows(
    board: OldBoard, live: Any, tmp_path: Path
) -> None:
    await board.create("alice", title="Old")
    exported = move.Export(await export(board, tmp_path))
    await move.stage(target(live), exported, installation_id=INSTALLATION, app_id=APP_ID)
    await store(live, "alice").create(
        title="Written in the app first", body="", type="task", priority=2, labels=[], parent=None,
        assignee=None,
    )
    before = await move.live_listing(target(live), INSTALLATION)

    with pytest.raises(move.MoveRefused, match="already has"):
        await move.open_staged(target(live), INSTALLATION)

    assert await move.live_listing(target(live), INSTALLATION) == before
    assert await move.staged_listing(target(live), INSTALLATION) == move.listing(
        move.dataset(exported, installation_id=INSTALLATION, app_id=APP_ID)
    )


async def test_open_refuses_another_installation(board: OldBoard, live: Any, tmp_path: Path) -> None:
    await board.create("alice", title="Old")
    exported = move.Export(await export(board, tmp_path))
    await move.stage(target(live), exported, installation_id=INSTALLATION, app_id=APP_ID)

    with pytest.raises(move.MoveRefused, match="staged for"):
        await move.open_staged(target(live), "inst-other")

    assert await move.live_listing(target(live), "inst-other") == b""
