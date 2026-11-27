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

from board_move import move

from .old_board import OldBoard, old_board


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
