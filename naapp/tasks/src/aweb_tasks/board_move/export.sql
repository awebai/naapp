-- The board move's export (spec section 8, step 3): one team's frozen board on
-- the aweb server, as one JSON document. Read-only, in one snapshot.
--
--   psql -X -q -At -v ON_ERROR_STOP=1 -v team_id=<team id> -f export.sql -o export.json
--
-- It reads the aweb schema as aweb 1.27.55 lays it out and stops, writing
-- nothing, if any table it reads has other columns.

BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL TIME ZONE 'UTC';

DO $check$
DECLARE
    expected CONSTANT jsonb := '{
        "tasks": ["task_id", "team_id", "task_number", "root_task_seq", "task_ref_suffix",
                  "title", "description", "notes", "status", "priority", "task_type",
                  "assignee_alias", "created_by_alias", "closed_by_alias", "labels",
                  "parent_task_id", "created_at", "updated_at", "closed_at", "deleted_at",
                  "rev", "question_rev", "assignee_did", "created_by_did", "closed_by_did",
                  "app_request", "cancellation", "picked_up"],
        "task_comments": ["comment_id", "task_id", "team_id", "author_alias", "body", "created_at"],
        "task_dependencies": ["task_id", "depends_on_id", "team_id"],
        "task_counters": ["team_id", "next_number"],
        "task_root_counters": ["team_id", "next_number"],
        "task_claims": ["id", "team_id", "workspace_id", "alias", "human_name", "task_ref",
                        "apex_task_ref", "claimed_at"],
        "reservations": ["team_id", "resource_key", "holder_alias", "holder_agent_id",
                         "acquired_at", "expires_at", "metadata_json"],
        "task_app_requests": ["task_id", "team_id", "app_id", "installation_id",
                              "installation_generation", "originator_did", "external_request_id",
                              "task_body_sha256", "task_authorization", "receipt"],
        "task_answers": ["receipt_id", "task_id", "question_rev", "task_rev", "answer_key",
                         "answer_sha256", "answered_by_did", "certificate_id", "answered_at",
                         "choice", "proposal_rev", "proposal_sha256", "stage_receipt"],
        "task_answer_stage_nonces": ["team_id", "app_id", "installation_id", "nonce", "receipt_id"]
    }';
    relation text;
    found text[];
    wanted text[];
BEGIN
    FOR relation IN SELECT jsonb_object_keys(expected) LOOP
        SELECT array_agg(column_name::text ORDER BY column_name::text) INTO found
        FROM information_schema.columns
        WHERE table_schema = 'aweb' AND table_name = relation;
        SELECT array_agg(name) INTO wanted FROM jsonb_array_elements_text(expected -> relation) AS name;
        -- The same set of names, compared without an order: a database's
        -- collation orders information_schema's names and these differently.
        IF found IS NULL OR NOT (found @> wanted AND wanted @> found) THEN
            RAISE EXCEPTION 'board export: aweb.% has the columns %, not those of aweb 1.27.55', relation, found;
        END IF;
    END LOOP;
END
$check$;

WITH team_tasks AS (
    SELECT * FROM aweb.tasks WHERE team_id = :'team_id'
)
SELECT json_build_object(
    'format', 'aweb-board-export/1',
    'team_id', :'team_id'::text,
    'exported_at', now(),
    'last_write_at', GREATEST(
        (SELECT max(GREATEST(created_at, updated_at, closed_at, deleted_at)) FROM team_tasks),
        (SELECT max(created_at) FROM aweb.task_comments WHERE team_id = :'team_id'),
        (SELECT max(claimed_at) FROM aweb.task_claims WHERE team_id = :'team_id'),
        (SELECT max(acquired_at) FROM aweb.reservations WHERE team_id = :'team_id')
    ),
    'task_counter', (SELECT next_number FROM aweb.task_counters WHERE team_id = :'team_id'),
    'root_task_counter', (SELECT next_number FROM aweb.task_root_counters WHERE team_id = :'team_id'),
    'tasks', COALESCE((
        SELECT json_agg(json_build_object(
            'task_id', task_id, 'task_number', task_number, 'root_task_seq', root_task_seq,
            'task_ref_suffix', task_ref_suffix, 'title', title, 'description', description,
            'notes', notes, 'status', status, 'priority', priority, 'task_type', task_type,
            'assignee_alias', assignee_alias, 'assignee_did', assignee_did,
            'created_by_alias', created_by_alias, 'created_by_did', created_by_did,
            'closed_by_alias', closed_by_alias, 'closed_by_did', closed_by_did,
            'labels', labels, 'parent_task_id', parent_task_id, 'created_at', created_at,
            'updated_at', updated_at, 'closed_at', closed_at, 'deleted_at', deleted_at,
            'rev', rev, 'question_rev', question_rev, 'app_request', app_request,
            'cancellation', cancellation, 'picked_up', picked_up
        ) ORDER BY task_number) FROM team_tasks
    ), '[]'),
    'comments', COALESCE((
        SELECT json_agg(json_build_object(
            'comment_id', comment_id, 'task_id', task_id, 'author_alias', author_alias,
            'body', body, 'created_at', created_at
        ) ORDER BY created_at, comment_id) FROM aweb.task_comments WHERE team_id = :'team_id'
    ), '[]'),
    'dependencies', COALESCE((
        SELECT json_agg(json_build_object('task_id', task_id, 'depends_on_id', depends_on_id)
                        ORDER BY task_id, depends_on_id)
        FROM aweb.task_dependencies WHERE team_id = :'team_id'
    ), '[]'),
    'claims', COALESCE((
        SELECT json_agg(json_build_object(
            'id', id, 'workspace_id', workspace_id, 'alias', alias, 'human_name', human_name,
            'task_ref', task_ref, 'apex_task_ref', apex_task_ref, 'claimed_at', claimed_at
        ) ORDER BY task_ref, claimed_at, id) FROM aweb.task_claims WHERE team_id = :'team_id'
    ), '[]'),
    -- Live as the server reads a reservation: its expiry is after now.
    'reservations', COALESCE((
        SELECT json_agg(json_build_object(
            'resource_key', resource_key, 'holder_alias', holder_alias,
            'holder_agent_id', holder_agent_id, 'acquired_at', acquired_at,
            'expires_at', expires_at, 'metadata_json', metadata_json,
            'live', COALESCE(expires_at > now(), false)
        ) ORDER BY resource_key) FROM aweb.reservations WHERE team_id = :'team_id'
    ), '[]'),
    'task_app_requests', COALESCE((
        SELECT json_agg(to_jsonb(r) - 'team_id' ORDER BY r.task_id)
        FROM aweb.task_app_requests r WHERE r.team_id = :'team_id'
    ), '[]'),
    'task_answers', COALESCE((
        SELECT json_agg(to_jsonb(a) ORDER BY a.receipt_id)
        FROM aweb.task_answers a JOIN team_tasks t USING (task_id)
    ), '[]'),
    'task_answer_stage_nonces', COALESCE((
        SELECT json_agg(to_jsonb(n) - 'team_id' ORDER BY n.receipt_id)
        FROM aweb.task_answer_stage_nonces n WHERE n.team_id = :'team_id'
    ), '[]')
);

COMMIT;
