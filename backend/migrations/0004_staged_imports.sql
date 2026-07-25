PRAGMA foreign_keys = ON;

-- 上传文件先进入暂存区，用户确认 Sheet 和字段类型后才写入正式数据源。
CREATE TABLE staged_imports (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    file_kind TEXT NOT NULL CHECK (file_kind IN ('excel', 'csv')),
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX idx_staged_imports_expiry
    ON staged_imports(expires_at);

CREATE INDEX idx_staged_imports_workspace
    ON staged_imports(workspace_id, created_at);
