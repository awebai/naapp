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

**The staging schema is `<TASKS_DB_SCHEMA>_board_move`,** for example `public_board_move`. The operator's psql files take it as `-v schema=...`.

**No database URL is ever on a command line.** The operator's psql commands name services from the operator's psql service file (mode 0600), so no URL appears in the process list:
- `service=tasks`: the Tasks database, with its owner URL;
- for the export, a service naming the aweb production database (for example `service=aweb`).

**The destination is never typed in.** Every job that names the installation reads the exported team's live grant with the deployment's grant-read key, as the running app does, and starts the app's runtime to do so.
- That start is the app's own. It runs the outbox drains, so a job can deliver the app's pending events, even while the service is suspended.
- It writes nothing in the installation's dataset. It refuses (exit 3) unless that grant is active and grants exactly `tasks.read`. The listings name the installation id and generation it found.

**A job's log is its receipt.** A listing is stored in the staging schema, and the log shows one line for it: `<name> sha256 <hex> bytes <n> lines <m>`. The operator fetches the stored listing with `fetch_listing.sql`, and the verifier recomputes its sha256 against that line. Exit status 2 is a stop on the export; 3 is a step the move refuses.

## Before step 3

- **The Tasks service runs this commit.** The job's image contains the script.
- **Step 1:** the app is installed on the team, granting only `tasks.read`.
- **Step 2:** every writer is stopped, and that has been shown.

## Step 3: export, load and check

On the operator's host. The export is read-only and runs in one snapshot, against the aweb server's database:

    psql "service=aweb" -X -q -At -v ON_ERROR_STOP=1 -v team_id=<team id> \
      -f naapp/tasks/src/aweb_tasks/board_move/export.sql -o export.json

The query stops, writing nothing, if any task table has columns other than those of aweb 1.27.55.

    psql "service=tasks" -X -q -At -v ON_ERROR_STOP=1 \
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

They store the listings `export` and `staged`. `list-staged` also logs a `dataset` line for the listing's first lines, before its source facts: the rollback baseline. The operator fetches each listing with:

    psql "service=tasks" -X -q -At -v ON_ERROR_STOP=1 -v schema=<staging schema> \
      -v name=<export | staged> -f naapp/tasks/src/aweb_tasks/board_move/fetch_listing.sql \
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

**The export fact names the file the operator exported.** Its sha256 must equal both the load's `loaded export sha256` line and `printf %s "$(cat export.json)" | shasum -a 256` on the operator's file. Every job recomputes the sha256 of the loaded text and refuses if it differs from the recorded one.

Read the board's last write time again with a fresh export, as a cross-check.

**What the comparison proves.** The export-side listing goes through the same mapping the import used. So equal listings prove the staged rows are what the mapping produces from this export. They don't prove the mapping is right. The verifier reads the mapping in `move.py` (`dataset`) against the rulings listed in the pull request.

**The verifier approves the staging listing by its sha256.** Step 6 opens only the staging contents with that digest.

**The verifier also derives the rollback baseline from the approved listing.** It is the `staged` listing's first `<lines>` lines, `<lines>` being the count on the `dataset` log line:

    head -n <lines> staged.jsonl | shasum -a 256

It must match the `dataset` line's sha256 and bytes, and every remaining line must be a source fact.

**If verification fails,** discard. The old board stays authoritative, and a retry starts from a new export and load:

    python -m aweb_tasks.board_move discard

## Step 6: open

    python -m aweb_tasks.board_move open --listing-sha256 <the approved digest>

It reads the team's grant afresh, then in one transaction:
- locks the staging tables, so nothing changes them until the transaction ends;
- reads the grant again once it holds the locks, so an installation change made while it waited refuses it;
- refuses unless the staging lists with the approved digest, was staged for the installation and generation the grant names now, and the installation has no live rows;
- copies the staged rows into the live tables;
- drops the staging schema.

A refusal copies and drops nothing.

**Operator hold: make no installation change from step 4 until open has finished.** The grant can't be read in the same transaction as the copy, so a change after open's last read and before its commit is outside what open can refuse.

Then grant `tasks.write` and `tasks.override`; our agents switch to `aw tasks`.

## Rolling back

**The rollback boundary is the app's first write after opening.** To roll back before it, in this order:

1. **Suspend the Tasks service** with `aweb_naapp.ops prod-suspend`. That target depends on its own task and doesn't exist yet: no ad hoc call stands in for it. Nothing new is admitted, and every admitted request's transaction ends, committed or rolled back.
2. **Set the installation's grant back to `tasks.read` only.**
3. **Stay suspended for at least 60 seconds after step 2.** A process judges scopes against the grant answer it last read, and reuses an answer for at most 60 seconds. That bound is aweb's `get_app_grant_max_age_seconds` (aweb `config.py`): 60 by default, and the server refuses values outside 1..60. The installation fence row holds no scopes.
   - This step does not rely on what Render does on resume.
   - `tests/test_board_move.py::test_a_rolled_back_installation_refuses_every_write_once_resumed` shows that a process reading the read-only grant refuses the write. A process that had already read the write grant kept admitting writes from it.
