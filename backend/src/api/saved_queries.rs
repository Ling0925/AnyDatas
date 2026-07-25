use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::get,
};
use chrono::Utc;
use uuid::Uuid;

use crate::{
    api::auth::AuthContext,
    error::{AppError, AppResult},
    models::{SavedQuery, SavedQueryListParams, SavedQueryPayload, SavedQueryRow, SharedState},
    services::query_bindings::{self, BindingTarget, ValidatedBindings},
};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/saved-queries", get(list).post(create))
        .route(
            "/saved-queries/{id}",
            get(get_one).put(update).delete(delete_one),
        )
}

async fn list(
    State(state): State<SharedState>,
    auth: AuthContext,
    Query(params): Query<SavedQueryListParams>,
) -> AppResult<Json<Vec<SavedQuery>>> {
    let source_id = params.source_id.unwrap_or_default();
    let rows = sqlx::query_as::<_, SavedQueryRow>(
        r#"
        SELECT q.id, q.source_id, d.name AS source_name, q.name, q.sql_text,
               q.created_at, q.updated_at
        FROM saved_queries q
        JOIN data_sources d ON d.id = q.source_id
        WHERE d.workspace_id = ? AND (? = '' OR q.source_id = ?)
        ORDER BY q.updated_at DESC
        "#,
    )
    .bind(&auth.workspace_id)
    .bind(&source_id)
    .bind(&source_id)
    .fetch_all(&state.pool)
    .await?;
    let mut queries = Vec::with_capacity(rows.len());
    for row in rows {
        queries.push(hydrate_query(&state, row).await?);
    }
    Ok(Json(queries))
}

async fn get_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<SavedQuery>> {
    let row = required_query(&state, &id, &auth.workspace_id).await?;
    Ok(Json(hydrate_query(&state, row).await?))
}

async fn create(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(payload): Json<SavedQueryPayload>,
) -> AppResult<(StatusCode, Json<SavedQuery>)> {
    auth.require_analyst()?;
    let bindings = validate_payload(&state, &payload, &auth.workspace_id).await?;
    let id = Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339();
    let mut transaction = state.pool.begin().await?;
    sqlx::query(
        "INSERT INTO saved_queries (id, source_id, name, sql_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
    )
    .bind(&id)
    .bind(&bindings.primary_source_id)
    .bind(payload.name.trim())
    .bind(payload.sql.trim())
    .bind(&now)
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    query_bindings::replace_bindings(
        &mut transaction,
        BindingTarget::SavedQuery,
        &id,
        &bindings.tables,
    )
    .await?;
    transaction.commit().await?;
    let row = required_query(&state, &id, &auth.workspace_id).await?;
    Ok((StatusCode::CREATED, Json(hydrate_query(&state, row).await?)))
}

async fn update(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(payload): Json<SavedQueryPayload>,
) -> AppResult<Json<SavedQuery>> {
    auth.require_analyst()?;
    required_query(&state, &id, &auth.workspace_id).await?;
    let bindings = validate_payload(&state, &payload, &auth.workspace_id).await?;
    let mut transaction = state.pool.begin().await?;
    let result = sqlx::query(
        "UPDATE saved_queries SET source_id = ?, name = ?, sql_text = ?, updated_at = ? WHERE id = ?",
    )
    .bind(&bindings.primary_source_id)
    .bind(payload.name.trim())
    .bind(payload.sql.trim())
    .bind(Utc::now().to_rfc3339())
    .bind(&id)
    .execute(&mut *transaction)
    .await?;
    if result.rows_affected() != 1 {
        return Err(AppError::NotFound("保存的查询不存在".to_owned()));
    }
    query_bindings::replace_bindings(
        &mut transaction,
        BindingTarget::SavedQuery,
        &id,
        &bindings.tables,
    )
    .await?;
    transaction.commit().await?;
    let row = required_query(&state, &id, &auth.workspace_id).await?;
    Ok(Json(hydrate_query(&state, row).await?))
}

async fn delete_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<StatusCode> {
    auth.require_analyst()?;
    required_query(&state, &id, &auth.workspace_id).await?;
    sqlx::query("DELETE FROM saved_queries WHERE id = ?")
        .bind(id)
        .execute(&state.pool)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn validate_payload(
    state: &SharedState,
    payload: &SavedQueryPayload,
    workspace_id: &str,
) -> AppResult<ValidatedBindings> {
    if payload.name.trim().is_empty() {
        return Err(AppError::BadRequest("查询名称不能为空".to_owned()));
    }
    if payload.name.chars().count() > 80 {
        return Err(AppError::BadRequest(
            "查询名称不能超过 80 个字符".to_owned(),
        ));
    }
    if payload.sql.trim().is_empty() {
        return Err(AppError::BadRequest("SQL 不能为空".to_owned()));
    }
    query_bindings::validate_bindings(
        &state.pool,
        workspace_id,
        payload.source_id.as_deref(),
        &payload.tables,
    )
    .await
}

/// 为保存查询附加有序表绑定，列表和详情返回值可直接恢复完整查询上下文。
async fn hydrate_query(state: &SharedState, row: SavedQueryRow) -> AppResult<SavedQuery> {
    let id = row.id.clone();
    let mut query = SavedQuery::from(row);
    query.tables =
        query_bindings::load_bindings(&state.pool, BindingTarget::SavedQuery, &id).await?;
    Ok(query)
}

async fn required_query(
    state: &SharedState,
    id: &str,
    workspace_id: &str,
) -> AppResult<SavedQueryRow> {
    sqlx::query_as::<_, SavedQueryRow>(
        r#"
        SELECT q.id, q.source_id, d.name AS source_name, q.name, q.sql_text,
               q.created_at, q.updated_at
        FROM saved_queries q
        JOIN data_sources d ON d.id = q.source_id
        WHERE q.id = ? AND d.workspace_id = ?
        "#,
    )
    .bind(id)
    .bind(workspace_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("保存的查询不存在".to_owned()))
}
