-- The board move's writer check, first step: the seven dataset tables'
-- EXCLUSIVE locks, taken in the listing's transaction before anything is read,
-- and held until it ends. A transaction that has written the dataset holds a
-- lock they wait for: after 10 s the step fails with lock_not_available
-- (55P03), which the check reports as lock_timeout.
--
-- It locks the dataset tables the first schema of the search_path holds as
-- tables. writer_authority.sql refuses any that is missing or is not an
-- ordinary table.

SET LOCAL lock_timeout = '10s';

DO $lock$
DECLARE
    dataset text[] := ARRAY['installations', 'tasks', 'claims', 'locks', 'comments', 'dependencies', 'events'];
    present text;
BEGIN
    SELECT string_agg(format('%I.%I', current_schema(), relname), ', ' ORDER BY array_position(dataset, relname::text))
    INTO present
    FROM pg_class
    WHERE relkind IN ('r', 'p') AND relname = ANY (dataset)
      AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = current_schema());
    IF present IS NOT NULL THEN
        EXECUTE format('LOCK TABLE %s IN EXCLUSIVE MODE', present);
    END IF;
END
$lock$;
