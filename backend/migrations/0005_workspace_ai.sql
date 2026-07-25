PRAGMA foreign_keys = ON;

-- 每个工作区独立配置 OpenAI-compatible Chat 接口，密钥仅保存加密后的密文。
CREATE TABLE workspace_ai_settings (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    api_key_ciphertext TEXT,
    updated_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
