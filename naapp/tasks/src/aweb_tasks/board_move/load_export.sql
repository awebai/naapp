-- The board move's export, loaded by the operator into the staging schema of
-- the Tasks database (spec section 8, step 3), so that the move's jobs read it
-- without the aweb server's database:
--
--   psql "service=tasks" -X -q -At -v ON_ERROR_STOP=1 \
--     -v schema=<TASKS_DB_SCHEMA>_board_move -v file=export.json -f load_export.sql
--
-- psql reads the file itself, so the board never passes through a command
-- line. Its value is the file's text without the final newline; the stored
-- sha256 names that text. It refuses if the staging schema exists.

\set export `cat :'file'`

BEGIN;
CREATE SCHEMA :"schema";
CREATE TABLE :"schema".board_move_export (
    sha256   TEXT NOT NULL,
    document TEXT NOT NULL
);
INSERT INTO :"schema".board_move_export (sha256, document)
SELECT encode(sha256(convert_to(:'export', 'UTF8')), 'hex'), :'export';
COMMIT;

SELECT 'loaded export sha256 ' || sha256 FROM :"schema".board_move_export;
