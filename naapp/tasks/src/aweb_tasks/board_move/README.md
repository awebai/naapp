# The board move (one-off)

This moves our own team's board from the aweb server's task store into the
Tasks app, once. It carries out steps 3 to 6 of section 8 of the Tasks app
specification (draft 9).
- It ships in the app's image only for the move's releases, so that it runs inside the Tasks deployment. The deployment's keys never leave it.
- The change made after the old board is archived (step 7) removes it.

**Three parts:**
- The operator runs `export.sql` against the aweb server's database on her host, and loads the export into the Tasks database with `load_export.sql`.
- Every other step is a one-off job of the Tasks service: `python -m aweb_tasks.board_move <command>`.
- Someone other than the script's author verifies step 5.

## What a job needs

A job inherits the Tasks service's own environment. Nothing is set by hand:
- `TASKS_DATABASE_URL`: a role that can create and drop a schema in the Tasks database.
- `TASKS_DB_SCHEMA`: the live schema; `public` when unset.
- `TASKS_APP_ID` and `TASKS_PUBLIC_ORIGIN`: the deployment's app id and origin.
- `TASKS_AWID_REGISTRY_URL` with `TASKS_AWID_SERVICE_TOKEN` or `TASKS_AWID_MEMBER_CREDENTIALS`: the registry.
- `TASKS_APP_KEY_FILES`: the deployment's keys, including the grant-read key.
- `TASKS_APP_EVENTS_ORIGIN`: the team's aweb server, which the app's runtime is started with.

**The staging schema is `<TASKS_DB_SCHEMA>_board_move`,** for example `public_board_move`. The operator's psql files take it as `-v schema=...`, and their database URL is the Tasks database owner URL.

**The destination is never typed in.** Every job that names the installation reads the exported team's live grant with the deployment's grant-read key, as the running app does, and starts the app's runtime to do so. It refuses (exit 3) unless that grant is active and grants exactly `tasks.read`. The listings name the installation id and generation it found.

**A job's log is its receipt.** A listing is stored in the staging schema, and the log shows one line for it: `<name> sha256 <hex> bytes <n> lines <m>`. The operator fetches the stored listing with `fetch_listing.sql`, and the verifier recomputes its sha256 against that line. Exit status 2 is a stop on the export; 3 is a step the move refuses.

## Before step 3

- **The Tasks service runs this commit.** The job's image contains the script.
- **Step 1:** the app is installed on the team, granting only `tasks.read`.
- **Step 2:** every writer is stopped, and that has been shown.

## Step 3: export, load and check

On the operator's host. The export is read-only and runs in one snapshot, against the aweb server's database:

    psql "<aweb database URL>" -X -q -At -v ON_ERROR_STOP=1 -v team_id=<team id> \
      -f naapp/tasks/src/aweb_tasks/board_move/export.sql -o export.json

The query stops, writing nothing, if any task table has columns other than those of aweb 1.27.55.

    psql "<Tasks database owner URL>" -X -q -At -v ON_ERROR_STOP=1 \
      -v schema=<staging schema> -v file=export.json -f naapp/tasks/src/aweb_tasks/board_move/load_export.sql

- This creates the staging schema and loads the export into it.
- It prints `loaded export sha256 <hex>`, the sha256 of the file's text without its final newline: `printf %s "$(cat export.json)" | shasum -a 256`.
- It refuses if the staging schema exists.

Then, as a job:

    python -m aweb_tasks.board_move check

- Prints `no stops in export sha256 <hex>`, or one JSON line per stop (`code`, `reason`, `subjects`) and exits 2.
- A stop needs an explicit disposition with its reason before the board moves. The script never decides one by itself.
- After a disposition, either the source is put right and a new export is loaded (discard first), or a reviewed change to the script handles the stop.

## Step 4: stage

    python -m aweb_tasks.board_move stage

- This writes the board into the staging schema, built by the app's own migrations.
- Nothing in the live tables changes, so `tasks.read` can't reach the staged rows.
- It refuses (exit 3) if the export has stops, if the team's grant isn't active and read-only, or if the schema is already staged.
- A stage that fails discards the schema, export included.

