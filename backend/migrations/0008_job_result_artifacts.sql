PRAGMA foreign_keys = ON;

-- 后台任务只在 SQLite 保存有界样本；完整结果使用独立 DuckDB 产物，避免主库膨胀。
ALTER TABLE jobs ADD COLUMN result_artifact_key TEXT;
ALTER TABLE jobs ADD COLUMN result_artifact_format TEXT;
ALTER TABLE jobs ADD COLUMN result_size_bytes INTEGER;
ALTER TABLE jobs ADD COLUMN result_expires_at TEXT;

CREATE INDEX idx_jobs_result_expiry
    ON jobs(result_expires_at)
    WHERE result_artifact_key IS NOT NULL;
