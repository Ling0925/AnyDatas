use std::{sync::Arc, time::Duration};

use chrono::Utc;
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
            if let Err(error) = enqueue_due_schedules(state.clone()).await {
                tracing::error!(?error, "schedule worker failed");
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
    append_log(&state, &id, "info", "开始读取数据文件").await?;
    let job = required_job(&state, &id, None).await?;
    sqlx::query("UPDATE jobs SET progress = 35, updated_at = ? WHERE id = ?")
        .bind(Utc::now().to_rfc3339())
        .bind(&id)
        .execute(&state.pool)
        .await?;
    append_log(&state, &id, "info", "正在执行 DuckDB 查询").await?;
    let tables = query_bindings::load_bindings(&state.pool, BindingTarget::Job, &id).await?;
    let request = QueryRequest {
        source_id: Some(job.source_id),
        tables,
        sql: job.sql_text,
        sheet: None,
        start_cell: None,
        first_row_as_header: None,
        limit: Some(5_000),
    };
    let result = execution::execute_job_request(state.clone(), &request, id.clone()).await;
    let current_status: String = sqlx::query_scalar("SELECT status FROM jobs WHERE id = ?")
        .bind(&id)
        .fetch_one(&state.pool)
        .await?;
    if current_status == "canceled" {
        return Ok(());
    }
    match result {
        Ok(result) => {
            append_log(
                &state,
                &id,
                "success",
                &format!("查询完成，共返回 {} 行", result.row_count),
            )
            .await?;
            let now = Utc::now().to_rfc3339();
            sqlx::query(
                r#"
                UPDATE jobs
                SET status = 'succeeded', progress = 100, result_json = ?,
                    result_row_count = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                "#,
            )
            .bind(
                serde_json::to_string(&result)
                    .map_err(|error| AppError::Internal(error.to_string()))?,
            )
            .bind(result.row_count as i64)
            .bind(&now)
            .bind(&now)
            .bind(&id)
            .execute(&state.pool)
            .await?;
        }
        Err(error) => {
            let message = error.to_string();
            append_log(&state, &id, "error", &message).await?;
            let now = Utc::now().to_rfc3339();
            sqlx::query(
                "UPDATE jobs SET status = 'failed', error_message = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            )
            .bind(message)
            .bind(&now)
            .bind(&now)
            .bind(&id)
            .execute(&state.pool)
            .await?;
        }
    }
    Ok(())
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
        SELECT id, source_id, name, sql_text, cron_expression, timezone
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
        let expression: String = row.get("cron_expression");
        let timezone: String = row.get("timezone");
        let next = next_run(&expression, &timezone)?;
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
                Some(&id),
                "schedule",
            )
            .await?;
        }
    }
    Ok(())
}
