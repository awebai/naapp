"""Reads the board export (step 3) and judges whether it may be moved.

An export is the JSON document ``export.sql`` writes. A stop names a state the
move must not decide by itself: it needs an explicit disposition, with its
reason, before the board is moved.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awid.team_ids import team_slug

EXPORT_FORMAT = "aweb-board-export/1"
# The old store's types the app has, and the one naapps ruled into "task".
KNOWN_TYPES = {"task", "bug", "feature", "epic", "chore"}


class ExportError(ValueError):
    """The file is not an export this script reads."""


@dataclass(frozen=True)
class Stop:
    code: str
    reason: str
    subjects: tuple[str, ...]


class Export:
    """One team's frozen board, as the export query read it."""

    def __init__(self, document: dict[str, Any]) -> None:
        if document.get("format") != EXPORT_FORMAT:
            raise ExportError(f"not an {EXPORT_FORMAT} document")
        self.document = document
        self.team_id: str = document["team_id"]
        self.tasks: list[dict[str, Any]] = document["tasks"]
        self.comments: list[dict[str, Any]] = document["comments"]
        self.dependencies: list[dict[str, Any]] = document["dependencies"]
        self.claims: list[dict[str, Any]] = document["claims"]
        self.reservations: list[dict[str, Any]] = document["reservations"]
        self._by_id = {task["task_id"]: task for task in self.tasks}

    @classmethod
    def read(cls, path: Path) -> Export:
        return cls(json.loads(path.read_text(encoding="utf-8")))

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
