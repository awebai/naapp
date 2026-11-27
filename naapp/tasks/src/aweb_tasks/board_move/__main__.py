"""The board move's commands, run as one-off jobs of the Tasks service:

    python -m aweb_tasks.board_move <command> ...

The environment is the Tasks deployment's own TASKS_* configuration: its
database, and the registry and grant-read key through which every command
that names the installation reads the exported team's live grant. A listing
goes to standard output. Exit status 2 is a stop on the export, 3 a step the
move refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, BinaryIO

from aweb_tasks.app import create_app
from aweb_tasks.board_move import move
from aweb_tasks.config import TasksSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aweb_tasks.board_move")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "list the export's stops"),
        ("list-export", "the canonical listing of the export, for its team's installation"),
        ("stage", "stage the export in the staging schema, for its team's installation"),
        ("list-live", "the canonical listing of the export's team's live dataset"),
        ("open", "copy the approved staged rows into the live tables and drop the staging schema"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("export", type=Path)
        if name == "list-export":
            command.add_argument(
                "--dataset-only", action="store_true",
                help="without the source facts, as the live dataset lists after opening",
            )
        if name == "open":
            command.add_argument(
                "--listing-sha256", required=True,
                help="the SHA-256 of the staging listing the verifier approved",
            )
    commands.add_parser("list-staged", help="the canonical listing of the staging schema")
    commands.add_parser("discard", help="drop the staging schema")
    return parser


@asynccontextmanager
async def destination_of(
    export: move.Export, settings: TasksSettings, app_options: dict[str, Any]
) -> AsyncIterator[move.Destination]:
    """The export's team's installation, read afresh as the running app reads it."""
    app = create_app(settings, **app_options)
    runtime = app.state.runtime
    async with app.router.lifespan_context(app):
        yield await move.destination(
            runtime.installation_grants(), team_id=export.team_id, app_id=runtime.manifest.app_id
        )


async def run(
    argv: list[str], *, settings: TasksSettings, out: BinaryIO,
    app_options: dict[str, Any] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    target = move.Target(dsn=settings.database_url, live_schema=settings.db_schema)
    if args.command == "list-staged":
        out.write(await move.staged_listing(target))
        return 0
    if args.command == "discard":
        existed = await move.discard(target)
        out.write(f"{target.staging_schema} {'dropped' if existed else 'did not exist'}\n".encode())
        return 0
    export = move.Export.read(args.export)
    if args.command == "check":
        stops = move.stops(export)
        for stop in stops:
            out.write(json.dumps(dataclasses.asdict(stop), sort_keys=True).encode() + b"\n")
        if stops:
            return 2
        out.write(b"no stops\n")
        return 0
    async with destination_of(export, settings, app_options or {}) as destination:
        if args.command == "list-export":
            out.write(move.listing(move.dataset(export, destination), source=not args.dataset_only))
        elif args.command == "stage":
            await move.stage(target, export, destination)
            out.write(f"staged {destination.installation_id} in {target.staging_schema}\n".encode())
        elif args.command == "list-live":
            out.write(await move.live_listing(target, destination))
        elif args.command == "open":
            await move.open_staged(target, destination, listing_sha256=args.listing_sha256)
            out.write(f"opened {destination.installation_id}; {target.staging_schema} dropped\n".encode())
    return 0


def main() -> int:
    try:
        return asyncio.run(run(sys.argv[1:], settings=TasksSettings(), out=sys.stdout.buffer))
    except move.MoveRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
