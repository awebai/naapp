"""The board move's commands, run as one-off jobs of the Tasks service:

    python -m aweb_tasks.board_move <command> ...

A job inherits the deployment's own TASKS_* configuration: its database, and
the registry and grant-read key through which every command that names the
installation reads the exported team's live grant. Every command reads the
export the operator loaded into the staging schema. A listing is stored there,
for the operator to fetch, and the job's log shows only its digest line. Exit
status 2 is a stop on the export, 3 a step the move refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TextIO

from aweb_tasks.app import create_app
from aweb_tasks.board_move import move
from aweb_tasks.config import TasksSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aweb_tasks.board_move")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="the export's stops")
    commands.add_parser(
        "list-export", help="store the export's canonical listing, for its team's installation"
    )
    commands.add_parser("stage", help="stage the export, for its team's installation")
    commands.add_parser("list-staged", help="store the staging schema's canonical listing")
    opening = commands.add_parser(
        "open", help="copy the approved staged rows into the live tables and drop the staging schema"
    )
    opening.add_argument(
        "--listing-sha256", required=True, help="the SHA-256 of the staging listing the verifier approved"
    )
    commands.add_parser(
        "list-live", help="the digest line of the live dataset, once no other session is writing"
    ).add_argument("--installation-id", required=True, help="the installation the approved listing names")
    commands.add_parser("discard", help="drop the staging schema, export included")
    return parser


@asynccontextmanager
async def destination_of(
    team_id: str, settings: TasksSettings, app_options: dict[str, Any]
) -> AsyncIterator[move.Destination]:
    """The team's installation, read afresh as the running app reads it."""
    app = create_app(settings, **app_options)
    runtime = app.state.runtime
    async with app.router.lifespan_context(app):
        yield await move.destination(
            runtime.installation_grants(), team_id=team_id, app_id=runtime.manifest.app_id
        )


async def run(
    argv: list[str], *, settings: TasksSettings, out: TextIO,
    app_options: dict[str, Any] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    target = move.Target(dsn=settings.database_url, live_schema=settings.db_schema)
    if args.command == "discard":
        existed = await move.discard(target)
        print(f"{target.staging_schema} {'dropped' if existed else 'did not exist'}", file=out)
        return 0
    if args.command == "list-live":
        print(move.digest_line(await move.live_listing(target, args.installation_id)), file=out)
        return 0
    if args.command == "list-staged":
        staged = await move.staged_listing(target)
        print("staged " + await move.store_listing(target, "staged", staged), file=out)
        # The rollback baseline: the listing's first lines, before its source facts.
        print("dataset " + move.digest_line(move.dataset_part(staged)), file=out)
        return 0
    export = await move.read_export(target)
    if args.command == "check":
        stops = move.stops(export)
        for stop in stops:
            print(json.dumps(dataclasses.asdict(stop), sort_keys=True), file=out)
        if stops:
            return 2
        print(f"no stops in export sha256 {export.sha256}", file=out)
        return 0
    async with destination_of(export.team_id, settings, app_options or {}) as destination:
        if args.command == "list-export":
            data = move.listing(move.dataset(export, destination))
            print("export " + await move.store_listing(target, "export", data), file=out)
        elif args.command == "stage":
            await move.stage(target, export, destination)
            print(f"staged {destination.installation_id} generation {destination.generation}"
                  f" in {target.staging_schema}", file=out)
        elif args.command == "open":
            async def current() -> move.Destination:
                async with destination_of(export.team_id, settings, app_options or {}) as now:
                    return now

            await move.open_staged(
                target, destination, listing_sha256=args.listing_sha256, current=current,
            )
            print(f"opened {destination.installation_id}; {target.staging_schema} dropped", file=out)
    return 0


def main() -> int:
    try:
        return asyncio.run(run(sys.argv[1:], settings=TasksSettings(), out=sys.stdout))
    except move.MoveRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
