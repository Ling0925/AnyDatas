use std::{collections::HashSet, path::PathBuf};

use axum::{
    Json,
    extract::{Multipart, Path, State},
};
use sqlx::FromRow;
use uuid::Uuid;

mod persistence;

use crate::{
    api::{
        auth::AuthContext,
        data_sources::{StoreMultipartOptions, required_source, store_multipart_file},
    },
    error::{AppError, AppResult},
    models::{DataSource, FieldDefinition, SharedState, TableData},
    services::{maintenance, resource_control, spreadsheet},
};

#[derive(Debug, FromRow)]
struct ExistingTable {
    id: String,
    name: String,
    sheet_name: String,
    start_cell: String,
    end_cell: Option<String>,
    first_row_as_header: bool,
    schema_json: String,
    cache_key: Option<String>,
    is_default: bool,
}

struct PreparedTable {
    id: String,
    table: TableData,
    fields: Vec<FieldDefinition>,
    is_default: bool,
    sheet_name: String,
    start_cell: String,
    first_row_as_header: bool,
    previous_cache_key: Option<String>,
}

struct PreparedReplacement {
    sheet_names: Vec<String>,
    tables: Vec<PreparedTable>,
}

pub(super) async fn replace(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    multipart: Multipart,
) -> AppResult<Json<DataSource>> {
    auth.require_analyst()?;
    let source = required_source(&state, &id, &auth.workspace_id).await?;
    let tables = sqlx::query_as::<_, ExistingTable>(
        r#"
        SELECT id, name, sheet_name, start_cell, end_cell, first_row_as_header,
               schema_json, cache_key, is_default
        FROM source_tables
        WHERE source_id = ?
        ORDER BY is_default DESC, created_at, id
        "#,
    )
    .bind(&id)
    .fetch_all(&state.pool)
    .await?;
    if tables.is_empty() {
        return Err(AppError::BadRequest(
            "数据文件没有可复用的逻辑表配置".to_owned(),
        ));
    }

    let staging_id = Uuid::new_v4().to_string();
    let staging_dir = state.data_dir.join("staging");
    let stored = store_multipart_file(
        &state,
        multipart,
        StoreMultipartOptions {
            directory: &staging_dir,
            file_id: &staging_id,
            reject_tables: true,
        },
    )
    .await?;
    let validation_path = stored.path.clone();
    let validation_kind = stored.file_kind.to_owned();
    let prepared = match resource_control::run_file_task(&state, "替换文件校验", move || {
        prepare_replacement(&validation_path, &validation_kind, tables)
    })
    .await
    {
        Ok(prepared) => prepared,
        Err(error) => {
            let _ = tokio::fs::remove_file(&stored.path).await;
            return Err(error);
        }
    };

    let extension = std::path::Path::new(&stored.original_filename)
        .extension()
        .and_then(|value| value.to_str())
        .ok_or_else(|| AppError::BadRequest("无法识别文件扩展名".to_owned()))?;
    let final_path = state
        .data_dir
        .join("uploads")
        .join(format!("{id}.{extension}"));
    let old_path = PathBuf::from(&source.stored_path);
    let backup_path = old_path.with_file_name(format!(".{id}.backup-{}", Uuid::new_v4()));
    tokio::fs::rename(&old_path, &backup_path).await?;
    if let Err(error) = tokio::fs::rename(&stored.path, &final_path).await {
        tokio::fs::rename(&backup_path, &old_path)
            .await
            .map_err(|rollback| {
                AppError::Internal(format!(
                    "新文件安装失败且旧文件恢复失败: {error}; {rollback}"
                ))
            })?;
        return Err(error.into());
    }

    let cache_keys = prepared
        .tables
        .iter()
        .filter_map(|table| table.previous_cache_key.clone())
        .collect::<Vec<_>>();
    let database_result = persistence::persist(
        &state,
        persistence::ReplacementCommit {
            source_id: &id,
            stored: &stored,
            final_path: &final_path,
            prepared: &prepared,
        },
    )
    .await;
    if let Err(error) = database_result {
        tokio::fs::remove_file(&final_path)
            .await
            .map_err(|rollback| {
                AppError::Internal(format!(
                    "数据库更新失败且新文件清理失败: {error}; {rollback}"
                ))
            })?;
        tokio::fs::rename(&backup_path, &old_path)
            .await
            .map_err(|rollback| {
                AppError::Internal(format!(
                    "数据库更新失败且旧文件恢复失败: {error}; {rollback}"
                ))
            })?;
        return Err(error);
    }
    if let Err(error) = tokio::fs::remove_file(&backup_path).await {
        tracing::warn!(
            ?error,
            source_id = %id,
            backup_path = %backup_path.display(),
            "failed to remove committed replacement backup"
        );
    }
    if let Err(error) = maintenance::remove_cache_keys_if_unreferenced(&state, cache_keys).await {
        tracing::warn!(
            ?error,
            source_id = %id,
            "failed to remove unreferenced caches after source replacement"
        );
    }
    Ok(Json(
        required_source(&state, &id, &auth.workspace_id)
            .await?
            .into(),
    ))
}

fn prepare_replacement(
    path: &std::path::Path,
    file_kind: &str,
    tables: Vec<ExistingTable>,
) -> anyhow::Result<PreparedReplacement> {
    let inspection = spreadsheet::inspect_file(path, file_kind)?;
    let sheet_names = inspection
        .sheets
        .iter()
        .map(|sheet| sheet.name.clone())
        .collect::<Vec<_>>();
    let available = sheet_names.iter().collect::<HashSet<_>>();
    let mut prepared = Vec::with_capacity(tables.len());
    for existing in tables {
        anyhow::ensure!(
            available.contains(&existing.sheet_name),
            "逻辑表 {} 的工作表已不存在",
            existing.name
        );
        let requested: Vec<FieldDefinition> = serde_json::from_str(&existing.schema_json)?;
        let table = spreadsheet::read_table_range(
            path,
            file_kind,
            &existing.sheet_name,
            &existing.start_cell,
            existing.end_cell.as_deref(),
            existing.first_row_as_header,
            Some(2_000),
        )?;
        let fields = spreadsheet::apply_field_overrides(&table.columns, Some(&requested))?;
        prepared.push(PreparedTable {
            id: existing.id,
            table,
            fields,
            is_default: existing.is_default,
            sheet_name: existing.sheet_name,
            start_cell: existing.start_cell,
            first_row_as_header: existing.first_row_as_header,
            previous_cache_key: existing.cache_key,
        });
    }
    Ok(PreparedReplacement {
        sheet_names,
        tables: prepared,
    })
}
