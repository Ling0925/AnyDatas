PRAGMA foreign_keys = ON;

-- Immutable source bytes make recurring desktop refreshes crash-safe and auditable.
CREATE TABLE source_revisions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    content_sha256 TEXT,
    size_bytes INTEGER NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    file_kind TEXT NOT NULL CHECK (file_kind IN ('excel', 'csv')),
    retained_until TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        content_sha256 IS NULL
        OR (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*')
    )
);

CREATE INDEX idx_source_revisions_source_hash
    ON source_revisions(source_id, content_sha256)
    WHERE content_sha256 IS NOT NULL;
CREATE INDEX idx_source_revisions_retention
    ON source_revisions(retained_until)
    WHERE retained_until IS NOT NULL;

ALTER TABLE data_sources
    ADD COLUMN current_revision_id TEXT REFERENCES source_revisions(id) ON DELETE SET NULL;

-- Existing files become immutable legacy revisions. Their hashes are filled after startup because
-- SQL migrations cannot read file bytes.
INSERT INTO source_revisions (
    id, source_id, content_sha256, size_bytes, stored_path, original_filename,
    media_type, file_kind, retained_until, created_at
)
SELECT
    'legacy-' || id, id, NULL, size_bytes, stored_path, original_filename,
    media_type, file_kind, NULL, created_at
FROM data_sources;

UPDATE data_sources
SET current_revision_id = 'legacy-' || id;

ALTER TABLE staged_imports ADD COLUMN content_sha256 TEXT;

-- One receipt is the server-side authority for a refresh retry and its single saved-query job.
CREATE TABLE source_refresh_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    saved_query_id TEXT,
    saved_query_updated_at TEXT,
    revision_id TEXT NOT NULL,
    job_id TEXT,
    unchanged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, idempotency_key),
    CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE INDEX idx_source_refresh_runs_source_created
    ON source_refresh_runs(source_id, created_at DESC);

-- Jobs created after this migration execute exactly the source revision and table configuration
-- captured at enqueue time. Existing job_tables remain for API compatibility and display.
CREATE TABLE job_input_tables (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    source_table_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision_id TEXT REFERENCES source_revisions(id) ON DELETE SET NULL,
    content_sha256 TEXT,
    stored_path TEXT NOT NULL,
    file_kind TEXT NOT NULL CHECK (file_kind IN ('excel', 'csv')),
    sheet_name TEXT NOT NULL,
    start_cell TEXT NOT NULL,
    end_cell TEXT,
    first_row_as_header INTEGER NOT NULL,
    schema_json TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    cache_key TEXT,
    alias TEXT NOT NULL,
    PRIMARY KEY (job_id, ordinal),
    UNIQUE (job_id, alias)
);

CREATE INDEX idx_job_input_tables_revision
    ON job_input_tables(source_revision_id);
CREATE INDEX idx_job_input_tables_source
    ON job_input_tables(source_id);

-- Preserve queued work across upgrade by snapshotting the inputs visible at migration time.
INSERT INTO job_input_tables (
    job_id, ordinal, source_table_id, source_id, source_revision_id,
    content_sha256, stored_path, file_kind, sheet_name, start_cell, end_cell,
    first_row_as_header, schema_json, row_count, config_version, cache_key, alias
)
SELECT
    jt.job_id, jt.ordinal, t.id, t.source_id, d.current_revision_id,
    r.content_sha256, d.stored_path, d.file_kind, t.sheet_name, t.start_cell, t.end_cell,
    t.first_row_as_header, t.schema_json, t.row_count, t.config_version, t.cache_key, jt.alias
FROM job_tables jt
JOIN source_tables t ON t.id = jt.source_table_id
JOIN data_sources d ON d.id = t.source_id
LEFT JOIN source_revisions r ON r.id = d.current_revision_id;
