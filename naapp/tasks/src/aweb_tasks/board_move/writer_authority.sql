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
-- pg_auth_members, in any mode, NOINHERIT and NOLOGIN roles included:
-- - is the owner, a superuser or pg_write_all_data, or owns a dataset table;
-- - holds a write privilege on a dataset table, on the table or on any column,
--   or TRIGGER on one: a trigger runs with the rights of whoever next writes;
-- - can maintain a relation a writer owns: holds MAINTAIN on it or on a
--   partition ancestor of it, owns such an ancestor, or owns the database or
--   the schema of the relation or of an ancestor. Maintenance, and a
--   materialized view's refresh, run code as the relation's owner; a
--   partitioned table's maintenance reaches every partition with no check on
--   the partitions; and a database's or schema's owner may reindex every
--   table in it.
--
-- The writers are the owner, each dataset table's owner and the database's
-- owner, whatever their privileges show, since an owner can grant itself back
-- what it revoked; every role holding a write or TRIGGER privilege on a
-- dataset table, every superuser among them; and, repeatedly, every member of
-- a writer, in any mode, and every role that can maintain a relation a writer
-- owns, as above. No writer may own a view or materialized
-- view, a SECURITY DEFINER function or procedure, or a rule but a view's
-- _RETURN: each lets whoever can use it act with its owner's rights.
--
-- Nor may a writer own a relation whose maintenance runs anything but
-- PostgreSQL's own code. ANALYZE, which autovacuum runs at any time with no
-- principal, acts as the relation's owner: it evaluates the index expressions
-- and predicates and the statistics expressions, uses the columns' types and
-- collations and the indexes' operator classes and collations, calls the
-- access methods, and samples any foreign table below the relation through its
-- wrapper. So a writer's relation may have no column whose type, or whose
-- type's element, base, range subtype or multirange's range, at any depth, is
-- outside pg_catalog; no collation or operator class outside pg_catalog; no
-- table or index access method PostgreSQL does not ship; and no foreign
-- partition or inheritance descendant. Nor may it have expression
-- statistics: PostgreSQL accepts a volatile statistics expression. An
-- expression or partial index may depend, as pg_depend records it, only on
-- the indexed table or its columns and on the parent partitioned index it is
-- attached to. Any other recorded dependency
-- refuses: a function, an operator, a collation, a type or another relation.
-- PostgreSQL records no dependency on its pinned core objects, so an
-- expression of core functions and operators passes.
--
-- Objects in pg_catalog and information_schema are not checked: the trust
-- assumption is that PostgreSQL's shipped objects there write no dataset row.
-- Every other object, an extension's included, is.

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
checked AS (
    SELECT oid FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', 'information_schema')
),
writers (role) AS (
    SELECT oid FROM owner
    UNION
    SELECT relowner FROM tables WHERE oid IS NOT NULL
    UNION
    SELECT datdba FROM pg_database WHERE datname = current_database()
    UNION
    -- Every superuser holds every privilege. MAINTAIN on a dataset table is
    -- found below, on the relations the table owners own.
    SELECT pg_roles.oid FROM pg_roles CROSS JOIN tables
    WHERE tables.oid IS NOT NULL
      AND (has_table_privilege(pg_roles.oid, tables.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER')
           OR has_any_column_privilege(pg_roles.oid, tables.oid, 'INSERT, UPDATE'))
    UNION
    SELECT next.role FROM writers CROSS JOIN LATERAL (
        SELECT member FROM pg_auth_members WHERE roleid = writers.role
        UNION ALL
        SELECT authority.role
        FROM pg_class relation
        CROSS JOIN LATERAL (
            SELECT relation.oid UNION SELECT relid FROM pg_partition_ancestors(relation.oid)
        ) ancestor (oid)
        JOIN pg_class above ON above.oid = ancestor.oid
        CROSS JOIN LATERAL (
            SELECT above.relowner
            UNION ALL
            SELECT nspowner FROM pg_namespace
            WHERE oid = above.relnamespace AND oid IN (SELECT oid FROM checked)
            UNION ALL
            SELECT oid FROM pg_roles WHERE has_table_privilege(oid, above.oid, 'MAINTAIN')
        ) authority (role)
        WHERE relation.relowner = writers.role AND relation.relkind IN ('r', 'p', 'm')
          AND relation.relnamespace IN (SELECT oid FROM checked)
    ) next (role)
),
-- Every relation whose maintenance runs a writer's code: the relations writers
-- own. A partition ancestor of one has a writer for its owner too.
maintained (oid, relnamespace, relowner) AS (
    SELECT oid, relnamespace, relowner FROM pg_class
    WHERE relkind IN ('r', 'p', 'm')
      AND relnamespace IN (SELECT oid FROM checked)
      AND relowner IN (SELECT role FROM writers)
),
-- Every type a maintained relation's column reaches: the column's own type,
-- and through each type its element, its base, its range's subtype and a
-- multirange's range.
column_types (relation, relowner, attname, type) AS (
    SELECT maintained.oid, maintained.relowner, attribute.attname, attribute.atttypid
    FROM maintained
    JOIN pg_attribute attribute
        ON attribute.attrelid = maintained.oid AND attribute.attnum > 0 AND NOT attribute.attisdropped
    UNION
    SELECT column_types.relation, column_types.relowner, column_types.attname, reached.type
    FROM column_types
    JOIN pg_type ON pg_type.oid = column_types.type
    CROSS JOIN LATERAL (
        SELECT pg_type.typelem WHERE pg_type.typelem <> 0
        UNION ALL
        SELECT pg_type.typbasetype WHERE pg_type.typbasetype <> 0
        UNION ALL
        SELECT rngsubtype FROM pg_range WHERE rngtypid = pg_type.oid
        UNION ALL
        SELECT rngtypid FROM pg_range WHERE rngmultitypid = pg_type.oid
    ) reached (type)
),
-- Every relation below a maintained one, through partitions and inheritance.
descendants (root, oid) AS (
    SELECT oid, oid FROM maintained
    UNION
    SELECT descendants.root, pg_inherits.inhrelid
    FROM descendants JOIN pg_inherits ON pg_inherits.inhparent = descendants.oid
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
    UNION ALL
    SELECT 'write_authority_trigger_privilege', principal, role, tables.name
    FROM reach CROSS JOIN tables
    WHERE tables.oid IS NOT NULL AND has_table_privilege(reach.role, tables.oid, 'TRIGGER')
    UNION ALL
    SELECT 'write_authority_maintain_privilege', principal, role, maintained.oid::regclass::text
    FROM reach CROSS JOIN maintained
    WHERE has_table_privilege(reach.role, maintained.oid, 'MAINTAIN')
    UNION ALL
    -- An owner can grant itself back whatever privilege it revoked.
    SELECT 'write_authority_maintenance_owner', principal, role, maintained.oid::regclass::text
    FROM reach JOIN maintained ON maintained.relowner = reach.role
    UNION ALL
    SELECT 'write_authority_maintenance_scope', principal, role, namespace.nspname::text
    FROM reach JOIN pg_namespace namespace ON namespace.nspowner = reach.role
    WHERE namespace.oid IN (SELECT oid FROM checked)
      AND namespace.oid IN (SELECT relnamespace FROM maintained)
    UNION ALL
    SELECT 'write_authority_maintenance_scope', principal, role, datname::text
    FROM reach JOIN pg_database ON pg_database.datdba = reach.role
    WHERE datname = current_database()
    -- A3, through what a writer owns
    UNION ALL
    SELECT 'indirection_view', NULL, relowner, oid::regclass::text FROM pg_class
    WHERE relkind IN ('v', 'm')
      AND relnamespace IN (SELECT oid FROM checked) AND relowner IN (SELECT role FROM writers)
    UNION ALL
    SELECT 'indirection_security_definer', NULL, proowner, oid::regprocedure::text FROM pg_proc
    WHERE prosecdef
      AND pronamespace IN (SELECT oid FROM checked) AND proowner IN (SELECT role FROM writers)
    UNION ALL
    SELECT 'indirection_rule', NULL, relation.relowner, rule.rulename
    FROM pg_rewrite rule JOIN pg_class relation ON relation.oid = rule.ev_class
    WHERE relation.relnamespace IN (SELECT oid FROM checked)
      AND relation.relowner IN (SELECT role FROM writers)
      AND NOT (relation.relkind IN ('v', 'm') AND rule.rulename = '_RETURN')
    -- A3, through what maintenance runs as a writer
    UNION ALL
    SELECT 'indirection_index_expression', NULL, maintained.relowner, idx.indexrelid::regclass::text
    FROM maintained JOIN pg_index idx ON idx.indrelid = maintained.oid
    WHERE (idx.indexprs IS NOT NULL OR idx.indpred IS NOT NULL)
      AND EXISTS (
          SELECT 1 FROM pg_depend dependency
          WHERE dependency.classid = 'pg_class'::regclass AND dependency.objid = idx.indexrelid
            -- the indexed table, or one of its columns
            AND NOT (dependency.refclassid = 'pg_class'::regclass AND dependency.refobjid = idx.indrelid)
            -- the parent partitioned index it is attached to
            AND NOT (dependency.refclassid = 'pg_class'::regclass
                     AND dependency.refobjid IN (SELECT inhparent FROM pg_inherits WHERE inhrelid = idx.indexrelid))
      )
    UNION ALL
    SELECT 'indirection_statistics_expression', NULL, maintained.relowner,
           format('%I.%I', namespace.nspname, statistics.stxname)
    FROM maintained
    JOIN pg_statistic_ext statistics ON statistics.stxrelid = maintained.oid
    JOIN pg_namespace namespace ON namespace.oid = statistics.stxnamespace
    WHERE statistics.stxexprs IS NOT NULL
    UNION ALL
    -- One row per column, however many of its types are outside pg_catalog.
    SELECT DISTINCT 'indirection_column_type', NULL::oid, column_types.relowner,
           format('%s.%I', column_types.relation::regclass, column_types.attname)
    FROM column_types
    JOIN pg_type column_type ON column_type.oid = column_types.type
    WHERE column_type.typnamespace <> 'pg_catalog'::regnamespace
    UNION ALL
    SELECT 'indirection_collation', NULL, maintained.relowner,
           format('%s.%I', maintained.oid::regclass, attribute.attname)
    FROM maintained
    JOIN pg_attribute attribute
        ON attribute.attrelid = maintained.oid AND attribute.attnum > 0 AND NOT attribute.attisdropped
    JOIN pg_collation named ON named.oid = attribute.attcollation
    WHERE named.collnamespace <> 'pg_catalog'::regnamespace
    UNION ALL
    SELECT 'indirection_collation', NULL, maintained.relowner, idx.indexrelid::regclass::text
    FROM maintained JOIN pg_index idx ON idx.indrelid = maintained.oid
    WHERE EXISTS (
        SELECT 1 FROM unnest(idx.indcollation) used (oid)
        JOIN pg_collation named ON named.oid = used.oid
        WHERE named.collnamespace <> 'pg_catalog'::regnamespace
    )
    UNION ALL
    SELECT 'indirection_operator_class', NULL, maintained.relowner, idx.indexrelid::regclass::text
    FROM maintained JOIN pg_index idx ON idx.indrelid = maintained.oid
    WHERE EXISTS (
        SELECT 1 FROM unnest(idx.indclass) used (oid)
        JOIN pg_opclass opclass ON opclass.oid = used.oid
        WHERE opclass.opcnamespace <> 'pg_catalog'::regnamespace
    )
    UNION ALL
    SELECT 'indirection_access_method', NULL, maintained.relowner, relation.oid::regclass::text
    FROM maintained
    CROSS JOIN LATERAL (
        SELECT maintained.oid
        UNION ALL
        SELECT indexrelid FROM pg_index WHERE indrelid = maintained.oid
    ) used (oid)
    JOIN pg_class relation ON relation.oid = used.oid
    JOIN pg_am method ON method.oid = relation.relam
    WHERE method.amname NOT IN ('heap', 'btree', 'hash', 'gist', 'gin', 'spgist', 'brin')
    UNION ALL
    SELECT 'indirection_foreign_descendant', NULL, maintained.relowner, relation.oid::regclass::text
    FROM maintained
    JOIN descendants ON descendants.root = maintained.oid
    JOIN pg_class relation ON relation.oid = descendants.oid
    WHERE relation.relkind = 'f'
)
SELECT refusals.code, principal.rolname AS principal, reached.rolname AS role, refusals.object
FROM refusals
LEFT JOIN pg_roles principal ON principal.oid = refusals.principal
LEFT JOIN pg_roles reached ON reached.oid = refusals.role
ORDER BY refusals.code, principal.rolname, reached.rolname, refusals.object;
