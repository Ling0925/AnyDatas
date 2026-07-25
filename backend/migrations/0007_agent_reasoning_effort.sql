-- 思考等级属于每次 Run 的执行参数，独立持久化后可用于恢复、重试和审计。
ALTER TABLE ai_runs
ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'medium'
    CHECK (reasoning_effort IN ('low', 'medium', 'high'));
