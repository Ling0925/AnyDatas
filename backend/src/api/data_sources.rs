use std::{
    collections::HashSet,
    path::{Path, PathBuf},
};

use axum::{
    Json, Router,
    extract::{Multipart, Path as AxumPath, Query, State},
    http::StatusCode,
    routing::{get, patch},
};
use chrono::{DateTime, Duration, Utc};
use sqlx::FromRow;
use tokio::io::AsyncWriteExt;
use uuid::Uuid;

use crate::{
    api::auth::AuthContext,
    db,
    error::{AppError, AppResult},
    models::{
        CommitImportRequest, DataSource, DataSourceRow, FieldDefinition, ImportInspection,
        ImportSheetInspection, ImportTableConfig, InspectImportTableRequest, PreviewParams,
        PreviewResponse, SharedState, TableData, UpdateSourceConfig,
    },
    services::{job_results, maintenance, resource_control, spreadsheet},
};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/data-sources", get(list).post(upload))
        .route("/data-sources/inspect", axum::routing::post(inspect_upload))
        .route("/data-sources/import", axum::routing::post(commit_import))
        .route(
            "/data-sources/imports/{token}",
            axum::routing::delete(discard_import),
        )
        .route(
            "/data-sources/imports/{token}/preview",
            axum::routing::post(preview_import),
        )
        .route("/data-sources/{id}", get(get_one).delete(delete_one))
        .route("/data-sources/{id}/config", patch(update_config))
        .route("/data-sources/{id}/preview", get(preview))
}

#[derive(Debug, FromRow)]
struct StagedImportRow {
    id: String,
    original_filename: String,
    stored_path: String,
    media_type: String,
    file_kind: String,
    size_bytes: i64,
    expires_at: String,
}

struct StoredUpload {
    original_filename: String,
    file_kind: &'static str,
    media_type: &'static str,
    path: PathBuf,
    size_bytes: usize,
}

struct PreparedImportTable {
    name: String,
    sheet_name: String,
    start_cell: String,
    end_cell: Option<String>,
    first_row_as_header: bool,
    table: TableData,
    fields: Vec<FieldDefinition>,
}

async fn list(
    State(state): State<SharedState>,
    auth: AuthContext,
) -> AppResult<Json<Vec<DataSource>>> {
    let rows = sqlx::query_as::<_, DataSourceRow>(
        r#"
        SELECT id, name, original_filename, stored_path, media_type, file_kind,
               size_bytes, selected_sheet, start_cell, first_row_as_header,
               sheet_names_json, row_count, column_count, created_at, updated_at
        FROM data_sources
        WHERE workspace_id = ?
        ORDER BY created_at DESC
        "#,
    )
    .bind(&auth.workspace_id)
    .fetch_all(&state.pool)
    .await?;
    Ok(Json(rows.into_iter().map(DataSource::from).collect()))
}

async fn get_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    AxumPath(id): AxumPath<String>,
) -> AppResult<Json<DataSource>> {
    let row = required_source(&state, &id, &auth.workspace_id).await?;
    Ok(Json(row.into()))
}

