use std::path::PathBuf;

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
    db,
    error::{AppError, AppResult},
    models::{
        CreateSourceTableRequest, DataSourceRow, FieldDefinition, PreviewParams, PreviewResponse,
        SharedState, SourceTable, SourceTableListParams, SourceTableRow, TableData,
        UpdateSourceTableRequest,
    },
    services::{maintenance, resource_control, spreadsheet},
};

/// 注册逻辑表路由，使 Sheet、范围配置和预览拥有独立于物理文件的生命周期。
pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/source-tables", get(list))
        .route(
            "/source-tables/{id}",
            get(get_one).patch(update).delete(delete_one),
        )
        .route("/source-tables/{id}/preview", get(preview))
        .route("/data-sources/{id}/tables", axum::routing::post(create))
}

/// 返回当前工作区的逻辑表，可按物理文件筛选，前端因此可以构建文件与 Sheet 树。
async fn list(
    State(state): State<SharedState>,
    auth: AuthContext,
    Query(params): Query<SourceTableListParams>,
) -> AppResult<Json<Vec<SourceTable>>> {
    let rows = sqlx::query_as::<_, SourceTableRow>(
        r#"
        SELECT t.id, t.source_id, d.name AS source_name, d.original_filename,
               d.stored_path, d.file_kind, t.name, t.sheet_name, t.start_cell,
               t.end_cell, t.first_row_as_header, t.row_count, t.column_count,
               t.schema_json, t.config_version, t.cache_status,
               t.cache_error, t.is_default, t.created_at, t.updated_at
        FROM source_tables t
        JOIN data_sources d ON d.id = t.source_id
        WHERE d.workspace_id = ? AND (? IS NULL OR t.source_id = ?)
        ORDER BY d.created_at DESC, t.is_default DESC, t.created_at ASC
        "#,
    )
    .bind(&auth.workspace_id)
    .bind(&params.source_id)
    .bind(&params.source_id)
    .fetch_all(&state.pool)
    .await?;
    Ok(Json(rows.into_iter().map(SourceTable::from).collect()))
}

/// 读取单个逻辑表，所有查询都经过工作区校验，避免跨租户探测表结构。
async fn get_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<SourceTable>> {
    Ok(Json(
        required_table(&state, &id, &auth.workspace_id)
            .await?
            .into(),
    ))
}

/// 在同一文件上创建额外范围，支持一个 Sheet 拆成多张可复用的逻辑表。
async fn create(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(source_id): Path<String>,
    Json(request): Json<CreateSourceTableRequest>,
) -> AppResult<(StatusCode, Json<SourceTable>)> {
    auth.require_analyst()?;
    let source = db::get_data_source(&state.pool, &source_id, Some(&auth.workspace_id))
        .await?
        .ok_or_else(|| AppError::NotFound("数据文件不存在".to_owned()))?;
    let validated = validate_config(
        &state,
        &source,
        &request.name,
        &request.sheet_name,
        &request.start_cell,
        request.end_cell.as_deref(),
        request.first_row_as_header,
    )
    .await?;
    let id = Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339();
    let fields =
        spreadsheet::apply_field_overrides(&validated.table.columns, request.fields.as_deref())
            .map_err(|error| AppError::BadRequest(error.to_string()))?;
    let schema_json =
        serde_json::to_string(&fields).map_err(|error| AppError::Internal(error.to_string()))?;
    sqlx::query(
        r#"
        INSERT INTO source_tables (
            id, source_id, name, sheet_name, start_cell, end_cell,
            first_row_as_header, row_count, column_count, schema_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&id)
    .bind(&source_id)
    .bind(validated.name)
    .bind(validated.sheet_name)
    .bind(validated.start_cell)
    .bind(validated.end_cell)
    .bind(request.first_row_as_header)
    .bind(validated.table.total_rows as i64)
    .bind(validated.table.columns.len() as i64)
    .bind(schema_json)
    .bind(&now)
    .bind(&now)
    .execute(&state.pool)
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(
            required_table(&state, &id, &auth.workspace_id)
                .await?
                .into(),
        ),
    ))
}

