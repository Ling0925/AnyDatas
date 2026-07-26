use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::{
        StatusCode,
        header::{CONTENT_DISPOSITION, CONTENT_TYPE},
    },
    response::Response,
    routing::{get, post},
};
use chrono::Utc;
use uuid::Uuid;

use crate::{
    api::auth::AuthContext,
    error::{AppError, AppResult},
    models::{
        CreateJobRequest, Job, JobListParams, JobLog, JobResultPage, JobResultParams, JobRow,
        JobSummary, QueryTableBinding, SharedState,
    },
    services::{
        job_results,
        query_bindings::{self, BindingTarget, ValidatedBindings},
    },
};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/jobs", get(list).post(create))
        .route("/jobs/summary", get(summary))
        .route("/jobs/{id}", get(get_one).delete(delete_one))
        .route("/jobs/{id}/result", get(get_result))
        .route("/jobs/{id}/result.csv", get(download_result))
        .route("/jobs/{id}/cancel", post(cancel))
        .route("/jobs/{id}/retry", post(retry))
}

async fn summary(
    State(state): State<SharedState>,
    auth: AuthContext,
) -> AppResult<Json<JobSummary>> {
    let summary = sqlx::query_as::<_, JobSummary>(
        r#"
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN j.status = 'queued' THEN 1 ELSE 0 END), 0) AS queued,
            COALESCE(SUM(CASE WHEN j.status = 'running' THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN j.status = 'succeeded' THEN 1 ELSE 0 END), 0) AS succeeded,
            COALESCE(SUM(CASE WHEN j.status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
            COALESCE(SUM(CASE WHEN j.status = 'canceled' THEN 1 ELSE 0 END), 0) AS canceled
        FROM jobs j
        JOIN data_sources d ON d.id = j.source_id
        WHERE d.workspace_id = ?
        "#,
    )
    .bind(&auth.workspace_id)
    .fetch_one(&state.pool)
    .await?;
    Ok(Json(summary))
}

async fn list(
    State(state): State<SharedState>,
    auth: AuthContext,
    Query(params): Query<JobListParams>,
) -> AppResult<Json<Vec<Job>>> {
    let limit = params.limit.unwrap_or(100).clamp(1, 500) as i64;
    let status = params.status.unwrap_or_default();
    let query = params.query.unwrap_or_default();
    let pattern = format!("%{query}%");
    let rows = sqlx::query_as::<_, JobRow>(
        r#"
        SELECT j.id, j.source_id, d.name AS source_name, j.schedule_id, j.name,
               j.kind, j.sql_text, j.status, j.progress, j.trigger_type,
               NULL AS result_json, j.result_row_count,
               j.result_artifact_key, j.result_artifact_format,
               j.result_size_bytes, j.result_expires_at,
               j.error_message, '[]' AS logs_json,
               j.created_at, j.started_at, j.finished_at, j.updated_at
        FROM jobs j
        JOIN data_sources d ON d.id = j.source_id
        WHERE d.workspace_id = ?
          AND (? = '' OR j.status = ?)
          AND (? = '' OR j.name LIKE ? OR d.name LIKE ?)
        ORDER BY j.created_at DESC
        LIMIT ?
        "#,
    )
    .bind(&auth.workspace_id)
    .bind(&status)
    .bind(&status)
    .bind(&query)
    .bind(&pattern)
    .bind(&pattern)
    .bind(limit)
    .fetch_all(&state.pool)
    .await?;
    let mut jobs = Vec::with_capacity(rows.len());
    for row in rows {
        jobs.push(hydrate_job(&state, row).await?);
    }
    Ok(Json(jobs))
}

async fn get_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<Job>> {
    let row = required_job(&state, &id, Some(&auth.workspace_id)).await?;
    Ok(Json(hydrate_job(&state, row).await?))
}

