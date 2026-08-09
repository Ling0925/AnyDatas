use chrono::Utc;

use crate::{
    error::{AppError, AppResult},
    models::SharedState,
};

use super::PreparedReplacement;

pub(super) struct ReplacementCommit<'a> {
    pub(super) source_id: &'a str,
    pub(super) stored: &'a crate::api::data_sources::StoredUpload,
    pub(super) final_path: &'a std::path::Path,
    pub(super) prepared: &'a PreparedReplacement,
}

pub(super) async fn persist(state: &SharedState, commit: ReplacementCommit<'_>) -> AppResult<()> {
    let default = commit
        .prepared
        .tables
        .iter()
        .find(|table| table.is_default)
        .ok_or_else(|| AppError::BadRequest("数据文件缺少默认逻辑表".to_owned()))?;
    let now = Utc::now().to_rfc3339();
    let sheet_names_json = serde_json::to_string(&commit.prepared.sheet_names)
        .map_err(|error| AppError::Internal(error.to_string()))?;
    let size_bytes = i64::try_from(commit.stored.size_bytes)
        .map_err(|_| AppError::Internal("上传文件大小超出数据库范围".to_owned()))?;
    let default_row_count = i64::try_from(default.table.total_rows)
        .map_err(|_| AppError::Internal("默认逻辑表行数超出数据库范围".to_owned()))?;
    let default_column_count = i64::try_from(default.fields.len())
        .map_err(|_| AppError::Internal("默认逻辑表列数超出数据库范围".to_owned()))?;
    let mut transaction = state.pool.begin().await?;
    sqlx::query(
        r#"
        UPDATE data_sources
        SET original_filename = ?, stored_path = ?, media_type = ?, file_kind = ?,
            size_bytes = ?, selected_sheet = ?, start_cell = ?, first_row_as_header = ?,
            sheet_names_json = ?, row_count = ?, column_count = ?, updated_at = ?
        WHERE id = ?
        "#,
    )
    .bind(&commit.stored.original_filename)
    .bind(commit.final_path.to_string_lossy().to_string())
    .bind(commit.stored.media_type)
    .bind(commit.stored.file_kind)
    .bind(size_bytes)
    .bind(&default.sheet_name)
    .bind(&default.start_cell)
    .bind(default.first_row_as_header)
    .bind(sheet_names_json)
    .bind(default_row_count)
    .bind(default_column_count)
    .bind(&now)
    .bind(commit.source_id)
    .execute(&mut *transaction)
    .await?;
    for table in &commit.prepared.tables {
        let row_count = i64::try_from(table.table.total_rows)
            .map_err(|_| AppError::Internal("逻辑表行数超出数据库范围".to_owned()))?;
        let column_count = i64::try_from(table.fields.len())
            .map_err(|_| AppError::Internal("逻辑表列数超出数据库范围".to_owned()))?;
        let schema_json = serde_json::to_string(&table.fields)
            .map_err(|error| AppError::Internal(error.to_string()))?;
        sqlx::query(
            r#"
            UPDATE source_tables
            SET row_count = ?, column_count = ?, schema_json = ?,
                config_version = config_version + 1, cache_key = NULL,
                cache_status = 'pending', cache_error = NULL, updated_at = ?
            WHERE id = ? AND source_id = ?
            "#,
        )
        .bind(row_count)
        .bind(column_count)
        .bind(schema_json)
        .bind(&now)
        .bind(&table.id)
        .bind(commit.source_id)
        .execute(&mut *transaction)
        .await?;
    }
    transaction.commit().await?;
    Ok(())
}