/// 更新逻辑表配置并使旧缓存失效，配置版本递增可确保后续查询不会复用错误数据。
async fn update(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(request): Json<UpdateSourceTableRequest>,
) -> AppResult<Json<SourceTable>> {
    auth.require_analyst()?;
    let current = required_table(&state, &id, &auth.workspace_id).await?;
    let previous_cache_key =
        sqlx::query_scalar::<_, Option<String>>("SELECT cache_key FROM source_tables WHERE id = ?")
            .bind(&id)
            .fetch_one(&state.pool)
            .await?;
    let source = db::get_data_source(&state.pool, &current.source_id, Some(&auth.workspace_id))
        .await?
        .ok_or_else(|| AppError::NotFound("数据文件不存在".to_owned()))?;
    let validated = validate_config(
        &state,
        &source,
        &request.name,
        &request.sheet_name,
        &request.start_cell,
        request.end_cell.as_deref(),
        request.first_row_as_header,
    )
    .await?;
    let now = Utc::now().to_rfc3339();
    let fields =
        spreadsheet::apply_field_overrides(&validated.table.columns, request.fields.as_deref())
            .map_err(|error| AppError::BadRequest(error.to_string()))?;
    let schema_json =
        serde_json::to_string(&fields).map_err(|error| AppError::Internal(error.to_string()))?;
    sqlx::query(
        r#"
        UPDATE source_tables
        SET name = ?, sheet_name = ?, start_cell = ?, end_cell = ?,
            first_row_as_header = ?, row_count = ?, column_count = ?,
            schema_json = ?, config_version = config_version + 1,
            cache_key = NULL, cache_status = 'pending', cache_error = NULL,
            updated_at = ?
        WHERE id = ?
        "#,
    )
    .bind(validated.name)
    .bind(validated.sheet_name)
    .bind(validated.start_cell)
    .bind(validated.end_cell)
    .bind(request.first_row_as_header)
    .bind(validated.table.total_rows as i64)
    .bind(validated.table.columns.len() as i64)
    .bind(schema_json)
    .bind(now)
    .bind(&id)
    .execute(&state.pool)
    .await?;
    if let Some(cache_key) = previous_cache_key {
        maintenance::remove_cache_keys_if_unreferenced(&state, [cache_key])
            .await
            .map_err(|error| AppError::Internal(error.to_string()))?;
    }
    Ok(Json(
        required_table(&state, &id, &auth.workspace_id)
            .await?
            .into(),
    ))
}

/// 预览逻辑表自身配置，用户看到的数据与 SQL 中绑定的表保持完全一致。
async fn preview(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Query(params): Query<PreviewParams>,
) -> AppResult<Json<PreviewResponse>> {
    let source_table = required_table(&state, &id, &auth.workspace_id).await?;
    let limit = params.limit.unwrap_or(100).clamp(1, 500);
    let path = PathBuf::from(&source_table.stored_path);
    let kind = source_table.file_kind.clone();
    let sheet = source_table.sheet_name.clone();
    let start_cell = source_table.start_cell.clone();
    let end_cell = source_table.end_cell.clone();
    let first_row_as_header = source_table.first_row_as_header;
    let query_sheet = sheet.clone();
    let query_start = start_cell.clone();
    let query_end = end_cell.clone();
    let mut table = resource_control::run_file_task(&state, "逻辑表预览", move || {
        spreadsheet::read_table_range(
            &path,
            &kind,
            &query_sheet,
            &query_start,
            query_end.as_deref(),
            first_row_as_header,
            Some(limit),
        )
    })
    .await?;
    let persisted_fields: Vec<FieldDefinition> =
        serde_json::from_str(&source_table.schema_json).unwrap_or_default();
    table.columns = spreadsheet::apply_field_overrides(
        &table.columns,
        (!persisted_fields.is_empty()).then_some(persisted_fields.as_slice()),
    )
    .map_err(|error| AppError::BadRequest(error.to_string()))?;
    Ok(Json(PreviewResponse {
        truncated: table.total_rows > table.rows.len(),
        total_rows: table.total_rows,
        columns: table.columns,
        rows: table.rows,
        sheet,
        start_cell,
        end_cell,
    }))
}