## Step 5: verify

As jobs:

    python -m aweb_tasks.board_move list-export
    python -m aweb_tasks.board_move list-staged
    python -m aweb_tasks.board_move list-export --dataset-only

They store the listings `export`, `staged` and `export-dataset`. The operator fetches each with:

    psql "<Tasks database owner URL>" -X -q -At -v ON_ERROR_STOP=1 -v schema=<staging schema> \
      -v name=<export | staged | export-dataset> -f naapp/tasks/src/aweb_tasks/board_move/fetch_listing.sql \
      | base64 --decode > <name>.jsonl

**`export` and `staged` must be identical, byte for byte, and each must match its job's log line.** One line per row, in a fixed order:
- the counter
- tasks
- claims
- locks
- comments
- dependencies
- the event log
- then the ref-to-number map
- then the source facts the app doesn't hold:
  - the destination: team, app id, installation id and generation
  - the export: its team, its sha256, the export time and the board's last write time
  - the root task counter
  - each deleted task
  - each expired lock

Read the board's last write time again with a fresh export, as a cross-check.

**What the comparison proves.** The export-side listing goes through the same mapping the import used. So equal listings prove the staged rows are what the mapping produces from this export. They don't prove the mapping is right. The verifier reads the mapping in `move.py` (`dataset`) against the rulings listed in the pull request.

**The verifier approves the staging listing by its sha256.** Step 6 opens only the staging contents with that digest. Keep the `export-dataset` log line: the rollback check compares against it once the staging schema is gone.

**If verification fails,** discard. The old board stays authoritative, and a retry starts from a new export and load:

    python -m aweb_tasks.board_move discard

## Step 6: open

    python -m aweb_tasks.board_move open --listing-sha256 <the approved digest>

It reads the team's grant afresh, then in one transaction:
- locks the staging tables, so nothing changes them until the transaction ends;
- refuses unless the staging lists with the approved digest, was staged for the installation and generation the grant names now, and the installation has no live rows;
- copies the staged rows into the live tables;
- drops the staging schema.

A refusal copies and drops nothing. Then grant `tasks.write` and `tasks.override`; our agents switch to `aw tasks`.

## Rolling back

**The rollback boundary is the app's first write after opening.** To roll back before it, in this order:

1. **Suspend the Tasks service** with `aweb_naapp.ops prod-suspend`. That target depends on its own task and doesn't exist yet: no ad hoc call stands in for it. Nothing new is admitted, and every admitted request's transaction ends, committed or rolled back.
2. **Set the installation's grant back to `tasks.read` only.**
3. **Stay suspended for at least 60 seconds after step 2.** A process judges scopes against the grant answer it last read, and reuses an answer for at most 60 seconds. That bound is aweb's `get_app_grant_max_age_seconds` (aweb `config.py`): 60 by default, and the server refuses values outside 1..60. The installation fence row holds no scopes.
   - This step does not rely on what Render does on resume.
   - `tests/test_board_move.py::test_a_rolled_back_installation_refuses_every_write_once_resumed` shows that a process reading the read-only grant refuses the write. A process that had already read the write grant kept admitting writes from it.
4. **Compare, while still suspended:**

       python -m aweb_tasks.board_move list-live --team-id <team id>

   Its `live` line must equal the `export-dataset` line kept at step 5. If it does, no write committed. The job reads the grant, so it runs after step 2.
5. **Return the agents to the old board,** which is untouched.
6. **Resume** with `aweb_naapp.ops prod-resume`, which also waits on its own task. Then check:
   - `/live` answers;
   - the build SHA is this commit;
   - one probe write (`aw tasks create`) is refused with `scope_denied`.

   The installation stays read-only.

## What the move carries

- **Numbers and the saved task counter** (`task_counters`) are kept. Each task's old id and ref go into its first history entry, `imported`.
- **Actors are carried exactly as stored,** with nothing derived. No claim and no lock is imported as live: each live one becomes a history entry whose holder is not proven.
- **Not carried:** the server's revision counters (`rev`, `question_rev`), `root_task_seq`, `picked_up` and comment ids. The root task counter is in the listing's source facts.
