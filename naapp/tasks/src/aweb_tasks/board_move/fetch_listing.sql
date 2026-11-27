-- One listing a board move job stored in the staging schema, as base64 of its
-- exact bytes, for the operator to hand to the verifier:
--
--   psql "service=tasks" -X -q -At -v ON_ERROR_STOP=1 \
--     -v schema=<TASKS_DB_SCHEMA>_board_move -v name=staged -f fetch_listing.sql \
--     | base64 --decode > staged.jsonl
--
-- It prints nothing if no such listing is stored.

SELECT encode(listing, 'base64') FROM :"schema".board_move_listings WHERE name = :'name';
