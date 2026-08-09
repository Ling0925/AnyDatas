use std::{
    sync::{Arc, atomic::Ordering},
    time::Duration,
};

use chrono::{Duration as ChronoDuration, Utc};
use sqlx::Row;
use tokio::time::MissedTickBehavior;

use crate::{
    api::{jobs::required_job, schedules::next_run},
    error::AppError,
    models::{AppState, JobLog, QueryRequest, SharedState},
    services::{
        execution,
        query_bindings::{self, BindingTarget},
    },
};

pub fn spawn_job_worker(state: Arc<AppState>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_millis(750));
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            state
                .metrics
                .job_worker_heartbeat
                .store(Utc::now().timestamp(), Ordering::Relaxed);
            if let Err(error) = claim_and_run_job(state.clone()).await {
                tracing::error!(?error, "background job worker failed");
            }
        }
    });
}

pub fn spawn_schedule_worker(state: Arc<AppState>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(10));
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            state
                .metrics
                .schedule_worker_heartbeat
                .store(Utc::now().timestamp(), Ordering::Relaxed);
            if let Err(error) = enqueue_due_schedules(state.clone()).await {
                tracing::error!(?error, "schedule worker failed");
            }
        }
    });
}

/// 每小时回收到期后台结果，长期单机运行时不依赖额外 Cron 或运维容器。
pub fn spawn_maintenance_worker(state: Arc<AppState>) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(60 * 60));
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
        interval.tick().await;
        state
            .metrics
            .maintenance_worker_heartbeat
            .store(Utc::now().timestamp(), Ordering::Relaxed);
        loop {
            interval.tick().await;
            state
                .metrics
                .maintenance_worker_heartbeat
                .store(Utc::now().timestamp(), Ordering::Relaxed);
            match crate::services::maintenance::cleanup_expired_job_results(&state).await {
                Ok(removed) if removed > 0 => {
                    tracing::info!(removed, "expired background results cleaned");
                }
                Ok(_) => {}
                Err(error) => {
                    tracing::error!(?error, "background result cleanup failed");
                }
            }
        }
    });
}

async fn claim_and_run_job(state: SharedState) -> Result<(), AppError> {
    let candidate =
        sqlx::query("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1")
            .fetch_optional(&state.pool)
            .await?;
    let Some(candidate) = candidate else {
        return Ok(());
    };
    let id: String = candidate.get("id");
    let now = Utc::now().to_rfc3339();
    let claimed = sqlx::query(
        "UPDATE jobs SET status = 'running', progress = 10, started_at = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
    )
    .bind(&now)
    .bind(&now)
    .bind(&id)
    .execute(&state.pool)
    .await?;
    if claimed.rows_affected() == 0 {
        return Ok(());
    }

    // 认领后任何一步出错都必须落到终态，否则任务会永久卡在 running，直到下次进程重启才被
    // recover_interrupted_jobs 收敛。业务型的查询失败在 run_claimed_job 内部已写 failed；
    // 这里兜底处理写入/加载等基础设施型错误（如 SQLite 短暂繁忙）。
    if let Err(error) = run_claimed_job(&state, &id).await {
        tracing::error!(?error, job_id = %id, "background job failed after claim");
        let now = Utc::now().to_rfc3339();
        // 状态守卫保证已被用户取消的任务不会被兜底逻辑重新翻成 failed。
        let _ = sqlx::query(
            "UPDATE jobs SET status = 'failed', error_message = ?, finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
        )
        .bind(error.to_string())
        .bind(&now)
        .bind(&now)
        .bind(&id)
        .execute(&state.pool)
        .await;
    }
    Ok(())
}