4. **Compare, while still suspended.**

       python -m aweb_tasks.board_move list-live --installation-id <the installation id in the approved listing>

   Its line, `sha256 <hex> bytes <n> lines <m>`, must equal the `dataset` line from step 5. If it does, no write committed.
   - **It reads no grant and starts no app runtime,** so no drain runs. Its one transaction, run as the owner, runs the writer check and then lists:
     1. `lock.sql` takes the seven dataset tables' EXCLUSIVE locks and holds them. It waits at most 10 s, so a transaction that has written the dataset makes it refuse with `lock_timeout`.
     2. `writer_authority.sql` reads S and A3, the dataset's shape and every principal's write authority, as [the write-authority preflight](#the-write-authority-preflight) describes.
     3. `sessions.sql` reads P and C3:
        - **P:** no prepared transaction in the database.
        - **C3:** every other backend attached to the database, classified by its role id.
          - The owner's refuses while it is inside a transaction, active or idle.
          - A superuser's refuses whenever connected.
          - A platform role's is allowed.
          - Any other role's refuses whenever connected. The role a session acts as after SET ROLE doesn't show, so a session that took the owner's role and kept it after losing the membership is caught only here.
          - A role id that names no role refuses as unattributed.
          - A backend with no role id is allowed. PostgreSQL 17 sets the role id of every client backend, WAL sender and background worker, and of no other backend ([backend_status.c](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/utils/activity/backend_status.c#L292-L305)). Attached to a database, that leaves an autovacuum worker, or a standby's slot-sync worker ([slotsync.c](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/backend/replication/logical/slotsync.c#L10-L16)).
          - The owner can't read another role's `backend_type`, so the check never uses it. What autovacuum runs as each table's owner is covered by the preflight's maintenance rules.
   - **Platform roles** are login roles allowed to stay connected, named with `--platform-roles <name,...>`.
     - The job first echoes them, as `platform roles: <names>` or `platform roles: none`.
     - Each must be an existing login role, neither the owner nor a superuser, so A3 checks it as a principal.
     - **On Render the set is empty.** In four reads of tasks-db's sessions, no platform role kept a session there.
   - **A refusal** prints one `refused: code|principal|role|object` line for each reason, and the job exits with status 3. A refusal changes nothing.
   - **A superuser's session makes it refuse at random.** On Render one comes and goes in tasks-db. Refusing is right, because a superuser can write. Rerun the whole command while the hold of steps 1 to 3 remains in effect.
   - **The installation id is taken from the approved listing, not from the grant.** Every dataset row of that listing carries it, so another installation's listing can never equal the baseline. A wrong id can only fail the comparison, never pass it.
   - **Where it runs:** as a job only once aweb-adzd.8 has observed that Render runs a one-off job on a suspended service. Until then, and before cutover, the operator runs the same check on the Tasks database itself:

         PGOPTIONS='-c search_path=<TASKS_DB_SCHEMA>' psql "service=tasks" -X -At -v ON_ERROR_STOP=1 -c 'BEGIN' \
           -f naapp/tasks/src/aweb_tasks/board_move/lock.sql \
           -f naapp/tasks/src/aweb_tasks/board_move/writer_authority.sql \
           -f naapp/tasks/src/aweb_tasks/board_move/sessions.sql -c 'ROLLBACK'

     - A pass prints `BEGIN`, `SET`, `DO` and `ROLLBACK`, and nothing else.
     - A refusal prints one `code|principal|role|object` line for each reason, before `ROLLBACK`.
     - A lock timeout stops it with an error instead, and exit status 3.
     - To name platform roles, add `-c board_move.platform_roles=<name,...>` to PGOPTIONS.
     - It must be shown to refuse while another session is connected to the database, and to pass without one. The listing itself still needs the job.
5. **Return the agents to the old board,** which is untouched.
6. **Resume** with `aweb_naapp.ops prod-resume`, which also waits on its own task. Then check:
   - `/live` answers;
   - the build SHA is this commit;
   - one probe write (`aw tasks create`) is refused with `scope_denied`.

   The installation stays read-only.

## The write-authority preflight

`writer_authority.sql` proves that no role but the dataset's owner can write the seven dataset tables. It checks these things:
- **S, the dataset's shape.** Each table is an ordinary table owned by the owner, with no inheritance parent or child. No foreign key leads from it to a table outside the seven with an ON DELETE or ON UPDATE action that writes.
- **A3, every principal's write authority.** The principals are every login role that is neither the owner nor a superuser, and every subscription owner in the database. No role a principal reaches through any membership may:
  - be the owner, a superuser or `pg_write_all_data`, or own a dataset table;
  - hold a write privilege on a dataset table, or TRIGGER on one;
  - be able to maintain a relation a writer owns: hold MAINTAIN on it or on a partition ancestor of it, own such an ancestor, or own the database or the schema of the relation or of an ancestor. Maintenance and a materialized view's refresh run code as the relation's owner. A partitioned table's maintenance reaches every partition without checking the partitions, and a database's or schema's owner may reindex every table in it.
- **Nothing a writer owns acts for others.** These are the writers:
  - the owner, each dataset table's owner and the database's owner, whatever their privileges show, because an owner can grant back what it revoked;
  - every role with a write or TRIGGER privilege on a dataset table, every superuser included;
  - and, applied again to each writer found, every member of a writer and every role able to maintain a relation a writer owns, as above.

  No writer may own a view or materialized view, a SECURITY DEFINER function or procedure, or a rule but a view's.
- **Maintenance runs nothing from outside PostgreSQL as a writer.** ANALYZE, which autovacuum runs at any time with no principal, acts as each relation's owner. So no relation a writer owns may have:
  - a column type, column collation, index operator class or index collation outside `pg_catalog`. A type in `pg_catalog` has its element, base and range subtype there too, so checking the column's own type covers them;
  - a table or index access method PostgreSQL doesn't ship;
  - a foreign partition or inheritance descendant, which ANALYZE samples through its wrapper;
  - expression statistics, since PostgreSQL accepts a volatile statistics expression;
  - an expression or partial index with any recorded dependency (`pg_depend`) other than its indexed table, that table's columns, and the parent partitioned index it is attached to. A user function, operator, collation or type refuses, and so does another relation.
    - PostgreSQL records no dependency on its pinned core objects. So an expression of core functions and operators passes, as the app runtime's own partial indexes do.
    - The two allowed dependencies are structural: what the index is on, and what it is attached to.
- **What isn't checked.** Objects in `pg_catalog` and `information_schema` are not checked. The trust assumption is that PostgreSQL's shipped objects there write no dataset row. Every other object is checked, an extension's included.

It is one read-only statement. Each row it returns is a refusal: `code`, `principal`, `role` (the role the principal reaches) and `object`. No row means nothing refuses. It returns only codes and the names of roles and database objects, never a secret.

**The operator runs it on the Tasks database as the owner,** once its exact text and sha256 are approved:

    shasum -a 256 naapp/tasks/src/aweb_tasks/board_move/writer_authority.sql
    PGOPTIONS='-c search_path=<TASKS_DB_SCHEMA>' psql "service=tasks" -X -At -v ON_ERROR_STOP=1 \
      -c 'BEGIN READ ONLY' -f naapp/tasks/src/aweb_tasks/board_move/writer_authority.sql -c 'ROLLBACK'

- psql reads the SQL from the file, so none of it is on the command line.
- A pass prints `BEGIN` and `ROLLBACK` and nothing between them, with exit status 0.
- A refusal prints one `code|principal|role|object` line for each refusal between them.

## What the check can't prove

The check proves that, inside its transaction, nothing but the owner can write the dataset. After the listing, the proof rests on the operational fence:
- the Tasks service is suspended;
- the grant is cut back to `tasks.read` and drained for at least the 60 s cache bound;
- the operator holds every other job on the Tasks service, and any direct write with the owner's URL, until the handoff to the old board.

Within that fence, these are what the check can't prove. **The platform assumption** covers them: Render's platform roles don't write customer rows.
- **`postgres`, Render's superuser,** can write anything, at any time after the check. The locks block it only while the listing runs, and C3 refuses while one of its sessions is connected.
- **A role holding ADMIN on the owner, or CREATEROLE,** could grant itself membership after the check. On tasks-db no role but `postgres` holds either.
- **A role or grant created after the check.** Only the platform can make one, since the owner has no power over other roles.

The platform's roles, as read on tasks-db on 2026-11-28:
- `datadog`: a login whose only membership is `pg_monitor`;
- `primaryuser`: a login with no membership;
- `render_conn`: a NOINHERIT login with no membership;
- `postgres`: the superuser.

None of the first three can become the owner or holds ADMIN on it, and none can write any of the owner's tables. A3 checks each as a principal. None kept a session in tasks-db in four reads, so `--platform-roles` stays empty, and C3 refuses any session of theirs. That includes a session that ran SET ROLE before a membership was revoked.

## What the move carries

- **Numbers and the saved task counter** (`task_counters`) are kept. Each task's old id and ref go into its first history entry, `imported`.
- **Actors are carried exactly as stored,** with nothing derived. No claim and no lock is imported as live: each live one becomes a history entry whose holder is not proven.
- **Not carried:** the server's revision counters (`rev`, `question_rev`), `root_task_seq`, `picked_up` and comment ids. The root task counter is in the listing's source facts.