/// 将文件放入暂存区并返回各 Sheet 的样本和推断类型，正式数据源在用户确认前不会创建。
async fn inspect_upload(
    State(state): State<SharedState>,
    auth: AuthContext,
    multipart: Multipart,
) -> AppResult<(StatusCode, Json<ImportInspection>)> {
    auth.require_analyst()?;
    cleanup_expired_imports(&state).await?;
    let token = Uuid::new_v4().to_string();
    let staged_dir = state.data_dir.join("staging");
    let stored = store_multipart_file(&state, multipart, &staged_dir, &token).await?;
    let inspect_path = stored.path.clone();
    let file_kind = stored.file_kind.to_owned();
    let sheets = match resource_control::run_file_task(
        &state,
        "文件预检",
        move || -> anyhow::Result<Vec<ImportSheetInspection>> {
            let workbook = spreadsheet::inspect_file(&inspect_path, &file_kind)?;
            let mut sheets = Vec::with_capacity(workbook.sheets.len());
            for sheet in workbook.sheets {
                let inspection =
                    inspect_import_table(&inspect_path, &file_kind, &sheet.name, "A1", None, true)?;
                if !inspection.fields.is_empty() {
                    sheets.push(inspection);
                }
            }
            anyhow::ensure!(!sheets.is_empty(), "文件中没有可导入的数据表");
            Ok(sheets)
        },
    )
    .await
    {
        Ok(value) => value,
        Err(error) => {
            let _ = tokio::fs::remove_file(&stored.path).await;
            return Err(error);
        }
    };
    let now = Utc::now();
    let expires_at = now + Duration::hours(24);
    let insert = sqlx::query(
        r#"
        INSERT INTO staged_imports (
            id, workspace_id, user_id, original_filename, stored_path,
            media_type, file_kind, size_bytes, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&token)
    .bind(&auth.workspace_id)
    .bind(&auth.user_id)
    .bind(&stored.original_filename)
    .bind(stored.path.to_string_lossy().to_string())
    .bind(stored.media_type)
    .bind(stored.file_kind)
    .bind(stored.size_bytes as i64)
    .bind(now.to_rfc3339())
    .bind(expires_at.to_rfc3339())
    .execute(&state.pool)
    .await;
    if let Err(error) = insert {
        let _ = tokio::fs::remove_file(&stored.path).await;
        return Err(error.into());
    }
    Ok((
        StatusCode::CREATED,
        Json(ImportInspection {
            token,
            original_filename: stored.original_filename,
            file_kind: stored.file_kind.to_owned(),
            size_bytes: stored.size_bytes,
            sheets,
            expires_at: expires_at.to_rfc3339(),
        }),
    ))
}

/// 根据暂存文件和用户输入的范围重新生成字段与样本，让导入前的类型设置基于真实读取区域。
async fn preview_import(
    State(state): State<SharedState>,
    auth: AuthContext,
    AxumPath(token): AxumPath<String>,
    Json(request): Json<InspectImportTableRequest>,
) -> AppResult<Json<ImportSheetInspection>> {
    auth.require_analyst()?;
    let staged = sqlx::query_as::<_, StagedImportRow>(
        r#"
        SELECT id, original_filename, stored_path, media_type, file_kind,
               size_bytes, expires_at
        FROM staged_imports
        WHERE id = ? AND workspace_id = ? AND user_id = ?
        "#,
    )
    .bind(&token)
    .bind(&auth.workspace_id)
    .bind(&auth.user_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("导入预检已失效，请重新上传".to_owned()))?;
    let expires_at = DateTime::parse_from_rfc3339(&staged.expires_at)
        .map_err(|_| AppError::Internal("暂存记录时间无效".to_owned()))?;
    if expires_at < Utc::now() {
        discard_staged_file(&state, &staged.id, &staged.stored_path).await?;
        return Err(AppError::BadRequest(
            "导入预检已过期，请重新上传".to_owned(),
        ));
    }

    let path = PathBuf::from(staged.stored_path);
    let file_kind = staged.file_kind;
    let inspection = resource_control::run_file_task(&state, "范围预检", move || {
        inspect_import_table(
            &path,
            &file_kind,
            &request.sheet_name,
            &request.start_cell,
            request.end_cell.as_deref(),
            request.first_row_as_header,
        )
    })
    .await?;
    if inspection.fields.is_empty() {
        return Err(AppError::BadRequest(
            "所选范围中没有可读取的数据".to_owned(),
        ));
    }
    Ok(Json(inspection))
}

/// 根据用户确认的字段类型创建正式数据源；文件和元数据均成功后才消费暂存记录。
async fn commit_import(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(request): Json<CommitImportRequest>,
) -> AppResult<(StatusCode, Json<DataSource>)> {
    auth.require_analyst()?;
    if request.tables.is_empty() {
        return Err(AppError::BadRequest("至少选择一张工作表".to_owned()));
    }
    if request.tables.len() > 64 {
        return Err(AppError::BadRequest(
            "单个文件最多导入 64 张工作表".to_owned(),
        ));
    }
    let staged = sqlx::query_as::<_, StagedImportRow>(
        r#"
        SELECT id, original_filename, stored_path, media_type, file_kind,
               size_bytes, expires_at
        FROM staged_imports
        WHERE id = ? AND workspace_id = ? AND user_id = ?
        "#,
    )
    .bind(&request.token)
    .bind(&auth.workspace_id)
    .bind(&auth.user_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or_else(|| AppError::NotFound("导入预检已失效，请重新上传".to_owned()))?;
    let expires_at = DateTime::parse_from_rfc3339(&staged.expires_at)
        .map_err(|_| AppError::Internal("暂存记录时间无效".to_owned()))?;
    if expires_at < Utc::now() {
        discard_staged_file(&state, &staged.id, &staged.stored_path).await?;
        return Err(AppError::BadRequest(
            "导入预检已过期，请重新上传".to_owned(),
        ));
    }

    let staged_path = PathBuf::from(&staged.stored_path);
    let validation_path = staged_path.clone();
    let validation_kind = staged.file_kind.clone();
    let requested_tables = request.tables;
    let (sheet_names, prepared) =
        resource_control::run_file_task(&state, "导入校验", move || {
            prepare_import_tables(&validation_path, &validation_kind, requested_tables)
        })
        .await?;

    let extension = Path::new(&staged.original_filename)
        .extension()
        .and_then(|value| value.to_str())
        .ok_or_else(|| AppError::BadRequest("无法识别文件扩展名".to_owned()))?;
    let source_id = Uuid::new_v4().to_string();
    let final_path = state
        .data_dir
        .join("uploads")
        .join(format!("{source_id}.{extension}"));
    tokio::fs::rename(&staged_path, &final_path).await?;

    let default_table = prepared
        .first()
        .expect("non-empty import has a default table");
    let display_name = Path::new(&staged.original_filename)
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or(&staged.original_filename)
        .to_owned();
    let now = Utc::now().to_rfc3339();
    let database_result: AppResult<()> = async {
        let mut transaction = state.pool.begin().await?;
        sqlx::query(
            r#"
            INSERT INTO data_sources (
                id, name, original_filename, stored_path, media_type, file_kind,
                size_bytes, selected_sheet, start_cell, first_row_as_header,
                sheet_names_json, row_count, column_count, workspace_id,
                created_by_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(&source_id)
        .bind(display_name)
        .bind(&staged.original_filename)
        .bind(final_path.to_string_lossy().to_string())
        .bind(&staged.media_type)
        .bind(&staged.file_kind)
        .bind(staged.size_bytes)
        .bind(&default_table.sheet_name)
        .bind(&default_table.start_cell)
        .bind(default_table.first_row_as_header)
        .bind(
            serde_json::to_string(&sheet_names)
                .map_err(|error| AppError::Internal(error.to_string()))?,
        )
        .bind(default_table.table.total_rows as i64)
        .bind(default_table.fields.len() as i64)
        .bind(&auth.workspace_id)
        .bind(&auth.user_id)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await?;
        for (index, table) in prepared.iter().enumerate() {
            sqlx::query(
                r#"
                INSERT INTO source_tables (
                    id, source_id, name, sheet_name, start_cell, end_cell,
                    first_row_as_header, row_count, column_count, schema_json,
                    is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                "#,
            )
            .bind(Uuid::new_v4().to_string())
            .bind(&source_id)
            .bind(&table.name)
            .bind(&table.sheet_name)
            .bind(&table.start_cell)
            .bind(&table.end_cell)
            .bind(table.first_row_as_header)
            .bind(table.table.total_rows as i64)
            .bind(table.fields.len() as i64)
            .bind(
                serde_json::to_string(&table.fields)
                    .map_err(|error| AppError::Internal(error.to_string()))?,
            )
            .bind(index == 0)
            .bind(&now)
            .bind(&now)
            .execute(&mut *transaction)
            .await?;
        }
        sqlx::query("DELETE FROM staged_imports WHERE id = ?")
            .bind(&staged.id)
            .execute(&mut *transaction)
            .await?;
        transaction.commit().await?;
        Ok(())
    }
    .await;
    if let Err(error) = database_result {
        let _ = tokio::fs::rename(&final_path, &staged_path).await;
        return Err(error);
    }
    Ok((
        StatusCode::CREATED,
        Json(
            required_source(&state, &source_id, &auth.workspace_id)
                .await?
                .into(),
        ),
    ))
}

/// 主动丢弃用户取消的预检文件，避免大文件一直占用数据卷等待过期清理。
async fn discard_import(
    State(state): State<SharedState>,
    auth: AuthContext,
    AxumPath(token): AxumPath<String>,
) -> AppResult<StatusCode> {
    auth.require_analyst()?;
    let staged = sqlx::query_as::<_, StagedImportRow>(
        r#"
        SELECT id, original_filename, stored_path, media_type, file_kind,
               size_bytes, expires_at
        FROM staged_imports
        WHERE id = ? AND workspace_id = ? AND user_id = ?
        "#,
    )
    .bind(&token)
    .bind(&auth.workspace_id)
    .bind(&auth.user_id)
    .fetch_optional(&state.pool)
    .await?;
    if let Some(staged) = staged {
        discard_staged_file(&state, &staged.id, &staged.stored_path).await?;
    }
    Ok(StatusCode::NO_CONTENT)
}

async fn upload(
    State(state): State<SharedState>,
    auth: AuthContext,
    mut multipart: Multipart,
) -> AppResult<(StatusCode, Json<DataSource>)> {
    auth.require_analyst()?;
    let field = multipart
        .next_field()
        .await
        .map_err(|error| AppError::BadRequest(format!("无法读取上传内容: {error}")))?
        .ok_or_else(|| AppError::BadRequest("请选择 Excel 或 CSV 文件".to_owned()))?;
    let original_filename = field
        .file_name()
        .map(str::to_owned)
        .ok_or_else(|| AppError::BadRequest("上传文件缺少名称".to_owned()))?;
    let extension = Path::new(&original_filename)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .ok_or_else(|| AppError::BadRequest("无法识别文件扩展名".to_owned()))?;
    let (file_kind, media_type) = file_metadata(&extension)?;
    let id = Uuid::new_v4().to_string();
    let stored_path = state
        .data_dir
        .join("uploads")
        .join(format!("{id}.{extension}"));
    maintenance::ensure_free_space(&state.data_dir, state.query_runtime.min_free_space_bytes, 0)
        .map_err(|error| AppError::BadRequest(error.to_string()))?;
    let mut output = tokio::fs::File::create(&stored_path).await?;
    let mut size_bytes = 0usize;
    let mut next_space_check = 64 * 1024 * 1024usize;
    let mut field = field;
    while let Some(chunk) = field
        .chunk()
        .await
        .map_err(|error| AppError::BadRequest(format!("上传中断: {error}")))?
    {
        size_bytes = size_bytes.saturating_add(chunk.len());
        if size_bytes > state.max_upload_bytes {
            drop(output);
            let _ = tokio::fs::remove_file(&stored_path).await;
            return Err(AppError::BadRequest("文件超过服务器上传限制".to_owned()));
        }
        if size_bytes >= next_space_check {
            if let Err(error) = maintenance::ensure_free_space(
                &state.data_dir,
                state.query_runtime.min_free_space_bytes,
                0,
            ) {
                drop(output);
                let _ = tokio::fs::remove_file(&stored_path).await;
                return Err(AppError::BadRequest(error.to_string()));
            }
            next_space_check = next_space_check.saturating_add(64 * 1024 * 1024);
        }
        output.write_all(&chunk).await?;
    }
    output.flush().await?;
    drop(output);

    let inspect_path = stored_path.clone();
    let inspection = match resource_control::run_file_task(&state, "文件检查", move || {
        spreadsheet::inspect_file(&inspect_path, file_kind)
    })
    .await
    {
        Ok(value) => value,
        Err(error) => {
            let _ = tokio::fs::remove_file(&stored_path).await;
            return Err(error);
        }
    };
    let first_sheet = inspection
        .sheets
        .first()
        .expect("inspection always has a sheet");
    let default_sheet = first_sheet.name.clone();
    let stats_path = stored_path.clone();
    let default_table = match resource_control::run_file_task(&state, "文件读取", move || {
        spreadsheet::read_table(&stats_path, file_kind, &default_sheet, "A1", true, Some(1))
    })
    .await
    {
        Ok(value) => value,
        Err(error) => {
            let _ = tokio::fs::remove_file(&stored_path).await;
            return Err(error);
        }
    };
    let sheet_names = inspection
        .sheets
        .iter()
        .map(|sheet| sheet.name.clone())
        .collect::<Vec<_>>();
    let now = Utc::now().to_rfc3339();
    let display_name = Path::new(&original_filename)
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or(&original_filename)
        .to_owned();
    let mut transaction = state.pool.begin().await?;
    let insert = sqlx::query(
        r#"
        INSERT INTO data_sources (
            id, name, original_filename, stored_path, media_type, file_kind,
            size_bytes, selected_sheet, start_cell, first_row_as_header,
            sheet_names_json, row_count, column_count, workspace_id,
            created_by_user_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'A1', 1, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(&id)
    .bind(display_name)
    .bind(&original_filename)
    .bind(stored_path.to_string_lossy().to_string())
    .bind(media_type)
    .bind(file_kind)
    .bind(size_bytes as i64)
    .bind(&first_sheet.name)
    .bind(
        serde_json::to_string(&sheet_names)
            .map_err(|error| AppError::Internal(error.to_string()))?,
    )
    .bind(default_table.total_rows as i64)
    .bind(default_table.columns.len() as i64)
    .bind(&auth.workspace_id)
    .bind(&auth.user_id)
    .bind(&now)
    .bind(&now)
    .execute(&mut *transaction)
    .await;
    if let Err(error) = insert {
        let _ = tokio::fs::remove_file(&stored_path).await;
        return Err(error.into());
    }
    let default_schema = serde_json::to_string(&default_table.columns)
        .map_err(|error| AppError::Internal(error.to_string()))?;
    for (index, sheet) in inspection.sheets.iter().enumerate() {
        let table_id = Uuid::new_v4().to_string();
        let is_default = index == 0;
        let row_count = if is_default {
            default_table.total_rows as i64
        } else {
            sheet.row_count.saturating_sub(1) as i64
        };
        sqlx::query(
            r#"
            INSERT INTO source_tables (
                id, source_id, name, sheet_name, start_cell,
                first_row_as_header, row_count, column_count, schema_json,
                is_default, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'A1', 1, ?, ?, ?, ?, ?, ?)
            "#,
        )
        .bind(table_id)
        .bind(&id)
        .bind(&sheet.name)
        .bind(&sheet.name)
        .bind(row_count)
        .bind(sheet.column_count as i64)
        .bind(if is_default { &default_schema } else { "[]" })
        .bind(is_default)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await
        .map_err(|error| {
            let _ = std::fs::remove_file(&stored_path);
            AppError::Database(error)
        })?;
    }
    transaction.commit().await?;
    let source = required_source(&state, &id, &auth.workspace_id).await?;
    Ok((StatusCode::CREATED, Json(source.into())))
}

async fn update_config(
    State(state): State<SharedState>,
    auth: AuthContext,
    AxumPath(id): AxumPath<String>,
    Json(request): Json<UpdateSourceConfig>,
) -> AppResult<Json<DataSource>> {
    auth.require_analyst()?;
    spreadsheet::parse_cell_reference(&request.start_cell)
        .map_err(|error| AppError::BadRequest(error.to_string()))?;
    let source = required_source(&state, &id, &auth.workspace_id).await?;
    let previous_cache_keys = sqlx::query_scalar::<_, String>(
        "SELECT cache_key FROM source_tables WHERE source_id = ? AND cache_key IS NOT NULL",
    )
    .bind(&id)
    .fetch_all(&state.pool)
    .await?;
    let sheet_names: Vec<String> =
        serde_json::from_str(&source.sheet_names_json).unwrap_or_default();
    if !sheet_names
        .iter()
        .any(|sheet| sheet == &request.selected_sheet)
    {
        return Err(AppError::BadRequest("所选工作表不存在".to_owned()));
    }
    let path = PathBuf::from(&source.stored_path);
    let kind = source.file_kind.clone();
    let sheet = request.selected_sheet.clone();
    let start_cell = request.start_cell.to_ascii_uppercase();
    let preview_cell = start_cell.clone();
    let header = request.first_row_as_header;
    let table = resource_control::run_file_task(&state, "文件检查", move || {
        spreadsheet::read_table(&path, &kind, &sheet, &preview_cell, header, Some(1))
    })
    .await?;
    let now = Utc::now().to_rfc3339();
    sqlx::query(
        r#"
        UPDATE data_sources
        SET selected_sheet = ?, start_cell = ?, first_row_as_header = ?,
            row_count = ?, column_count = ?, updated_at = ?
        WHERE id = ? AND workspace_id = ?
        "#,
    )
    .bind(&request.selected_sheet)
    .bind(&start_cell)
    .bind(request.first_row_as_header)
    .bind(table.total_rows as i64)
    .bind(table.columns.len() as i64)
    .bind(now)
    .bind(&id)
    .bind(&auth.workspace_id)
    .execute(&state.pool)
    .await?;
    let schema_json = serde_json::to_string(&table.columns)
        .map_err(|error| AppError::Internal(error.to_string()))?;
    sqlx::query(
        r#"
        UPDATE source_tables
        SET name = ?, sheet_name = ?, start_cell = ?, end_cell = NULL,
            first_row_as_header = ?, row_count = ?, column_count = ?,
            schema_json = ?, config_version = config_version + 1,
            cache_key = NULL, cache_status = 'pending', cache_error = NULL,
            updated_at = ?
        WHERE source_id = ? AND is_default = 1
        "#,
    )
    .bind(&request.selected_sheet)
    .bind(&request.selected_sheet)
    .bind(&start_cell)
    .bind(request.first_row_as_header)
    .bind(table.total_rows as i64)
    .bind(table.columns.len() as i64)
    .bind(schema_json)
    .bind(Utc::now().to_rfc3339())
    .bind(&id)
    .execute(&state.pool)
    .await?;
    maintenance::remove_cache_keys_if_unreferenced(&state, previous_cache_keys)
        .await
        .map_err(|error| AppError::Internal(error.to_string()))?;
    Ok(Json(
        required_source(&state, &id, &auth.workspace_id)
            .await?
            .into(),
    ))
}

async fn preview(
    State(state): State<SharedState>,
    auth: AuthContext,
    AxumPath(id): AxumPath<String>,
    Query(params): Query<PreviewParams>,
) -> AppResult<Json<PreviewResponse>> {
    let source = required_source(&state, &id, &auth.workspace_id).await?;
    let sheet = params.sheet.unwrap_or(source.selected_sheet);
    let start_cell = params.start_cell.unwrap_or(source.start_cell);
    let first_row_as_header = params
        .first_row_as_header
        .unwrap_or(source.first_row_as_header);
    let limit = params.limit.unwrap_or(100).clamp(1, 500);
    let path = PathBuf::from(source.stored_path);
    let kind = source.file_kind;
    let query_sheet = sheet.clone();
    let query_cell = start_cell.clone();
    let table = resource_control::run_file_task(&state, "数据预览", move || {
        spreadsheet::read_table(
            &path,
            &kind,
            &query_sheet,
            &query_cell,
            first_row_as_header,
            Some(limit),
        )
    })
    .await?;
    Ok(Json(PreviewResponse {
        truncated: table.total_rows > table.rows.len(),
        total_rows: table.total_rows,
        columns: table.columns,
        rows: table.rows,
        sheet,
        start_cell,
        end_cell: None,
    }))
}

async fn delete_one(
    State(state): State<SharedState>,
    auth: AuthContext,
    AxumPath(id): AxumPath<String>,
) -> AppResult<StatusCode> {
    auth.require_analyst()?;
    let source = required_source(&state, &id, &auth.workspace_id).await?;
    let cache_keys = sqlx::query_scalar::<_, String>(
        "SELECT cache_key FROM source_tables WHERE source_id = ? AND cache_key IS NOT NULL",
    )
    .bind(&id)
    .fetch_all(&state.pool)
    .await?;
    let artifact_keys = sqlx::query_scalar::<_, String>(
        "SELECT result_artifact_key FROM jobs WHERE source_id = ? AND result_artifact_key IS NOT NULL",
    )
    .bind(&id)
    .fetch_all(&state.pool)
    .await?;
    for artifact_key in artifact_keys {
        job_results::remove_artifact(&state, &artifact_key).await?;
    }
    sqlx::query("DELETE FROM data_sources WHERE id = ? AND workspace_id = ?")
        .bind(&id)
        .bind(&auth.workspace_id)
        .execute(&state.pool)
        .await?;
    if let Err(error) = tokio::fs::remove_file(source.stored_path).await
        && error.kind() != std::io::ErrorKind::NotFound
    {
        tracing::warn!(?error, source_id = %id, "failed to remove uploaded file");
    }
    maintenance::remove_cache_keys_if_unreferenced(&state, cache_keys)
        .await
        .map_err(|error| AppError::Internal(error.to_string()))?;
    Ok(StatusCode::NO_CONTENT)
}

/// 流式保存 Multipart 文件并在超过上限时立即清理，避免将大文件完整缓存在内存中。
async fn store_multipart_file(
    state: &SharedState,
    mut multipart: Multipart,
    directory: &Path,
    file_id: &str,
) -> AppResult<StoredUpload> {
    let field = multipart
        .next_field()
        .await
        .map_err(|error| AppError::BadRequest(format!("无法读取上传内容: {error}")))?
        .ok_or_else(|| AppError::BadRequest("请选择 Excel 或 CSV 文件".to_owned()))?;
    let original_filename = field
        .file_name()
        .map(str::to_owned)
        .ok_or_else(|| AppError::BadRequest("上传文件缺少名称".to_owned()))?;
    let extension = Path::new(&original_filename)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .ok_or_else(|| AppError::BadRequest("无法识别文件扩展名".to_owned()))?;
    let (file_kind, media_type) = file_metadata(&extension)?;
    tokio::fs::create_dir_all(directory).await?;
    maintenance::ensure_free_space(directory, state.query_runtime.min_free_space_bytes, 0)
        .map_err(|error| AppError::BadRequest(error.to_string()))?;
    let path = directory.join(format!("{file_id}.{extension}"));
    let mut output = tokio::fs::File::create(&path).await?;
    let mut size_bytes = 0usize;
    let mut next_space_check = 64 * 1024 * 1024usize;
    let mut field = field;
    while let Some(chunk) = field
        .chunk()
        .await
        .map_err(|error| AppError::BadRequest(format!("上传中断: {error}")))?
    {
        size_bytes = size_bytes.saturating_add(chunk.len());
        if size_bytes > state.max_upload_bytes {
            drop(output);
            let _ = tokio::fs::remove_file(&path).await;
            return Err(AppError::BadRequest("文件超过服务器上传限制".to_owned()));
        }
        if size_bytes >= next_space_check {
            if let Err(error) = maintenance::ensure_free_space(
                directory,
                state.query_runtime.min_free_space_bytes,
                0,
            ) {
                drop(output);
                let _ = tokio::fs::remove_file(&path).await;
                return Err(AppError::BadRequest(error.to_string()));
            }
            next_space_check = next_space_check.saturating_add(64 * 1024 * 1024);
        }
        output.write_all(&chunk).await?;
    }
    output.flush().await?;
    drop(output);
    Ok(StoredUpload {
        original_filename,
        file_kind,
        media_type,
        path,
        size_bytes,
    })
}

/// 重新读取用户确认的范围并校验字段结构，防止客户端篡改 Sheet 名称或遗漏字段。
fn prepare_import_tables(
    path: &Path,
    file_kind: &str,
    requested_tables: Vec<ImportTableConfig>,
) -> anyhow::Result<(Vec<String>, Vec<PreparedImportTable>)> {
    let inspection = spreadsheet::inspect_file(path, file_kind)?;
    let sheet_names = inspection
        .sheets
        .iter()
        .map(|sheet| sheet.name.clone())
        .collect::<Vec<_>>();
    let available = sheet_names.iter().cloned().collect::<HashSet<_>>();
    let mut selected_sheets = HashSet::new();
    let mut table_names = HashSet::new();
    let mut prepared = Vec::with_capacity(requested_tables.len());
    for requested in requested_tables {
        let name = requested.name.trim();
        anyhow::ensure!(!name.is_empty(), "逻辑表名称不能为空");
        anyhow::ensure!(name.chars().count() <= 80, "逻辑表名称不能超过 80 个字符");
        anyhow::ensure!(
            available.contains(&requested.sheet_name),
            "所选工作表不存在"
        );
        anyhow::ensure!(
            selected_sheets.insert(requested.sheet_name.clone()),
            "同一工作表不能重复导入"
        );
        anyhow::ensure!(
            table_names.insert(name.to_ascii_lowercase()),
            "逻辑表名称不能重复"
        );
        let start_cell = requested.start_cell.trim().to_ascii_uppercase();
        let end_cell = requested
            .end_cell
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_ascii_uppercase);
        let table = spreadsheet::read_table_range(
            path,
            file_kind,
            &requested.sheet_name,
            &start_cell,
            end_cell.as_deref(),
            requested.first_row_as_header,
            Some(2_000),
        )?;
        anyhow::ensure!(!table.columns.is_empty(), "所选范围中没有可读取的数据");
        let fields = spreadsheet::apply_field_overrides(&table.columns, Some(&requested.fields))?;
        prepared.push(PreparedImportTable {
            name: name.to_owned(),
            sheet_name: requested.sheet_name,
            start_cell,
            end_cell,
            first_row_as_header: requested.first_row_as_header,
            table,
            fields,
        });
    }
    Ok((sheet_names, prepared))
}

/// 使用与正式导入相同的范围语义生成字段样本，避免弹窗预览和提交结果出现偏差。
fn inspect_import_table(
    path: &Path,
    file_kind: &str,
    sheet_name: &str,
    start_cell: &str,
    end_cell: Option<&str>,
    first_row_as_header: bool,
) -> anyhow::Result<ImportSheetInspection> {
    anyhow::ensure!(!sheet_name.is_empty(), "工作表名称不能为空");
    if file_kind == "csv" {
        anyhow::ensure!(sheet_name == "数据", "所选工作表不存在");
    }
    let start_cell = start_cell.trim().to_ascii_uppercase();
    let end_cell = end_cell
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase);
    let table = spreadsheet::read_table_range(
        path,
        file_kind,
        sheet_name,
        &start_cell,
        end_cell.as_deref(),
        first_row_as_header,
        Some(2_000),
    )?;
    Ok(ImportSheetInspection {
        name: sheet_name.to_owned(),
        row_count: table.total_rows,
        column_count: table.columns.len(),
        fields: table.columns,
        rows: table.rows.into_iter().take(20).collect(),
        start_cell,
        end_cell,
        first_row_as_header,
    })
}

/// 清理超过 24 小时仍未确认的暂存文件，按需执行可避免额外后台服务和运维负担。
async fn cleanup_expired_imports(state: &SharedState) -> AppResult<()> {
    let now = Utc::now().to_rfc3339();
    let expired = sqlx::query_as::<_, (String, String)>(
        "SELECT id, stored_path FROM staged_imports WHERE expires_at < ?",
    )
    .bind(now)
    .fetch_all(&state.pool)
    .await?;
    for (id, path) in expired {
        sqlx::query("DELETE FROM staged_imports WHERE id = ?")
            .bind(id)
            .execute(&state.pool)
            .await?;
        if let Err(error) = tokio::fs::remove_file(&path).await
            && error.kind() != std::io::ErrorKind::NotFound
        {
            tracing::warn!(?error, %path, "failed to remove expired staged import");
        }
    }
    Ok(())
}

/// 同时删除暂存记录和文件；记录先删可保证重复取消请求保持幂等。
async fn discard_staged_file(state: &SharedState, token: &str, stored_path: &str) -> AppResult<()> {
    sqlx::query("DELETE FROM staged_imports WHERE id = ?")
        .bind(token)
        .execute(&state.pool)
        .await?;
    if let Err(error) = tokio::fs::remove_file(stored_path).await
        && error.kind() != std::io::ErrorKind::NotFound
    {
        tracing::warn!(?error, %stored_path, "failed to remove staged import");
    }
    Ok(())
}

async fn required_source(
    state: &SharedState,
    id: &str,
    workspace_id: &str,
) -> AppResult<DataSourceRow> {
    db::get_data_source(&state.pool, id, Some(workspace_id))
        .await?
        .ok_or_else(|| AppError::NotFound("数据文件不存在".to_owned()))
}

fn file_metadata(extension: &str) -> AppResult<(&'static str, &'static str)> {
    match extension {
        "csv" => Ok(("csv", "text/csv")),
        "xlsx" => Ok((
            "excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )),
        "xls" => Ok(("excel", "application/vnd.ms-excel")),
        "xlsb" => Ok((
            "excel",
            "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
        )),
        "ods" => Ok(("excel", "application/vnd.oasis.opendocument.spreadsheet")),
        _ => Err(AppError::BadRequest(
            "仅支持 .xlsx、.xls、.xlsb、.ods 和 .csv 文件".to_owned(),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inspects_a_custom_start_cell_before_import() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("expenses.csv");
        std::fs::write(&path, "报销数据,\n费用项目,金额\n差旅费,120\n办公费,80\n").unwrap();

        let inspection = inspect_import_table(&path, "csv", "数据", "A2", None, true).unwrap();

        assert_eq!(inspection.start_cell, "A2");
        assert_eq!(inspection.row_count, 2);
        assert_eq!(inspection.column_count, 2);
        assert_eq!(inspection.fields[0].name, "费用项目");
        assert_eq!(inspection.fields[1].name, "金额");
        assert_eq!(inspection.rows[0], vec!["差旅费", "120"]);
    }
}
