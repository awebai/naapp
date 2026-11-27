# The board move (one-off)

This moves our own team's board from the aweb server's task store into the
Tasks app, once. It carries out steps 3 to 6 of section 8 of the Tasks app
specification (draft 9). It isn't part of the app or its image, and it is
deleted once the old board is archived (step 7).

The operator runs the steps, in order, from `naapp/tasks` in a checkout of the
landed commit. Someone other than the script's author verifies step 5.

## Before step 3

- **Step 1:** the app is installed on the team, granting only `tasks.read`. Note the installation id.
- **Step 2:** every writer is stopped, and that has been shown.
- The Tasks database settings are in the environment:
  - `TASKS_DATABASE_URL`: a role that can create and drop a schema in the Tasks database.
  - `TASKS_DB_SCHEMA`: the deployment's live schema; `public` when unset.
- **The staging schema is `<TASKS_DB_SCHEMA>_board_move`,** for example `public_board_move`. It must not exist before step 4.

## Step 3: export

This step is read-only and runs in one snapshot, against the aweb server's database:

    psql -X -q -At -v ON_ERROR_STOP=1 -v team_id=<team id> -f board_move/export.sql -o export.json

The query stops, writing nothing, if any task table has columns other than those of aweb 1.27.55. Keep `export.json`: every later step reads it.

    uv run python -m board_move check export.json

- Prints `no stops`, or one JSON line per stop (`code`, `reason`, `subjects`) and exits 2.
- A stop needs an explicit disposition with its reason before the board moves. The script never decides one by itself.
- After a disposition, either the source is put right and the export is taken again, or a reviewed change to the script handles the stop.

## Step 4: stage

    uv run python -m board_move stage export.json --installation-id <id> --app-id tasks

- This builds the staging schema with the app's own migrations and writes the board into it.
- Nothing in the live tables changes, so `tasks.read` can't reach the staged rows.
- It refuses (exit 3) if the export has stops or the staging schema already exists.

## Step 5: verify

    uv run python -m board_move list-export export.json --installation-id <id> --app-id tasks > from-export.jsonl
    uv run python -m board_move list-staged --installation-id <id> > from-staging.jsonl
    cmp from-export.jsonl from-staging.jsonl

**The two listings must be identical, byte for byte.** One line per row, in a fixed order:
- the counter
- tasks
- claims
- locks
- comments
- dependencies
- the event log
- then the ref-to-number map
- then the source facts the app doesn't hold:
  - the export time and the board's last write time
  - the root task counter
  - each deleted task
  - each expired lock

Read the board's last write time again with a fresh export, as a cross-check.

**What the comparison proves.** The export-side listing goes through the same mapping the import used. So equal listings prove the staged rows are what the mapping produces from this export. They don't prove the mapping is right. The verifier reads the mapping in `move.py` (`dataset`) against the rulings listed in the pull request.

**If verification fails,** discard the staged data. The old board stays authoritative, and a retry starts from a new export:

    uv run python -m board_move discard

## Step 6: open

    uv run python -m board_move open --installation-id <id>

In one transaction this does three things:
- it refuses unless the staging schema is staged for this installation and the installation has no live rows;
- it copies the staged rows into the live tables;
- it drops the staging schema.

Then grant `tasks.write` and `tasks.override`.

**The rollback boundary is the app's first write after opening.** To confirm that no write has committed:

    uv run python -m board_move list-export export.json --installation-id <id> --app-id tasks --dataset-only > dataset.jsonl
    uv run python -m board_move list-live --installation-id <id> > live.jsonl
    cmp dataset.jsonl live.jsonl

The live dataset lists exactly as staged until the app's first write.

## What the move carries

- **Numbers and the saved task counter** (`task_counters`) are kept. Each task's old id and ref go into its first history entry, `imported`.
- **Actors are carried exactly as stored,** with nothing derived. No claim and no lock is imported as live: each live one becomes a history entry whose holder is not proven.
- **Not carried:** the server's revision counters (`rev`, `question_rev`), `root_task_seq`, `picked_up` and comment ids. The root task counter is in the listing's source facts.
