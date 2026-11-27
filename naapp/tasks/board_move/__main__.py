"""The board move's commands, run by the operator from naapp/tasks:

    uv run python -m board_move <command> ...

The Tasks database is the deployment's: TASKS_DATABASE_URL and TASKS_DB_SCHEMA.
A listing goes to standard output. Exit status 2 is a stop on the export,
3 a step the move refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from aweb_tasks.config import TasksSettings
from board_move import move


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m board_move")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="list the export's stops")
    check.add_argument("export", type=Path)
    for name, help_text in (
        ("list-export", "the canonical listing of the export"),
        ("stage", "stage the export in the staging schema"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("export", type=Path)
        command.add_argument("--installation-id", required=True)
        command.add_argument("--app-id", required=True)
        if name == "list-export":
            command.add_argument(
                "--dataset-only", action="store_true",
                help="without the source facts, as the live dataset lists after opening",
            )
    for name, help_text in (
        ("list-staged", "the canonical listing of the staging schema"),
        ("open", "copy the staged rows into the live tables and drop the staging schema"),
        ("list-live", "the canonical listing of the installation's live dataset"),
    ):
        commands.add_parser(name, help=help_text).add_argument("--installation-id", required=True)
    commands.add_parser("discard", help="drop the staging schema")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "check":
        stops = move.stops(move.Export.read(args.export))
        for stop in stops:
            print(json.dumps(dataclasses.asdict(stop), sort_keys=True))
        if stops:
            return 2
        print("no stops")
        return 0
    if args.command == "list-export":
        data = move.dataset(
            move.Export.read(args.export), installation_id=args.installation_id, app_id=args.app_id
        )
        sys.stdout.buffer.write(move.listing(data, source=not args.dataset_only))
        return 0
    settings = TasksSettings()
    target = move.Target(dsn=settings.database_url, live_schema=settings.db_schema)
    if args.command == "stage":
        await move.stage(
            target, move.Export.read(args.export),
            installation_id=args.installation_id, app_id=args.app_id,
        )
        print(f"staged in {target.staging_schema}")
    elif args.command == "list-staged":
        sys.stdout.buffer.write(await move.staged_listing(target, args.installation_id))
    elif args.command == "open":
        await move.open_staged(target, args.installation_id)
        print(f"opened {args.installation_id}; {target.staging_schema} dropped")
    elif args.command == "list-live":
        sys.stdout.buffer.write(await move.live_listing(target, args.installation_id))
    elif args.command == "discard":
        existed = await move.discard(target)
        print(f"{target.staging_schema} {'dropped' if existed else 'did not exist'}")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run(_parser().parse_args()))
    except move.MoveRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
