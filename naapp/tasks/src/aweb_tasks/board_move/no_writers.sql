-- The board move's proof that no writer is left (rollback, step 4), run at the
-- start of the transaction that then lists the live dataset. The search_path
-- names the Tasks live schema: the job's connection pins it, and the operator
-- sets it with PGOPTIONS='-c search_path=<TASKS_DB_SCHEMA>'.
--
-- It refuses unless this session can see every other session's transaction;
-- then waits, at most 10 s, for every transaction that has touched the dataset
-- tables and holds them until the caller's transaction ends; then refuses if
-- any other client session of this database is inside a transaction, active
-- or idle, which covers a writer that has not reached the tables yet.

SET LOCAL lock_timeout = '10s';

DO $visible$
BEGIN
    IF NOT (
        pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER')
        OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
    ) THEN
        RAISE EXCEPTION 'board move: role % cannot see other sessions'' transactions (it needs pg_read_all_stats)', current_user;
    END IF;
END
$visible$;

LOCK TABLE installations, tasks, claims, locks, comments, dependencies, events IN EXCLUSIVE MODE;

DO $alone$
DECLARE
    others text;
BEGIN
    SELECT string_agg(format('pid %s (%s, %s)', pid, usename, state), ', ' ORDER BY pid) INTO others
    FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()
      AND backend_type = 'client backend' AND xact_start IS NOT NULL;
    IF others IS NOT NULL THEN
        RAISE EXCEPTION 'board move: other sessions are inside a transaction: %', others;
    END IF;
END
$alone$;