/// 按页返回后台完整结果，单次最多 1,000 行，浏览器无需加载整份产物。
async fn get_result(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Query(params): Query<JobResultParams>,
) -> AppResult<Json<JobResultPage>> {
    let job = required_job(&state, &id, Some(&auth.workspace_id)).await?;
    let artifact_key = job
        .result_artifact_key
        .as_deref()
        .ok_or_else(|| AppError::NotFound("该任务没有可用的完整结果".to_owned()))?;
    let offset = params.offset.unwrap_or(0);
    let limit = params.limit.unwrap_or(100).clamp(1, 1_000);
    Ok(Json(
        job_results::load_page(&state, artifact_key, offset, limit).await?,
    ))
}

/// 流式导出完整 CSV，响应体由后台线程边读边发送，不创建第二份临时结果文件。
async fn download_result(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Response> {
    let job = required_job(&state, &id, Some(&auth.workspace_id)).await?;
    let artifact_key = job
        .result_artifact_key
        .as_deref()
        .ok_or_else(|| AppError::NotFound("该任务没有可下载的完整结果".to_owned()))?;
    let body = job_results::csv_body(&state, artifact_key).await?;
    Response::builder()
        .header(CONTENT_TYPE, "text/csv; charset=utf-8")
        .header(
            CONTENT_DISPOSITION,
            format!("attachment; filename=\"anydatas-job-{id}.csv\""),
        )
        .body(body)
        .map_err(|error| AppError::Internal(error.to_string()))
}

async fn create(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(request): Json<CreateJobRequest>,
) -> AppResult<(StatusCode, Json<Job>)> {
    auth.require_analyst()?;
    let bindings = validate_job_request(&state, &request, &auth.workspace_id).await?;
    let id = enqueue_job(
        &state,
        &bindings.primary_source_id,
        &bindings.tables,
        &request.name,
        &request.sql,
        None,
        "manual",
    )
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(
            hydrate_job(
                &state,
                required_job(&state, &id, Some(&auth.workspace_id)).await?,
            )
            .await?,
        ),
    ))
}

async fn cancel(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<Job>> {
    auth.require_analyst()?;
    let job = required_job(&state, &id, Some(&auth.workspace_id)).await?;
    if !matches!(job.status.as_str(), "queued" | "running") {
        return Err(AppError::Conflict(
            "只有排队或运行中的任务可以停止".to_owned(),
        ));
    }
    let now = Utc::now().to_rfc3339();
    let mut logs: Vec<JobLog> = serde_json::from_str(&job.logs_json).unwrap_or_default();
    logs.push(JobLog {
        at: now.clone(),
        level: "warning".into(),
        message: "用户停止了任务".into(),
    });
    sqlx::query(
        "UPDATE jobs SET status = 'canceled', finished_at = ?, updated_at = ?, logs_json = ? WHERE id = ?",
    )
    .bind(&now)
    .bind(&now)
    .bind(serde_json::to_string(&logs).map_err(|error| AppError::Internal(error.to_string()))?)
    .bind(&id)
    .execute(&state.pool)
    .await?;
    let interrupt_handle = {
        let mut queries = state
            .query_control
            .lock()
            .map_err(|_| AppError::Internal("任务控制器不可用".to_owned()))?;
        if job.status == "running" {
            queries.canceled.insert(id.clone());
        }
        queries.active.get(&id).cloned()
    };
    if let Some(handle) = interrupt_handle {
        handle.interrupt();
    }
    Ok(Json(
        hydrate_job(
            &state,
            required_job(&state, &id, Some(&auth.workspace_id)).await?,
        )
        .await?,
    ))
}

async fn retry(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<(StatusCode, Json<Job>)> {
    auth.require_analyst()?;
    let job = required_job(&state, &id, Some(&auth.workspace_id)).await?;
    if matches!(job.status.as_str(), "queued" | "running") {
        return Err(AppError::Conflict("当前任务尚未结束".to_owned()));
    }
    let tables = query_bindings::load_bindings(&state.pool, BindingTarget::Job, &job.id).await?;
    let new_id = enqueue_job(
        &state,
        &job.source_id,
        &tables,
        &job.name,
        &job.sql_text,
        job.schedule_id.as_deref(),
        "retry",
    )
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(
            hydrate_job(
                &state,
                required_job(&state, &new_id, Some(&auth.workspace_id)).await?,
            )
            .await?,
        ),
    ))
}

