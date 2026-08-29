-- Agent 扩展：HITL 等待状态，以及工作区 MCP 服务器配置。
-- SQLite 不能 ALTER CHECK，需要重建 ai_runs / ai_run_steps。

CREATE TABLE ai_run_steps_new (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('model', 'tool')),
    status TEXT NOT NULL
        CHECK (status IN ('running', 'waiting', 'completed', 'failed', 'canceled')),
    tool_name TEXT,
    tool_call_id TEXT,
    input_json TEXT,
    output_json TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, ordinal)
);

INSERT INTO ai_run_steps_new (
    id, run_id, ordinal, kind, status, tool_name, tool_call_id,
    input_json, output_json, error_message, started_at, finished_at
)
SELECT
    id, run_id, ordinal, kind, status, tool_name, tool_call_id,
    input_json, output_json, error_message, started_at, finished_at
FROM ai_run_steps;

DROP TABLE ai_run_steps;

CREATE TABLE ai_runs_new (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    user_message_id TEXT NOT NULL REFERENCES ai_messages(id) ON DELETE CASCADE,
    assistant_message_id TEXT REFERENCES ai_messages(id) ON DELETE SET NULL,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'waiting_user', 'completed', 'failed', 'canceled')),
    model TEXT NOT NULL,
    finish_reason TEXT,
    step_count INTEGER NOT NULL DEFAULT 0,
    request_context_json TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL DEFAULT 'medium'
        CHECK (reasoning_effort IN ('low', 'medium', 'high'))
);

INSERT INTO ai_runs_new (
    id, conversation_id, user_message_id, assistant_message_id, status, model,
    finish_reason, step_count, request_context_json, error_message,
    created_at, started_at, finished_at, updated_at, reasoning_effort
)
SELECT
    id, conversation_id, user_message_id, assistant_message_id, status, model,
    finish_reason, step_count, request_context_json, error_message,
    created_at, started_at, finished_at, updated_at, reasoning_effort
FROM ai_runs;

DROP TABLE ai_runs;
ALTER TABLE ai_runs_new RENAME TO ai_runs;

CREATE INDEX idx_ai_runs_conversation_created
    ON ai_runs(conversation_id, created_at DESC);

CREATE UNIQUE INDEX idx_ai_runs_one_active
    ON ai_runs(conversation_id)
    WHERE status IN ('queued', 'running', 'waiting_user');

CREATE TABLE ai_run_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ai_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('model', 'tool')),
    status TEXT NOT NULL
        CHECK (status IN ('running', 'waiting', 'completed', 'failed', 'canceled')),
    tool_name TEXT,
    tool_call_id TEXT,
    input_json TEXT,
    output_json TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, ordinal)
);

INSERT INTO ai_run_steps (
    id, run_id, ordinal, kind, status, tool_name, tool_call_id,
    input_json, output_json, error_message, started_at, finished_at
)
SELECT
    id, run_id, ordinal, kind, status, tool_name, tool_call_id,
    input_json, output_json, error_message, started_at, finished_at
FROM ai_run_steps_new;

DROP TABLE ai_run_steps_new;

CREATE INDEX idx_ai_run_steps_run_ordinal
    ON ai_run_steps(run_id, ordinal);

ALTER TABLE workspace_ai_settings
ADD COLUMN mcp_servers_json TEXT NOT NULL DEFAULT '[]';
