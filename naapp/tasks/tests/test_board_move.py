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
