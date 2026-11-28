-- The board move's session checks, P and C3, read as the dataset's owner in the
-- listing's transaction, after the dataset's EXCLUSIVE locks and after
-- writer_authority.sql. The setting board_move.platform_roles names the platform
-- roles, comma-separated: login roles the operator allows to stay connected.
-- Unset or empty, it names none, which is the default unless the platform's
-- evidence names them.
--
-- Each row is one refusal: a code, and the principal, the role and the object
-- involved, where the code has them. No row: nothing refuses.
--
-- Platform roles. Each must be an existing login role, neither the owner nor a
-- superuser. A login role is one of writer_authority.sql's principals, so A3
-- has just proved that it reaches no write authority.
--
-- P. No prepared transaction in this database. One that has written the dataset
-- holds its locks, and the lock step times out on it; any other still runs as
-- a transaction no session shows.
--
-- C3. Every other backend attached to this database, classified by its raw
-- role id, never by backend_type, which the owner cannot read for another
-- role's backend:
-- - the owner's: refuses while it is inside a transaction, active or idle;
-- - a superuser's: refuses whenever connected;
-- - a platform role's: allowed;
-- - any other role's: refuses whenever connected, since the role it acts as
--   after SET ROLE does not show;
-- - an id that names no role: refuses as unattributed;
-- - no role id: allowed. PostgreSQL 17 gives a backend no role id only if it
--   is neither a client backend, a WAL sender nor a background worker
--   (backend_status.c, pgstat_bestart). Attached to a database, that is an
--   autovacuum worker, or a standby's slot-sync worker. Autovacuum runs code as
--   each table's owner, which writer_authority.sql's maintenance shape covers.

WITH
owner AS (
    SELECT oid FROM pg_roles WHERE rolname = current_user
),
platform (name, oid, rolsuper, rolcanlogin) AS (
    SELECT named.name, pg_roles.oid, pg_roles.rolsuper, pg_roles.rolcanlogin
    FROM unnest(string_to_array(nullif(current_setting('board_move.platform_roles', true), ''), ',')) named (name)
    LEFT JOIN pg_roles ON pg_roles.rolname = named.name
),
sessions AS (
    SELECT activity.pid, activity.usesysid, activity.xact_start, pg_roles.oid AS role, pg_roles.rolsuper
    FROM pg_stat_activity activity
    LEFT JOIN pg_roles ON pg_roles.oid = activity.usesysid
    WHERE activity.datid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND activity.pid <> pg_backend_pid()
),
refusals (code, principal, role, object) AS (
    SELECT 'platform_role_unknown', NULL::oid, NULL::oid, name FROM platform WHERE oid IS NULL
    UNION ALL
    SELECT 'platform_role_owner', NULL, oid, NULL FROM platform WHERE oid = (SELECT oid FROM owner)
    UNION ALL
    SELECT 'platform_role_superuser', NULL, oid, NULL FROM platform WHERE rolsuper
    UNION ALL
    SELECT 'platform_role_not_login', NULL, oid, NULL FROM platform WHERE NOT rolcanlogin
    -- P
    UNION ALL
    SELECT 'prepared_transaction', NULL, pg_roles.oid, prepared.gid
    FROM pg_prepared_xacts prepared LEFT JOIN pg_roles ON pg_roles.rolname = prepared.owner
    WHERE prepared.database = current_database()
    -- C3
    UNION ALL
    SELECT 'session_owner_in_transaction', NULL, role, pid::text FROM sessions
    WHERE role = (SELECT oid FROM owner) AND xact_start IS NOT NULL
    UNION ALL
    SELECT 'session_superuser', NULL, role, pid::text FROM sessions WHERE rolsuper
    UNION ALL
    SELECT 'session_connected', NULL, role, pid::text FROM sessions
    WHERE NOT rolsuper AND role <> (SELECT oid FROM owner)
      AND role NOT IN (SELECT oid FROM platform WHERE oid IS NOT NULL)
    UNION ALL
    SELECT 'session_unattributed', NULL, NULL, pid::text FROM sessions
    WHERE usesysid IS NOT NULL AND role IS NULL
)
SELECT refusals.code, principal.rolname AS principal, reached.rolname AS role, refusals.object
FROM refusals
LEFT JOIN pg_roles principal ON principal.oid = refusals.principal
LEFT JOIN pg_roles reached ON reached.oid = refusals.role
ORDER BY refusals.code, principal.rolname, reached.rolname, refusals.object;
