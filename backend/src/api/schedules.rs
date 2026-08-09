use std::str::FromStr;

use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    routing::{get, post, put},
};
use chrono::Utc;
use chrono_tz::Tz;
use cron::Schedule;
use uuid::Uuid;

use crate::{
    api::auth::AuthContext,
    error::{AppError, AppResult},
    models::{
        ScheduleItem, ScheduleRow, SharedState, ToggleScheduleRequest, UpsertScheduleRequest,
    },
    services::query_bindings::{self, BindingTarget, ValidatedBindings},
};

use super::jobs::{enqueue_job, hydrate_job, required_job};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/schedules", get(list).post(create))
        .route("/schedules/{id}", put(update).delete(delete_one))
        .route("/schedules/{id}/toggle", post(toggle))
        .route("/schedules/{id}/run", post(run_now))
}

async fn list(
    State(state): State<SharedState>,
    auth: AuthContext,
) -> AppResult<Json<Vec<ScheduleItem>>> {
    let rows = sqlx::query_as::<_, ScheduleRow>(
        r#"
        SELECT s.id, s.source_id, d.name AS source_name, s.name, s.sql_text,
               s.post_js, s.cron_expression, s.timezone, s.enabled, s.next_run_at,
               s.last_run_at, s.created_at, s.updated_at
        FROM schedules s
        JOIN data_sources d ON d.id = s.source_id
        WHERE d.workspace_id = ?
        ORDER BY s.created_at DESC
        "#,
    )
    .bind(&auth.workspace_id)
    .fetch_all(&state.pool)
    .await?;
    let mut schedules = Vec::with_capacity(rows.len());
    for row in rows {
        schedules.push(hydrate_schedule(&state, row).await?);
    }
    Ok(Json(schedules))
}

