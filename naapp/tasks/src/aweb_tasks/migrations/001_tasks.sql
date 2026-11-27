-- One installation's tasks, the claims on them and its locks. Every row names
-- the installation it belongs to; a new installation starts empty.

-- One row per installation: the next task number, never reused. Structural
-- changes (parents, dependencies, closes that walk descendants) lock this row
-- first, so they are serialized per installation.
CREATE TABLE IF NOT EXISTS {{tables.installations}} (
    installation_id TEXT PRIMARY KEY CHECK (BTRIM(installation_id) <> ''),
    next_number     INTEGER NOT NULL CHECK (next_number >= 1)
);

CREATE TABLE IF NOT EXISTS {{tables.tasks}} (
    installation_id TEXT NOT NULL REFERENCES {{tables.installations}} (installation_id),
    number          INTEGER NOT NULL CHECK (number >= 1),
    title           TEXT NOT NULL CHECK (BTRIM(title) <> ''),
    body            TEXT NOT NULL DEFAULT '',
    type            TEXT NOT NULL CHECK (type IN ('task', 'bug', 'feature', 'epic')),
    priority        SMALLINT NOT NULL CHECK (priority BETWEEN 0 AND 4),
    status          TEXT NOT NULL CHECK (status IN ('open', 'blocked', 'closed')),
    labels          TEXT[] NOT NULL DEFAULT '{}',
    parent          INTEGER,
    -- A label naming who should do it, recorded as given; not authority.
    assignee        TEXT,
    creator         JSONB NOT NULL,
    closer          JSONB,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    PRIMARY KEY (installation_id, number),
    FOREIGN KEY (installation_id, parent) REFERENCES {{tables.tasks}} (installation_id, number),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
    CHECK ((status = 'closed') = (closer IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON {{tables.tasks}} (installation_id, parent);

-- One claim row per task, created with it at revision 0. holder_* is null once
-- released; a lease is live while now < expires_at. The revision only grows.
CREATE TABLE IF NOT EXISTS {{tables.claims}} (
    installation_id TEXT NOT NULL,
    number          INTEGER NOT NULL,
    holder_team     TEXT,
    holder_identity TEXT,
    holder_alias    TEXT,
    revision        BIGINT NOT NULL CHECK (revision >= 0),
    expires_at      TIMESTAMPTZ,
    PRIMARY KEY (installation_id, number),
    FOREIGN KEY (installation_id, number) REFERENCES {{tables.tasks}} (installation_id, number),
    CHECK ((holder_identity IS NULL) = (expires_at IS NULL)),
    CHECK ((holder_identity IS NULL) = (holder_team IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_claims_holder ON {{tables.claims}} (installation_id, holder_identity);

-- One lock row per resource ever locked, kept after release so its revision only grows.
CREATE TABLE IF NOT EXISTS {{tables.locks}} (
    installation_id TEXT NOT NULL REFERENCES {{tables.installations}} (installation_id),
    resource        TEXT NOT NULL CHECK (BTRIM(resource) <> ''),
    holder_team     TEXT,
    holder_identity TEXT,
    holder_alias    TEXT,
    reason          TEXT,
    revision        BIGINT NOT NULL CHECK (revision >= 0),
    expires_at      TIMESTAMPTZ,
    PRIMARY KEY (installation_id, resource),
    CHECK ((holder_identity IS NULL) = (expires_at IS NULL)),
    CHECK ((holder_identity IS NULL) = (holder_team IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_locks_holder ON {{tables.locks}} (installation_id, holder_identity);

CREATE TABLE IF NOT EXISTS {{tables.comments}} (
    comment_id      BIGSERIAL PRIMARY KEY,
    installation_id TEXT NOT NULL,
    number          INTEGER NOT NULL,
    author          JSONB NOT NULL,
    body            TEXT NOT NULL CHECK (BTRIM(body) <> ''),
    created_at      TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (installation_id, number) REFERENCES {{tables.tasks}} (installation_id, number)
);

CREATE INDEX IF NOT EXISTS idx_comments_task ON {{tables.comments}} (installation_id, number, comment_id);

-- number waits on waits_on.
CREATE TABLE IF NOT EXISTS {{tables.dependencies}} (
    installation_id TEXT NOT NULL,
    number          INTEGER NOT NULL,
    waits_on        INTEGER NOT NULL,
    PRIMARY KEY (installation_id, number, waits_on),
    FOREIGN KEY (installation_id, number) REFERENCES {{tables.tasks}} (installation_id, number),
    FOREIGN KEY (installation_id, waits_on) REFERENCES {{tables.tasks}} (installation_id, number),
    CHECK (number <> waits_on)
);

-- Every change, with its actor and cause (the tool that made it). A lock's
-- events name its resource; a task's, its number.
CREATE TABLE IF NOT EXISTS {{tables.events}} (
    event_id        BIGSERIAL PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES {{tables.installations}} (installation_id),
    number          INTEGER,
    resource        TEXT,
    kind            TEXT NOT NULL,
    cause           TEXT NOT NULL,
    actor           JSONB NOT NULL,
    detail          JSONB NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    CHECK ((number IS NULL) <> (resource IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_events_task ON {{tables.events}} (installation_id, number, event_id);
