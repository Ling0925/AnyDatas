PRAGMA foreign_keys = ON;

-- Agent 会话由服务端持久化，避免浏览器刷新或本地存储损坏后丢失分析过程。
CREATE TABLE ai_conversations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    context_signature TEXT NOT NULL,
    table_bindings_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summary_through_sequence INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_ai_conversations_owner_updated
    ON ai_conversations(workspace_id, user_id, status, updated_at DESC);

-- 用户与助手消息保持线性序列；重新生成时旧分支标记为 superseded 而不是物理删除。
CREATE TABLE ai_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    sql_text TEXT,
    model TEXT,
    tool_runs_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'superseded')),
    sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, sequence)
);

CREATE INDEX idx_ai_messages_conversation_sequence
    ON ai_messages(conversation_id, state, sequence);

-- Run 是一次用户输入对应的完整 Agent 生命周期，可独立取消、恢复查看和审计失败。
CREATE TABLE ai_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    user_message_id TEXT NOT NULL REFERENCES ai_messages(id) ON DELETE CASCADE,
    assistant_message_id TEXT REFERENCES ai_messages(id) ON DELETE SET NULL,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'canceled')),
    model TEXT NOT NULL,
    finish_reason TEXT,
    step_count INTEGER NOT NULL DEFAULT 0,
    request_context_json TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_ai_runs_conversation_created
    ON ai_runs(conversation_id, created_at DESC);

CREATE UNIQUE INDEX idx_ai_runs_one_active
    ON ai_runs(conversation_id)
    WHERE status IN ('queued', 'running');

-- Step 保存每次模型决策和工具观察，前端可展示真实执行轨迹而不是模拟加载文案。
CREATE TABLE ai_run_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ai_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('model', 'tool')),
    status TEXT NOT NULL
        CHECK (status IN ('running', 'completed', 'failed', 'canceled')),
    tool_name TEXT,
    tool_call_id TEXT,
    input_json TEXT,
    output_json TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, ordinal)
);

CREATE INDEX idx_ai_run_steps_run_ordinal
    ON ai_run_steps(run_id, ordinal);