async fn create(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(request): Json<UpsertScheduleRequest>,
) -> AppResult<(StatusCode, Json<ScheduleItem>)> {
    auth.require_analyst()?;
    let bindings = validate_request(&state, &request, &auth.workspace_id).await?;
    let id = Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339();
    let next_run_at = if request.enabled {
        next_run(&request.cron_expression, &request.timezone)?
    } else {
        None
    };
    let mut transaction = state.pool.begin().await?;
    sqlx::query(
        r#"
        INSERT INTO schedules (
            id, source_id, name, sql_text, post_js, cron_expression, timezone,
            enabled, next_run_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&id)
    .bind(&bindings.primary_source_id)
    .bind(request.name.trim())
    .bind(request.sql.trim())
    .bind(normalize_optional_text(request.post_js.as_deref()))
    .bind(request.cron_expression.trim())
    .bind(&request.timezone)
    .bind(request.enabled)
    .bind(next_run_at)
    .bind(&now)
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    query_bindings::replace_bindings(
        &mut transaction,
        BindingTarget::Schedule,
        &id,
        &bindings.tables,
    )
    .await?;
    transaction.commit().await?;
    let row = required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    Ok((
        StatusCode::CREATED,
        Json(hydrate_schedule(&state, row).await?),
    ))
}

async fn update(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(request): Json<UpsertScheduleRequest>,
) -> AppResult<Json<ScheduleItem>> {
    auth.require_analyst()?;
    required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    let bindings = validate_request(&state, &request, &auth.workspace_id).await?;
    let now = Utc::now().to_rfc3339();
    let next_run_at = if request.enabled {
        next_run(&request.cron_expression, &request.timezone)?
    } else {
        None
    };
    let mut transaction = state.pool.begin().await?;
    sqlx::query(
        r#"
        UPDATE schedules
        SET source_id = ?, name = ?, sql_text = ?, post_js = ?, cron_expression = ?,
            timezone = ?, enabled = ?, next_run_at = ?, updated_at = ?
        WHERE id = ?
        "#,
    )
    .bind(&bindings.primary_source_id)
    .bind(request.name.trim())
    .bind(request.sql.trim())
    .bind(normalize_optional_text(request.post_js.as_deref()))
    .bind(request.cron_expression.trim())
    .bind(&request.timezone)
    .bind(request.enabled)
    .bind(next_run_at)
    .bind(now)
    .bind(&id)
    .execute(&mut *transaction)
    .await?;
    query_bindings::replace_bindings(
        &mut transaction,
        BindingTarget::Schedule,
        &id,
        &bindings.tables,
    )
    .await?;
    transaction.commit().await?;
    let row = required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    Ok(Json(hydrate_schedule(&state, row).await?))
}

async fn toggle(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(request): Json<ToggleScheduleRequest>,
) -> AppResult<Json<ScheduleItem>> {
    auth.require_analyst()?;
    let schedule = required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    let next_run_at = if request.enabled {
        next_run(&schedule.cron_expression, &schedule.timezone)?
    } else {
        None
    };
    let now = Utc::now().to_rfc3339();
    sqlx::query("UPDATE schedules SET enabled = ?, next_run_at = ?, updated_at = ? WHERE id = ?")
        .bind(request.enabled)
        .bind(next_run_at)
        .bind(now)
        .bind(&id)
        .execute(&state.pool)
        .await?;
    let row = required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    Ok(Json(hydrate_schedule(&state, row).await?))
}

async fn run_now(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<(StatusCode, Json<crate::models::Job>)> {
    auth.require_analyst()?;
    let schedule = required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    let tables =
        query_bindings::load_bindings(&state.pool, BindingTarget::Schedule, &schedule.id).await?;
    let job_id = enqueue_job(
        &state,
        &schedule.source_id,
        &tables,
        &schedule.name,
        &schedule.sql_text,
        schedule.post_js.as_deref(),
        Some(&schedule.id),
        "manual_schedule",
    )
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(
            hydrate_job(
                &state,
                required_job(&state, &job_id, Some(&auth.workspace_id)).await?,
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
    required_schedule(&state, &id, Some(&auth.workspace_id)).await?;
    sqlx::query("DELETE FROM schedules WHERE id = ?")
        .bind(&id)
        .execute(&state.pool)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn validate_request(
    state: &SharedState,
    request: &UpsertScheduleRequest,
    workspace_id: &str,
) -> AppResult<ValidatedBindings> {
    if request.name.trim().is_empty() || request.sql.trim().is_empty() {
        return Err(AppError::BadRequest("计划名称和 SQL 不能为空".to_owned()));
    }
    next_run(&request.cron_expression, &request.timezone)?;
    query_bindings::validate_bindings(
        &state.pool,
        workspace_id,
        request.source_id.as_deref(),
        &request.tables,
    )
    .await
}

/// 为计划任务附加固化的逻辑表绑定，手动运行和周期触发会得到同一查询上下文。
async fn hydrate_schedule(state: &SharedState, row: ScheduleRow) -> AppResult<ScheduleItem> {
    let id = row.id.clone();
    let mut schedule = ScheduleItem::from(row);
    schedule.tables =
        query_bindings::load_bindings(&state.pool, BindingTarget::Schedule, &id).await?;
    Ok(schedule)
}

fn normalize_optional_text(raw: Option<&str>) -> Option<String> {
    raw.map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

pub fn next_run(expression: &str, timezone: &str) -> AppResult<Option<String>> {
    let schedule = Schedule::from_str(expression.trim())
        .map_err(|error| AppError::BadRequest(format!("Cron 表达式无效: {error}")))?;
    let timezone =
        Tz::from_str(timezone).map_err(|_| AppError::BadRequest("时区无效".to_owned()))?;
    Ok(schedule
        .after(&Utc::now().with_timezone(&timezone))
        .next()
        .map(|date| date.with_timezone(&Utc).to_rfc3339()))
}

pub async fn required_schedule(
    state: &SharedState,
    id: &str,
    workspace_id: Option<&str>,
) -> AppResult<ScheduleRow> {
    sqlx::query_as::<_, ScheduleRow>(
        r#"
        SELECT s.id, s.source_id, d.name AS source_name, s.name, s.sql_text,
               s.post_js, s.cron_expression, s.timezone, s.enabled, s.next_run_at,
               s.last_run_at, s.created_at, s.updated_at
        FROM schedules s
        JOIN data_sources d ON d.id = s.source_id
        WHERE s.id = ? AND (? IS NULL OR d.workspace_id = ?)
        "#,
    )
    .bind(id)
    .bind(workspace_id)
    .bind(workspace_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("计划任务不存在".to_owned()))
}
