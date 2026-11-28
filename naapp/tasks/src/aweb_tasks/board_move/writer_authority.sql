-- The board move's proof that no role but the dataset's owner can write the
-- dataset: its shape (S) and the write authority of every principal (A3).
-- One read-only statement, run as the owner role. The search_path names the
-- Tasks live schema: the job's connection pins it, and the operator sets it
-- with PGOPTIONS='-c search_path=<TASKS_DB_SCHEMA>'.
--
-- Each row is one refusal: a code, and the principal, the role it reaches and
-- the object involved, where the code has them. No row: nothing refuses. It
-- reads only the catalogues, and returns no secret.
--
-- S. The seven dataset tables, in the first schema of the search_path, must be
-- ordinary tables owned by the owner, with no inheritance parent or child, and
-- with no foreign key to a table outside them whose ON DELETE or ON UPDATE
-- action writes: a referential action writes as the owner of the table it
-- changes, on nobody's grant.
--
-- A3. The principals are every login role that is neither the owner nor a
-- superuser, and the owner of every subscription in this database, as which
-- its apply worker runs. A principal refuses if any role it reaches through
-- pg_auth_members, in any mode, NOINHERIT and NOLOGIN roles included, is the
-- owner, a superuser or pg_write_all_data, owns a dataset table, or holds a
-- write privilege on one, on the table or on any column. And the owner may own
-- no view, no SECURITY DEFINER function or procedure, and no rule but a view's
-- _RETURN, on any relation in this database: each acts with the owner's rights.

WITH RECURSIVE
dataset (name) AS (
    VALUES ('installations'), ('tasks'), ('claims'), ('locks'), ('comments'), ('dependencies'), ('events')
),
owner AS (
    SELECT oid FROM pg_roles WHERE rolname = current_user
),
tables AS (
    SELECT dataset.name, class.oid, class.relkind, class.relowner
    FROM dataset
    LEFT JOIN pg_class class
        ON class.relname = dataset.name
       AND class.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = current_schema())
),
principals (oid) AS (
    SELECT oid FROM pg_roles
    WHERE rolcanlogin AND NOT rolsuper AND oid <> (SELECT oid FROM owner)
    UNION
    SELECT subowner FROM pg_subscription
    WHERE subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
),
reach (principal, role) AS (
    SELECT oid, oid FROM principals
    UNION
    SELECT reach.principal, membership.roleid
    FROM reach JOIN pg_auth_members membership ON membership.member = reach.role
),
refusals (code, principal, role, object) AS (
    -- S
    SELECT 'shape_missing_table', NULL::oid, NULL::oid, name FROM tables WHERE oid IS NULL
    UNION ALL
    SELECT 'shape_not_ordinary_table', NULL, NULL, name FROM tables WHERE relkind <> 'r'
    UNION ALL
    SELECT 'shape_inheritance', NULL, NULL, name FROM tables
    WHERE EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = tables.oid OR inhparent = tables.oid)
    UNION ALL
    SELECT 'shape_outside_referential_action', NULL, NULL, tables.name
    FROM tables JOIN pg_constraint fk ON fk.conrelid = tables.oid AND fk.contype = 'f'
    WHERE fk.confrelid NOT IN (SELECT oid FROM tables WHERE oid IS NOT NULL)
      AND (fk.confdeltype NOT IN ('a', 'r') OR fk.confupdtype NOT IN ('a', 'r'))
    UNION ALL
    SELECT 'shape_not_owned', NULL, NULL, name FROM tables WHERE relowner <> (SELECT oid FROM owner)
    -- A3, per principal
    UNION ALL
    SELECT 'write_authority_owner', principal, role, NULL FROM reach
    WHERE role = (SELECT oid FROM owner)
    UNION ALL
    SELECT 'write_authority_superuser', principal, role, NULL
    FROM reach JOIN pg_roles ON pg_roles.oid = reach.role WHERE pg_roles.rolsuper
    UNION ALL
    SELECT 'write_authority_write_all_data', principal, role, NULL FROM reach
    WHERE role = 'pg_write_all_data'::regrole
    UNION ALL
    SELECT 'write_authority_table_owner', principal, role, tables.name
    FROM reach JOIN tables ON tables.relowner = reach.role
    UNION ALL
    SELECT 'write_authority_table_privilege', principal, role, tables.name
    FROM reach CROSS JOIN tables
    WHERE tables.oid IS NOT NULL
      AND has_table_privilege(reach.role, tables.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
    UNION ALL
    SELECT 'write_authority_column_privilege', principal, role, tables.name
    FROM reach CROSS JOIN tables
    WHERE tables.oid IS NOT NULL
      AND has_any_column_privilege(reach.role, tables.oid, 'INSERT, UPDATE')
      AND NOT has_table_privilege(reach.role, tables.oid, 'INSERT, UPDATE')
    -- A3, through what the owner owns
    UNION ALL
    SELECT 'indirection_view', NULL, NULL, oid::regclass::text FROM pg_class
    WHERE relkind = 'v' AND relowner = (SELECT oid FROM owner)
    UNION ALL
    SELECT 'indirection_security_definer', NULL, NULL, oid::regprocedure::text FROM pg_proc
    WHERE prosecdef AND proowner = (SELECT oid FROM owner)
    UNION ALL
    SELECT 'indirection_rule', NULL, NULL, rule.rulename
    FROM pg_rewrite rule JOIN pg_class relation ON relation.oid = rule.ev_class
    WHERE relation.relowner = (SELECT oid FROM owner)
      AND NOT (relation.relkind = 'v' AND rule.rulename = '_RETURN')
)
SELECT refusals.code, principal.rolname AS principal, reached.rolname AS role, refusals.object
FROM refusals
LEFT JOIN pg_roles principal ON principal.oid = refusals.principal
LEFT JOIN pg_roles reached ON reached.oid = refusals.role
ORDER BY refusals.code, principal.rolname, reached.rolname, refusals.object;
