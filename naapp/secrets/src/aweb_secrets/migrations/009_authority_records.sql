-- Evidence for explicitly published authority. There is no backfill from the
-- database: standing rows must be independently approved before activation.
CREATE TABLE {{tables.authority_records}} (
    installation_id TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    identity        TEXT[] NOT NULL,
    revision        BIGINT NOT NULL CHECK (revision > 0),
    mac             BYTEA NOT NULL CHECK (octet_length(mac) = 32),
    PRIMARY KEY (installation_id, team_id, kind, identity)
);