/// 执行一个已认领的后台任务并写入终态。所有终态更新都带 `status = 'running'` 守卫，
/// 使执行期间到达的取消不会被 succeeded/failed 覆盖；取消导致守卫落空时清理已生成的制品。
async fn run_claimed_job(state: &SharedState, id: &str) -> Result<(), AppError> {
    append_log(state, id, "info", "开始读取数据文件").await?;
    let job = required_job(state, id, None).await?;
    sqlx::query("UPDATE jobs SET progress = 35, updated_at = ? WHERE id = ?")
        .bind(Utc::now().to_rfc3339())
        .bind(id)
        .execute(&state.pool)
        .await?;
    append_log(state, id, "info", "正在执行 DuckDB 查询").await?;
    let tables = query_bindings::load_bindings(&state.pool, BindingTarget::Job, id).await?;
    let request = QueryRequest {
        source_id: Some(job.source_id),
        tables,
        sql: job.sql_text,
        sheet: None,
        start_cell: None,
        first_row_as_header: None,
        limit: None,
        post_js: job.post_js,
    };
    let artifact_key = id.to_owned();
    let artifact_path = state
        .data_dir
        .join("job-results")
        .join(format!("{artifact_key}.duckdb"));
    let result = execution::execute_job_to_artifact(
        state.clone(),
        &request,
        id.to_owned(),
        artifact_path.clone(),
    )
    .await;

    match result {
        Ok(result) => {
            if result.sample.post_processed {
                append_log(
                    &state,
                    &id,
                    "info",
                    &format!(
                        "后处理 JS 完成，{} ms",
                        result.sample.post_process_ms.unwrap_or(0)
                    ),
                )
                .await?;
                for line in &result.console {
                    append_log(&state, &id, "info", &format!("后处理 console: {line}")).await?;
                }
            }
            append_log(
                &state,
                &id,
                "success",
                &format!(
                    "查询完成，共生成 {} 行完整结果，大小 {:.2} MB",
                    result.total_rows,
                    result.artifact_size_bytes as f64 / 1024.0 / 1024.0
                ),
            )
            .await?;
            let finished = Utc::now();
            let now = finished.to_rfc3339();
            let expires_at =
                (finished + ChronoDuration::days(state.job_result_retention_days)).to_rfc3339();
            let updated = sqlx::query(
                r#"
                UPDATE jobs
                SET status = 'succeeded', progress = 100, result_json = ?,
                    result_row_count = ?, result_artifact_key = ?,
                    result_artifact_format = 'duckdb', result_size_bytes = ?,
                    result_expires_at = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                "#,
            )
            .bind(
                serde_json::to_string(&result.sample)
                    .map_err(|error| AppError::Internal(error.to_string()))?,
            )
            .bind(result.total_rows as i64)
            .bind(&artifact_key)
            .bind(result.artifact_size_bytes as i64)
            .bind(expires_at)
            .bind(&now)
            .bind(&now)
            .bind(id)
            .execute(&state.pool)
            .await?;
            if updated.rows_affected() > 0 {
                append_log(
                    state,
                    id,
                    "success",
                    &format!(
                        "查询完成，共生成 {} 行完整结果，大小 {:.2} MB",
                        result.total_rows,
                        result.artifact_size_bytes as f64 / 1024.0 / 1024.0
                    ),
                )
                .await?;
            } else {
                // 任务在完成瞬间被取消：删除已写好的完整结果，避免为 canceled 任务保留可下载制品。
                remove_artifact_file(&artifact_path, id).await;
            }
        }
        Err(error) => {
            remove_artifact_file(&artifact_path, id).await;
            let now = Utc::now().to_rfc3339();
            let updated = sqlx::query(
                "UPDATE jobs SET status = 'failed', error_message = ?, finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            )
            .bind(error.to_string())
            .bind(&now)
            .bind(&now)
            .bind(id)
            .execute(&state.pool)
            .await?;
            if updated.rows_affected() > 0 {
                append_log(state, id, "error", &error.to_string()).await?;
            }
        }
    }
    Ok(())
}

/// 尽力删除任务制品文件；文件不存在视为成功，其余错误只记录不阻断终态写入。
async fn remove_artifact_file(artifact_path: &std::path::Path, id: &str) {
    if let Err(remove_error) = tokio::fs::remove_file(artifact_path).await
        && remove_error.kind() != std::io::ErrorKind::NotFound
    {
        tracing::warn!(?remove_error, job_id = %id, "failed to remove job artifact");
    }
}

async fn append_log(
    state: &SharedState,
    id: &str,
    level: &str,
    message: &str,
) -> Result<(), AppError> {
    let current: String = sqlx::query_scalar("SELECT logs_json FROM jobs WHERE id = ?")
        .bind(id)
        .fetch_one(&state.pool)
        .await?;
    let mut logs: Vec<JobLog> = serde_json::from_str(&current).unwrap_or_default();
    logs.push(JobLog {
        at: Utc::now().to_rfc3339(),
        level: level.to_owned(),
        message: message.to_owned(),
    });
    sqlx::query("UPDATE jobs SET logs_json = ?, updated_at = ? WHERE id = ?")
        .bind(serde_json::to_string(&logs).map_err(|error| AppError::Internal(error.to_string()))?)
        .bind(Utc::now().to_rfc3339())
        .bind(id)
        .execute(&state.pool)
        .await?;
    Ok(())
}

async fn enqueue_due_schedules(state: SharedState) -> Result<(), AppError> {
    let now = Utc::now().to_rfc3339();
    let rows = sqlx::query(
        r#"
        SELECT id, source_id, name, sql_text, post_js, cron_expression, timezone
        FROM schedules
        WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
        ORDER BY next_run_at
        LIMIT 20
        "#,
    )
    .bind(&now)
    .fetch_all(&state.pool)
    .await?;
    for row in rows {
        let id: String = row.get("id");
        let source_id: String = row.get("source_id");
        let name: String = row.get("name");
        let sql: String = row.get("sql_text");
        let post_js: Option<String> = row.get("post_js");
        let expression: String = row.get("cron_expression");
        let timezone: String = row.get("timezone");
        // 单条计划的 cron/时区不可解析时只跳过该条，不能中断整批，否则会阻塞其后所有到期计划。
        let next = match next_run(&expression, &timezone) {
            Ok(next) => next,
            Err(error) => {
                tracing::error!(?error, schedule_id = %id, "skip schedule with invalid cron/timezone");
                continue;
            }
        };
        let changed = sqlx::query(
            "UPDATE schedules SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ? AND next_run_at <= ?",
        )
        .bind(&now)
        .bind(next)
        .bind(&now)
        .bind(&id)
        .bind(&now)
        .execute(&state.pool)
        .await?;
        if changed.rows_affected() == 1 {
            let tables =
                query_bindings::load_bindings(&state.pool, BindingTarget::Schedule, &id).await?;
            super::api::jobs::enqueue_job(
                &state,
                &source_id,
                &tables,
                &name,
                &sql,
                post_js.as_deref(),
                Some(&id),
                "schedule",
            )
            .await?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use chrono::{Duration as ChronoDuration, Utc};

    use crate::{
        api::jobs::enqueue_job,
        db,
        models::{
            AppState, FieldDefinition, QueryRuntimeLimits, QueryTableBinding, RuntimeMetrics,
            SharedState,
        },
    };

    use super::{claim_and_run_job, enqueue_due_schedules};

    /// 搭建带一个 CSV 数据源与默认逻辑表的最小运行时，用于驱动真实后台执行路径。
    async fn seeded_state() -> (tempfile::TempDir, SharedState) {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path().to_path_buf();
        let pool = db::connect(&format!(
            "sqlite://{}",
            data_dir.join("runtime.db").display()
        ))
        .await
        .unwrap();
        let now = Utc::now().to_rfc3339();
        sqlx::query("INSERT INTO users (id, email, name, password_hash, created_at, updated_at) VALUES ('user-1', 'a@example.com', 'A', 'h', ?, ?)")
            .bind(&now).bind(&now).execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO workspaces (id, name, created_at, updated_at) VALUES ('ws-1', '工作区', ?, ?)")
            .bind(&now).bind(&now).execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO workspace_memberships (user_id, workspace_id, role, created_at) VALUES ('user-1', 'ws-1', 'owner', ?)")
            .bind(&now).execute(&pool).await.unwrap();
        let csv_path = data_dir.join("data.csv");
        std::fs::write(&csv_path, "value\n1\n2\n3\n").unwrap();
        sqlx::query(
            r#"INSERT INTO data_sources (id, name, original_filename, stored_path, media_type, file_kind, size_bytes, selected_sheet, start_cell, first_row_as_header, sheet_names_json, row_count, column_count, created_at, updated_at, workspace_id, created_by_user_id) VALUES ('source-1', '数据', 'data.csv', ?, 'text/csv', 'csv', 16, 'CSV', 'A1', 1, '["CSV"]', 3, 1, ?, ?, 'ws-1', 'user-1')"#,
        )
        .bind(csv_path.to_string_lossy().to_string()).bind(&now).bind(&now).execute(&pool).await.unwrap();
        let schema = serde_json::to_string(&vec![FieldDefinition {
            name: "value".to_owned(),
            data_type: "整数".to_owned(),
            nullable: false,
        }])
        .unwrap();
        sqlx::query(
            r#"INSERT INTO source_tables (id, source_id, name, sheet_name, start_cell, first_row_as_header, row_count, column_count, schema_json, config_version, cache_status, is_default, created_at, updated_at) VALUES ('table-1', 'source-1', 'CSV', 'CSV', 'A1', 1, 3, 1, ?, 1, 'pending', 1, ?, ?)"#,
        )
        .bind(schema).bind(&now).bind(&now).execute(&pool).await.unwrap();
        let state = Arc::new(AppState {
            pool,
            data_dir,
            max_upload_bytes: 10_000_000,
            session_ttl_days: 7,
            cookie_secure: false,
            metrics_token: None,
            allow_private_ai_endpoints: true,
            secret_key: [7u8; 32],
            query_control: Default::default(),
            cache_build_locks: Default::default(),
            query_semaphore: Arc::new(tokio::sync::Semaphore::new(2)),
            file_parse_semaphore: Arc::new(tokio::sync::Semaphore::new(1)),
            query_max_concurrency: 2,
            file_parse_max_concurrency: 1,
            resource_queue_timeout_seconds: 5,
            query_timeout_seconds: 30,
            background_query_timeout_seconds: 60,
            file_parse_timeout_seconds: 60,
            query_runtime: QueryRuntimeLimits {
                memory_limit_mb: 256,
                threads: 2,
                temp_limit_mb: 1_024,
                min_free_space_bytes: 16 * 1024 * 1024,
                max_artifact_bytes: 512 * 1024 * 1024,
            },
            js_runtime: crate::models::JsRuntimeLimits::test_default(),
            job_result_retention_days: 30,
            metrics: RuntimeMetrics::new(),
            agent_control: Default::default(),
            agent_events: Default::default(),
            agent_max_steps: 4,
            agent_timeout_seconds: 30,
            agent_context_chars: 80_000,
        });
        (directory, state)
    }

    #[tokio::test]
    async fn claims_queued_job_and_writes_succeeded_result() {
        let (_directory, state) = seeded_state().await;
        let tables = vec![QueryTableBinding {
            table_id: "table-1".to_owned(),
            alias: "data".to_owned(),
        }];
        let id = enqueue_job(
            &state,
            "source-1",
            &tables,
            "汇总",
            "SELECT SUM(value) AS total FROM data",
            None,
            None,
            "manual",
        )
        .await
        .unwrap();

        claim_and_run_job(state.clone()).await.unwrap();

        let (status, artifact, rows): (String, Option<String>, Option<i64>) = sqlx::query_as(
            "SELECT status, result_artifact_key, result_row_count FROM jobs WHERE id = ?",
        )
        .bind(&id)
        .fetch_one(&state.pool)
        .await
        .unwrap();
        assert_eq!(status, "succeeded");
        assert_eq!(artifact.as_deref(), Some(id.as_str()));
        assert_eq!(rows, Some(1));
    }

    #[tokio::test]
    async fn enqueue_due_schedules_runs_once_and_advances_next_run() {
        let (_directory, state) = seeded_state().await;
        let past = (Utc::now() - ChronoDuration::hours(1)).to_rfc3339();
        let now = Utc::now().to_rfc3339();
        sqlx::query(
            "INSERT INTO schedules (id, source_id, name, sql_text, cron_expression, timezone, enabled, next_run_at, created_at, updated_at) VALUES ('sched-1', 'source-1', '每小时', 'SELECT 1', '0 0 * * * *', 'UTC', 1, ?, ?, ?)",
        )
        .bind(&past).bind(&now).bind(&now).execute(&state.pool).await.unwrap();

        enqueue_due_schedules(state.clone()).await.unwrap();
        let first: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM jobs WHERE schedule_id = 'sched-1'")
                .fetch_one(&state.pool)
                .await
                .unwrap();
        assert_eq!(first, 1);

        // 立即再跑一次：next_run_at 已推进到未来，不应重复入队（rows_affected 守卫生效）。
        enqueue_due_schedules(state.clone()).await.unwrap();
        let second: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM jobs WHERE schedule_id = 'sched-1'")
                .fetch_one(&state.pool)
                .await
                .unwrap();
        assert_eq!(second, 1);

        let next: Option<String> =
            sqlx::query_scalar("SELECT next_run_at FROM schedules WHERE id = 'sched-1'")
                .fetch_one(&state.pool)
                .await
                .unwrap();
        assert!(next.is_some_and(|value| value > now));
    }
}