async fn delete_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<StatusCode> {
    auth.require_analyst()?;
    let job = required_job(&state, &id, Some(&auth.workspace_id)).await?;
    if matches!(job.status.as_str(), "queued" | "running") {
        return Err(AppError::Conflict("请先停止任务再删除".to_owned()));
    }
    if let Some(artifact_key) = job.result_artifact_key.as_deref() {
        job_results::remove_artifact(&state, artifact_key).await?;
    }
    sqlx::query("DELETE FROM jobs WHERE id = ?")
        .bind(&id)
        .execute(&state.pool)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn enqueue_job(
    state: &SharedState,
    source_id: &str,
    tables: &[QueryTableBinding],
    name: &str,
    sql: &str,
    schedule_id: Option<&str>,
    trigger_type: &str,
) -> AppResult<String> {
    let id = Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339();
    let logs = vec![JobLog {
        at: now.clone(),
        level: "info".into(),
        message: "任务已进入队列".into(),
    }];
    let mut transaction = state.pool.begin().await?;
    sqlx::query(
        r#"
        INSERT INTO jobs (
            id, source_id, schedule_id, name, sql_text, status, progress,
            trigger_type, logs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
        "#,
    )
    .bind(&id)
    .bind(source_id)
    .bind(schedule_id)
    .bind(name.trim())
    .bind(sql.trim())
    .bind(trigger_type)
    .bind(serde_json::to_string(&logs).map_err(|error| AppError::Internal(error.to_string()))?)
    .bind(&now)
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    query_bindings::replace_bindings(&mut transaction, BindingTarget::Job, &id, tables).await?;
    transaction.commit().await?;
    Ok(id)
}

async fn validate_job_request(
    state: &SharedState,
    request: &CreateJobRequest,
    workspace_id: &str,
) -> AppResult<ValidatedBindings> {
    if request.name.trim().is_empty() {
        return Err(AppError::BadRequest("任务名称不能为空".to_owned()));
    }
    if request.sql.trim().is_empty() {
        return Err(AppError::BadRequest("SQL 不能为空".to_owned()));
    }
    query_bindings::validate_bindings(
        &state.pool,
        workspace_id,
        request.source_id.as_deref(),
        &request.tables,
    )
    .await
}

/// 为后台任务附加创建时固化的逻辑表绑定，重试与历史查看都不会依赖当前工作台状态。
pub async fn hydrate_job(state: &SharedState, row: JobRow) -> AppResult<Job> {
    let id = row.id.clone();
    let mut job = Job::from(row);
    job.tables = query_bindings::load_bindings(&state.pool, BindingTarget::Job, &id).await?;
    Ok(job)
}

pub async fn required_job(
    state: &SharedState,
    id: &str,
    workspace_id: Option<&str>,
) -> AppResult<JobRow> {
    sqlx::query_as::<_, JobRow>(
        r#"
        SELECT j.id, j.source_id, d.name AS source_name, j.schedule_id, j.name,
               j.kind, j.sql_text, j.status, j.progress, j.trigger_type,
               j.result_json, j.result_row_count,
               j.result_artifact_key, j.result_artifact_format,
               j.result_size_bytes, j.result_expires_at,
               j.error_message, j.logs_json,
               j.created_at, j.started_at, j.finished_at, j.updated_at
        FROM jobs j
        JOIN data_sources d ON d.id = j.source_id
        WHERE j.id = ? AND (? IS NULL OR d.workspace_id = ?)
        "#,
    )
    .bind(id)
    .bind(workspace_id)
    .bind(workspace_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("后台任务不存在".to_owned()))
}
