PRAGMA foreign_keys = ON;

CREATE TABLE source_tables (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    start_cell TEXT NOT NULL DEFAULT 'A1',
    end_cell TEXT,
    first_row_as_header INTEGER NOT NULL DEFAULT 1,
    row_count INTEGER NOT NULL DEFAULT 0,
    column_count INTEGER NOT NULL DEFAULT 0,
    schema_json TEXT NOT NULL DEFAULT '[]',
    config_version INTEGER NOT NULL DEFAULT 1,
    cache_key TEXT,
    cache_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (cache_status IN ('pending', 'building', 'ready', 'failed')),
    cache_error TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_source_tables_source_created
    ON source_tables(source_id, created_at);

CREATE UNIQUE INDEX idx_source_tables_one_default
    ON source_tables(source_id)
    WHERE is_default = 1;

-- 为历史文件生成默认逻辑表，升级后旧 SQL 仍可通过 data 别名运行。
INSERT INTO source_tables (
    id, source_id, name, sheet_name, start_cell, first_row_as_header,
    row_count, column_count, is_default, created_at, updated_at
)
SELECT
    lower(hex(randomblob(16))), id, selected_sheet, selected_sheet,
    start_cell, first_row_as_header, row_count, column_count, 1,
    created_at, updated_at
FROM data_sources;

CREATE TABLE saved_query_tables (
    saved_query_id TEXT NOT NULL REFERENCES saved_queries(id) ON DELETE CASCADE,
    source_table_id TEXT NOT NULL REFERENCES source_tables(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (saved_query_id, ordinal),
    UNIQUE (saved_query_id, alias)
);

INSERT INTO saved_query_tables (saved_query_id, source_table_id, alias, ordinal)
SELECT q.id, t.id, 'data', 0
FROM saved_queries q
JOIN source_tables t ON t.source_id = q.source_id AND t.is_default = 1;

CREATE TABLE job_tables (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_table_id TEXT NOT NULL REFERENCES source_tables(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (job_id, ordinal),
    UNIQUE (job_id, alias)
);

INSERT INTO job_tables (job_id, source_table_id, alias, ordinal)
SELECT j.id, t.id, 'data', 0
FROM jobs j
JOIN source_tables t ON t.source_id = j.source_id AND t.is_default = 1;

CREATE TABLE schedule_tables (
    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    source_table_id TEXT NOT NULL REFERENCES source_tables(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (schedule_id, ordinal),
    UNIQUE (schedule_id, alias)
);

INSERT INTO schedule_tables (schedule_id, source_table_id, alias, ordinal)
SELECT s.id, t.id, 'data', 0
FROM schedules s
JOIN source_tables t ON t.source_id = s.source_id AND t.is_default = 1;

CREATE INDEX idx_saved_query_tables_table
    ON saved_query_tables(source_table_id);
CREATE INDEX idx_job_tables_table
    ON job_tables(source_table_id);
CREATE INDEX idx_schedule_tables_table
    ON schedule_tables(source_table_id);