/// 删除非默认逻辑表；默认表作为旧接口兼容锚点，必须随物理文件一起删除。
async fn delete_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<StatusCode> {
    auth.require_analyst()?;
    let source_table = required_table(&state, &id, &auth.workspace_id).await?;
    if source_table.is_default {
        return Err(AppError::BadRequest(
            "默认逻辑表不能单独删除，请删除对应数据文件".to_owned(),
        ));
    }
    let cache_key =
        sqlx::query_scalar::<_, Option<String>>("SELECT cache_key FROM source_tables WHERE id = ?")
            .bind(&id)
            .fetch_one(&state.pool)
            .await?;
    sqlx::query("DELETE FROM source_tables WHERE id = ?")
        .bind(&id)
        .execute(&state.pool)
        .await?;
    if let Some(cache_key) = cache_key {
        maintenance::remove_cache_keys_if_unreferenced(&state, [cache_key])
            .await
            .map_err(|error| AppError::Internal(error.to_string()))?;
    }
    Ok(StatusCode::NO_CONTENT)
}

/// 查询并校验逻辑表归属，所有 API 复用同一权限边界可减少遗漏风险。
pub async fn required_table(
    state: &SharedState,
    id: &str,
    workspace_id: &str,
) -> AppResult<SourceTableRow> {
    db::get_source_table(&state.pool, id, Some(workspace_id))
        .await?
        .ok_or_else(|| AppError::NotFound("逻辑表不存在".to_owned()))
}

struct ValidatedTableConfig {
    name: String,
    sheet_name: String,
    start_cell: String,
    end_cell: Option<String>,
    table: TableData,
}

/// 校验 Sheet 与范围并读取样本，保存时即得到可信字段结构，查询阶段不再猜测配置是否合法。
async fn validate_config(
    state: &SharedState,
    source: &DataSourceRow,
    name: &str,
    sheet_name: &str,
    start_cell: &str,
    end_cell: Option<&str>,
    first_row_as_header: bool,
) -> AppResult<ValidatedTableConfig> {
    let name = name.trim();
    if name.is_empty() {
        return Err(AppError::BadRequest("逻辑表名称不能为空".to_owned()));
    }
    let sheets: Vec<String> = serde_json::from_str(&source.sheet_names_json).unwrap_or_default();
    if !sheets.iter().any(|sheet| sheet == sheet_name) {
        return Err(AppError::BadRequest("所选工作表不存在".to_owned()));
    }
    let start_cell = start_cell.trim().to_ascii_uppercase();
    let end_cell = end_cell
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase);
    let path = PathBuf::from(&source.stored_path);
    let kind = source.file_kind.clone();
    let sheet = sheet_name.to_owned();
    let query_start = start_cell.clone();
    let query_end = end_cell.clone();
    let table = resource_control::run_file_task(state, "逻辑表配置检查", move || {
        spreadsheet::read_table_range(
            &path,
            &kind,
            &sheet,
            &query_start,
            query_end.as_deref(),
            first_row_as_header,
            Some(2_000),
        )
    })
    .await?;
    if table.columns.is_empty() {
        return Err(AppError::BadRequest(
            "所选范围中没有可读取的数据".to_owned(),
        ));
    }
    Ok(ValidatedTableConfig {
        name: name.to_owned(),
        sheet_name: sheet_name.to_owned(),
        start_cell,
        end_cell,
        table,
    })
}
