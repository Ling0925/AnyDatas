use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use chrono::{Duration as ChronoDuration, Utc};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, SqliteConnection, SqlitePool};
use tokio::io::AsyncReadExt;
use uuid::Uuid;

use crate::{
    api::jobs,
    error::{AppError, AppResult},
    models::{FieldDefinition, QueryTableBinding, SharedState, SourceRefreshReceipt},
    services::{maintenance, query_engine, resource_control, spreadsheet},
};

#[derive(Debug)]
pub struct IncomingRevisionFile {
    pub original_filename: String,
    pub file_kind: String,
    pub media_type: String,
    pub staged_path: PathBuf,
    pub size_bytes: u64,
    pub content_sha256: String,
}

#[derive(Debug, Clone)]
pub struct RefreshRequest {
    pub workspace_id: String,
    pub source_id: String,
    pub idempotency_key: String,
    pub saved_query_id: Option<String>,
}

#[derive(Debug, FromRow)]
struct SourceState {
    current_revision_id: Option<String>,
    current_content_sha256: Option<String>,
}

#[derive(Debug, Clone, FromRow)]
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
    config_version: i64,
}

#[derive(Debug)]
struct PreparedTable {
    table: ExistingTable,
    update: query_engine::QueryCacheUpdate,
}

#[derive(Debug)]
struct CandidateCacheGuard {
    cache_root: PathBuf,
    keys: Vec<String>,
    active_counter: Arc<AtomicUsize>,
    armed: bool,
}

impl CandidateCacheGuard {
    fn new(cache_root: PathBuf, active_counter: Arc<AtomicUsize>) -> Self {
        active_counter.fetch_add(1, Ordering::SeqCst);
        Self {
            cache_root,
            keys: Vec::new(),
            active_counter,
            armed: true,
        }
    }

    fn track(&mut self, key: String) {
        self.keys.push(key);
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for CandidateCacheGuard {
    fn drop(&mut self) {
        self.active_counter.fetch_sub(1, Ordering::SeqCst);
        if !self.armed {
            return;
        }
        for key in &self.keys {
            let _ = std::fs::remove_file(self.cache_root.join(format!("{key}.duckdb")));
        }
    }
}

#[derive(Debug)]
struct PreparedRefresh {
    sheet_names: Vec<String>,
    tables: Vec<PreparedTable>,
    cache_guard: CandidateCacheGuard,
}

struct PublicationOutcome {
    receipt: SourceRefreshReceipt,
    committed_candidate: bool,
}

#[derive(Debug, Clone, FromRow)]
struct SavedQueryExecution {
    id: String,
    source_id: String,
    name: String,
    sql_text: String,
    post_js: Option<String>,
    updated_at: String,
}

#[derive(Debug, FromRow)]
struct RefreshReceiptRow {
    id: String,
    content_sha256: String,
    saved_query_id: Option<String>,
    saved_query_updated_at: Option<String>,
    revision_id: Option<String>,
    job_id: Option<String>,
    unchanged: bool,
}

impl RefreshReceiptRow {
    fn into_receipt(self) -> AppResult<SourceRefreshReceipt> {
        Ok(SourceRefreshReceipt {
            refresh_id: self.id,
            revision_id: self
                .revision_id
                .ok_or_else(|| AppError::Internal("刷新回执缺少数据版本".to_owned()))?,
            content_sha256: self.content_sha256,
            unchanged: self.unchanged,
            job_id: self.job_id,
        })
    }
}

pub async fn hash_file(path: &Path) -> AppResult<String> {
    let mut file = tokio::fs::File::open(path).await?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer).await?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex::encode(digest.finalize()))
}

/// Fill hashes for migration-created legacy revisions after their paths become immutable.
pub async fn backfill_legacy_hashes(state: &SharedState) -> AppResult<usize> {
    let rows = sqlx::query_as::<_, (String, String)>(
        "SELECT id, stored_path FROM source_revisions WHERE content_sha256 IS NULL",
    )
    .fetch_all(&state.pool)
    .await?;
    let mut updated = 0usize;
    for (revision_id, stored_path) in rows {
        let path = PathBuf::from(&stored_path);
        if !path.is_file() {
            tracing::warn!(revision_id, %stored_path, "legacy source revision file is missing");
            continue;
        }
        let digest = hash_file(&path).await?;
        let changed = sqlx::query(
            "UPDATE source_revisions SET content_sha256 = ? WHERE id = ? AND content_sha256 IS NULL",
        )
        .bind(&digest)
        .bind(&revision_id)
        .execute(&state.pool)
        .await?;
        if changed.rows_affected() == 1 {
            sqlx::query(
                "UPDATE job_input_tables SET content_sha256 = ? WHERE source_revision_id = ? AND content_sha256 IS NULL",
            )
            .bind(&digest)
            .bind(&revision_id)
            .execute(&state.pool)
            .await?;
            updated += 1;
        }
    }
    Ok(updated)
}

/// Verify every current revision before the server becomes ready. This turns a skewed restore or
/// missing immutable file into a startup failure instead of a later, data-dependent query error.
pub async fn verify_current_revisions(state: &SharedState) -> AppResult<()> {
    let rows = sqlx::query_as::<_, (String, String, i64, Option<String>)>(
        r#"
        SELECT d.id, r.stored_path, r.size_bytes, r.content_sha256
        FROM data_sources d
        JOIN source_revisions r ON r.id = d.current_revision_id
        "#,
    )
    .fetch_all(&state.pool)
    .await?;
    for (source_id, stored_path, expected_size, expected_hash) in rows {
        let path = PathBuf::from(&stored_path);
        let metadata = tokio::fs::metadata(&path).await.map_err(|error| {
            AppError::Internal(format!("数据源 {source_id} 的当前版本文件不存在: {error}"))
        })?;
        if metadata.len() != expected_size.max(0) as u64 {
            return Err(AppError::Internal(format!(
                "数据源 {source_id} 的当前版本文件大小与数据库不一致"
            )));
        }
        if let Some(expected_hash) = expected_hash {
            let actual_hash = hash_file(&path).await?;
            if actual_hash != expected_hash {
                return Err(AppError::Internal(format!(
                    "数据源 {source_id} 的当前版本文件摘要与数据库不一致"
                )));
            }
        }
    }
    Ok(())
}

/// Register the immutable bytes used to create a new data source and publish its current pointer.
#[allow(clippy::too_many_arguments)]
pub async fn register_initial_revision(
    connection: &mut SqliteConnection,
    revision_id: &str,
    source_id: &str,
    content_sha256: &str,
    size_bytes: i64,
    stored_path: &Path,
    original_filename: &str,
    media_type: &str,
    file_kind: &str,
    created_at: &str,
) -> AppResult<()> {
    sqlx::query(
        r#"
        INSERT INTO source_revisions (
            id, source_id, content_sha256, size_bytes, stored_path,
            original_filename, media_type, file_kind, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(revision_id)
    .bind(source_id)
    .bind(content_sha256)
    .bind(size_bytes)
    .bind(stored_path.to_string_lossy().to_string())
    .bind(original_filename)
    .bind(media_type)
    .bind(file_kind)
    .bind(created_at)
    .execute(&mut *connection)
    .await?;
    let updated = sqlx::query("UPDATE data_sources SET current_revision_id = ? WHERE id = ?")
        .bind(revision_id)
        .bind(source_id)
        .execute(&mut *connection)
        .await?;
    if updated.rows_affected() != 1 {
        return Err(AppError::Internal(
            "新数据源未能绑定初始数据版本".to_owned(),
        ));
    }
    Ok(())
}

pub async fn refresh_source(
    state: &SharedState,
    request: RefreshRequest,
    file: IncomingRevisionFile,
) -> AppResult<SourceRefreshReceipt> {
    validate_idempotency_key(&request.idempotency_key)?;
    validate_sha256(&file.content_sha256)?;
    if let Some(existing) =
        find_receipt(&state.pool, &request.source_id, &request.idempotency_key).await?
    {
        if existing.content_sha256 != file.content_sha256
            || existing.saved_query_id.as_deref() != request.saved_query_id.as_deref()
        {
            remove_staged_file(&file.staged_path).await;
            return Err(AppError::Conflict(
                "同一个幂等键不能用于不同的文件或查询".to_owned(),
            ));
        }
        // A committed receipt remains replayable even if its saved query was later deleted.
        if let Some(saved_query_id) = request.saved_query_id.as_deref()
            && let Ok(current_query) =
                load_saved_query(&state.pool, saved_query_id, &request.workspace_id).await
            && existing.saved_query_updated_at.as_deref() != Some(current_query.updated_at.as_str())
        {
            remove_staged_file(&file.staged_path).await;
            return Err(AppError::Conflict(
                "同一个幂等键不能用于已修改的查询".to_owned(),
            ));
        }
        remove_staged_file(&file.staged_path).await;
        return existing.into_receipt();
    }
    let saved_query = match request.saved_query_id.as_deref() {
        Some(id) => Some(load_saved_query(&state.pool, id, &request.workspace_id).await?),
        None => None,
    };

    let source = load_source_state(&state.pool, &request.source_id, &request.workspace_id).await?;
    if source.current_content_sha256.as_deref() == Some(file.content_sha256.as_str()) {
        let revision_id = source
            .current_revision_id
            .ok_or_else(|| AppError::Internal("数据源缺少当前版本".to_owned()))?;
        let receipt = publish_unchanged_receipt(
            state,
            &request,
            &file.content_sha256,
            &revision_id,
            saved_query.as_ref(),
        )
        .await?;
        remove_staged_file(&file.staged_path).await;
        return Ok(receipt);
    }

    let tables = load_existing_tables(&state.pool, &request.source_id).await?;
    if tables.is_empty() {
        remove_staged_file(&file.staged_path).await;
        return Err(AppError::BadRequest(
            "数据文件没有可复用的逻辑表配置".to_owned(),
        ));
    }
    if let Some(saved_query) = &saved_query {
        validate_saved_query_source(&state.pool, saved_query, &request.source_id).await?;
    }

    let revision_id = Uuid::new_v4().to_string();
    let _storage_guard = state.storage_maintenance_lock.lock().await;
    let mut prepared = prepare_candidate(state, &file, &revision_id, tables).await?;
    let candidate_cache_keys = prepared
        .tables
        .iter()
        .map(|table| table.update.cache_key.clone())
        .collect::<Vec<_>>();
    let extension = match Path::new(&file.original_filename)
        .extension()
        .and_then(|value| value.to_str())
    {
        Some(value) => value,
        None => {
            let _ =
                maintenance::remove_cache_keys_if_unreferenced(state, candidate_cache_keys).await;
            return Err(AppError::BadRequest("无法识别文件扩展名".to_owned()));
        }
    };
    let revision_dir = state.data_dir.join("uploads").join(&request.source_id);
    if let Err(error) = tokio::fs::create_dir_all(&revision_dir).await {
        let _ = maintenance::remove_cache_keys_if_unreferenced(state, candidate_cache_keys).await;
        return Err(error.into());
    }
    let final_path = revision_dir.join(format!("{revision_id}.{extension}"));
    if let Err(error) = tokio::fs::rename(&file.staged_path, &final_path).await {
        let _ = maintenance::remove_cache_keys_if_unreferenced(state, candidate_cache_keys).await;
        return Err(error.into());
    }

    let previous_cache_keys = prepared
        .tables
        .iter()
        .filter_map(|table| table.table.cache_key.clone())
        .collect::<Vec<_>>();
    let publication = publish_refresh(
        state,
        &request,
        &file,
        &final_path,
        &revision_id,
        &source,
        &prepared,
        saved_query.as_ref(),
    )
    .await;
    match publication {
        Ok(outcome) if outcome.committed_candidate => {
            prepared.cache_guard.disarm();
            if let Err(error) =
                maintenance::remove_cache_keys_if_unreferenced(state, previous_cache_keys).await
            {
                tracing::warn!(?error, source_id = %request.source_id, "failed to remove old revision caches");
            }
            Ok(outcome.receipt)
        }
        Ok(outcome) => {
            let _ =
                maintenance::remove_cache_keys_if_unreferenced(state, candidate_cache_keys).await;
            let _ = tokio::fs::remove_file(&final_path).await;
            Ok(outcome.receipt)
        }
        Err(error) => {
            let _ =
                maintenance::remove_cache_keys_if_unreferenced(state, candidate_cache_keys).await;
            let _ = tokio::fs::remove_file(&final_path).await;
            if let Some(existing) =
                find_receipt(&state.pool, &request.source_id, &request.idempotency_key).await?
            {
                validate_existing_receipt(&existing, &file.content_sha256, saved_query.as_ref())?;
                return existing.into_receipt();
            }
            Err(error)
        }
    }
}

async fn prepare_candidate(
    state: &SharedState,
    file: &IncomingRevisionFile,
    revision_id: &str,
    tables: Vec<ExistingTable>,
) -> AppResult<PreparedRefresh> {
    let path = file.staged_path.clone();
    let file_kind = file.file_kind.clone();
    let revision_id = revision_id.to_owned();
    let content_sha256 = file.content_sha256.clone();
    let cache_root = state.data_dir.join("table-cache");
    let state_for_task = state.clone();
    let query_permit = resource_control::acquire_permit(
        state.query_semaphore.clone(),
        state.resource_queue_timeout_seconds,
        "查询执行器",
    )
    .await?;
    resource_control::run_file_task(state, "完整刷新校验", move || {
        let _query_permit = query_permit;
        let inspection = spreadsheet::inspect_file(&path, &file_kind)?;
        let sheet_names = inspection
            .sheets
            .iter()
            .map(|sheet| sheet.name.clone())
            .collect::<Vec<_>>();
        let available = sheet_names.iter().collect::<HashSet<_>>();
        for table in &tables {
            anyhow::ensure!(
                available.contains(&table.sheet_name),
                "逻辑表 {} 的工作表已不存在",
                table.name
            );
        }
        let parsed_tables = tables
            .into_iter()
            .map(|table| {
                let columns: Vec<FieldDefinition> = serde_json::from_str(&table.schema_json)?;
                Ok::<_, anyhow::Error>((table, columns))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut prepared: Vec<PreparedTable> = Vec::with_capacity(parsed_tables.len());
        let mut cache_guard = CandidateCacheGuard::new(
            cache_root.clone(),
            state_for_task.active_refresh_preparations.clone(),
        );
        for (index, (table, columns)) in parsed_tables.into_iter().enumerate() {
            let source = query_engine::QuerySource {
                table_id: table.id.clone(),
                config_version: table.config_version + 1,
                revision_id: Some(revision_id.clone()),
                content_sha256: Some(content_sha256.clone()),
                path: path.clone(),
                file_kind: file_kind.clone(),
                sheet: table.sheet_name.clone(),
                start_cell: table.start_cell.clone(),
                end_cell: table.end_cell.clone(),
                first_row_as_header: table.first_row_as_header,
                alias: format!("refresh_{index}"),
                columns,
                row_count: 0,
            };
            let update = query_engine::prepare_source_cache_only(
                &source,
                &cache_root,
                &state_for_task.cache_build_locks,
                &state_for_task.query_runtime,
            )?;
            cache_guard.track(update.cache_key.clone());
            prepared.push(PreparedTable { table, update });
        }
        Ok(PreparedRefresh {
            sheet_names,
            tables: prepared,
            cache_guard,
        })
    })
    .await
}

#[allow(clippy::too_many_arguments)]
async fn publish_refresh(
    state: &SharedState,
    request: &RefreshRequest,
    file: &IncomingRevisionFile,
    final_path: &Path,
    revision_id: &str,
    source: &SourceState,
    prepared: &PreparedRefresh,
    saved_query: Option<&SavedQueryExecution>,
) -> AppResult<PublicationOutcome> {
    let now = Utc::now();
    let now_text = now.to_rfc3339();
    let retained_until = (now + ChronoDuration::days(state.job_result_retention_days)).to_rfc3339();
    let default = prepared
        .tables
        .iter()
        .find(|table| table.table.is_default)
        .ok_or_else(|| AppError::BadRequest("数据文件缺少默认逻辑表".to_owned()))?;
    let sheet_names_json = serde_json::to_string(&prepared.sheet_names)
        .map_err(|error| AppError::Internal(error.to_string()))?;
    let mut transaction = state.pool.begin().await?;

    if let Some(existing) = find_receipt_on_connection(
        &mut transaction,
        &request.source_id,
        &request.idempotency_key,
    )
    .await?
    {
        validate_existing_receipt(&existing, &file.content_sha256, saved_query)?;
        transaction.rollback().await?;
        return Ok(PublicationOutcome {
            receipt: existing.into_receipt()?,
            committed_candidate: false,
        });
    }

    sqlx::query(
        r#"
        INSERT INTO source_revisions (
            id, source_id, content_sha256, size_bytes, stored_path,
            original_filename, media_type, file_kind, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        "#,
    )
    .bind(revision_id)
    .bind(&request.source_id)
    .bind(&file.content_sha256)
    .bind(
        i64::try_from(file.size_bytes)
            .map_err(|_| AppError::BadRequest("文件大小超出平台范围".to_owned()))?,
    )
    .bind(final_path.to_string_lossy().to_string())
    .bind(&file.original_filename)
    .bind(&file.media_type)
    .bind(&file.file_kind)
    .bind(&now_text)
    .execute(&mut *transaction)
    .await?;

    let updated_source = sqlx::query(
        r#"
        UPDATE data_sources
        SET current_revision_id = ?, original_filename = ?, stored_path = ?,
            media_type = ?, file_kind = ?, size_bytes = ?, selected_sheet = ?,
            start_cell = ?, first_row_as_header = ?, sheet_names_json = ?,
            row_count = ?, column_count = ?, updated_at = ?
        WHERE id = ? AND workspace_id = ? AND current_revision_id IS ?
        "#,
    )
    .bind(revision_id)
    .bind(&file.original_filename)
    .bind(final_path.to_string_lossy().to_string())
    .bind(&file.media_type)
    .bind(&file.file_kind)
    .bind(i64::try_from(file.size_bytes).unwrap_or(i64::MAX))
    .bind(&default.table.sheet_name)
    .bind(&default.table.start_cell)
    .bind(default.table.first_row_as_header)
    .bind(sheet_names_json)
    .bind(default.update.row_count as i64)
    .bind(default.update.columns.len() as i64)
    .bind(&now_text)
    .bind(&request.source_id)
    .bind(&request.workspace_id)
    .bind(&source.current_revision_id)
    .execute(&mut *transaction)
    .await?;
    if updated_source.rows_affected() != 1 {
        return Err(AppError::Conflict(
            "数据源在刷新期间发生变化，请重新采集".to_owned(),
        ));
    }

    if let Some(previous_revision_id) = source.current_revision_id.as_deref() {
        sqlx::query(
            "UPDATE source_revisions SET retained_until = COALESCE(retained_until, ?) WHERE id = ?",
        )
        .bind(&retained_until)
        .bind(previous_revision_id)
        .execute(&mut *transaction)
        .await?;
    }

    for prepared_table in &prepared.tables {
        let schema_json = serde_json::to_string(&prepared_table.update.columns)
            .map_err(|error| AppError::Internal(error.to_string()))?;
        let updated = sqlx::query(
            r#"
            UPDATE source_tables
            SET row_count = ?, column_count = ?, schema_json = ?,
                config_version = ?, cache_key = ?, cache_status = 'ready',
                cache_error = NULL, updated_at = ?
            WHERE id = ? AND source_id = ? AND config_version = ?
            "#,
        )
        .bind(prepared_table.update.row_count as i64)
        .bind(prepared_table.update.columns.len() as i64)
        .bind(schema_json)
        .bind(prepared_table.update.config_version)
        .bind(&prepared_table.update.cache_key)
        .bind(&now_text)
        .bind(&prepared_table.table.id)
        .bind(&request.source_id)
        .bind(prepared_table.table.config_version)
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(AppError::Conflict(
                "逻辑表配置在刷新期间发生变化，请重新采集".to_owned(),
            ));
        }
    }

    let job_id = if let Some(expected_query) = saved_query {
        let current_query = load_saved_query_on_connection(
            &mut transaction,
            &expected_query.id,
            &request.workspace_id,
        )
        .await?;
        if current_query.updated_at != expected_query.updated_at {
            return Err(AppError::Conflict(
                "保存的查询在刷新期间发生变化，请重新采集".to_owned(),
            ));
        }
        let bindings = load_saved_query_bindings(&mut transaction, &current_query.id).await?;
        validate_bindings_belong_to_source(&mut transaction, &bindings, &request.source_id).await?;
        Some(
            jobs::enqueue_job_in_transaction(
                &mut transaction,
                &current_query.source_id,
                &bindings,
                &current_query.name,
                &current_query.sql_text,
                current_query.post_js.as_deref(),
                None,
                "local_refresh",
            )
            .await?,
        )
    } else {
        None
    };

    let refresh_id = Uuid::new_v4().to_string();
    sqlx::query(
        r#"
        INSERT INTO source_refresh_runs (
            id, source_id, idempotency_key, content_sha256, saved_query_id,
            saved_query_updated_at, revision_id, job_id, unchanged,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        "#,
    )
    .bind(&refresh_id)
    .bind(&request.source_id)
    .bind(&request.idempotency_key)
    .bind(&file.content_sha256)
    .bind(saved_query.map(|query| query.id.as_str()))
    .bind(saved_query.map(|query| query.updated_at.as_str()))
    .bind(revision_id)
    .bind(&job_id)
    .bind(&now_text)
    .bind(&now_text)
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok(PublicationOutcome {
        receipt: SourceRefreshReceipt {
            refresh_id,
            revision_id: revision_id.to_owned(),
            content_sha256: file.content_sha256.clone(),
            unchanged: false,
            job_id,
        },
        committed_candidate: true,
    })
}

async fn publish_unchanged_receipt(
    state: &SharedState,
    request: &RefreshRequest,
    content_sha256: &str,
    revision_id: &str,
    saved_query: Option<&SavedQueryExecution>,
) -> AppResult<SourceRefreshReceipt> {
    let now = Utc::now().to_rfc3339();
    let refresh_id = Uuid::new_v4().to_string();
    let publication: AppResult<()> = async {
        let mut transaction = state.pool.begin().await?;
        let pinned = sqlx::query(
            r#"
            UPDATE data_sources
            SET current_revision_id = current_revision_id
            WHERE id = ? AND workspace_id = ? AND current_revision_id = ?
            "#,
        )
        .bind(&request.source_id)
        .bind(&request.workspace_id)
        .bind(revision_id)
        .execute(&mut *transaction)
        .await?;
        if pinned.rows_affected() != 1 {
            return Err(AppError::Conflict(
                "数据源在确认未变化期间已被刷新，请重试".to_owned(),
            ));
        }
        if let Some(expected_query) = saved_query {
            let current_query = load_saved_query_on_connection(
                &mut transaction,
                &expected_query.id,
                &request.workspace_id,
            )
            .await?;
            if current_query.updated_at != expected_query.updated_at {
                return Err(AppError::Conflict(
                    "保存的查询在刷新期间发生变化，请重新采集".to_owned(),
                ));
            }
        }
        sqlx::query(
            r#"
            INSERT INTO source_refresh_runs (
                id, source_id, idempotency_key, content_sha256, saved_query_id,
                saved_query_updated_at, revision_id, unchanged, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            "#,
        )
        .bind(&refresh_id)
        .bind(&request.source_id)
        .bind(&request.idempotency_key)
        .bind(content_sha256)
        .bind(saved_query.map(|query| query.id.as_str()))
        .bind(saved_query.map(|query| query.updated_at.as_str()))
        .bind(revision_id)
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(())
    }
    .await;
    match publication {
        Ok(()) => Ok(SourceRefreshReceipt {
            refresh_id,
            revision_id: revision_id.to_owned(),
            content_sha256: content_sha256.to_owned(),
            unchanged: true,
            job_id: None,
        }),
        Err(error) => {
            if let Some(existing) =
                find_receipt(&state.pool, &request.source_id, &request.idempotency_key).await?
            {
                validate_existing_receipt(&existing, content_sha256, saved_query)?;
                existing.into_receipt()
            } else {
                Err(error)
            }
        }
    }
}

async fn load_source_state(
    pool: &SqlitePool,
    source_id: &str,
    workspace_id: &str,
) -> AppResult<SourceState> {
    sqlx::query_as::<_, SourceState>(
        r#"
        SELECT d.current_revision_id,
               r.content_sha256 AS current_content_sha256
        FROM data_sources d
        LEFT JOIN source_revisions r ON r.id = d.current_revision_id
        WHERE d.id = ? AND d.workspace_id = ?
        "#,
    )
    .bind(source_id)
    .bind(workspace_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::NotFound("数据文件不存在".to_owned()))
}

async fn load_existing_tables(pool: &SqlitePool, source_id: &str) -> AppResult<Vec<ExistingTable>> {
    Ok(sqlx::query_as::<_, ExistingTable>(
        r#"
        SELECT id, name, sheet_name, start_cell, end_cell, first_row_as_header,
               schema_json, cache_key, is_default, config_version
        FROM source_tables
        WHERE source_id = ?
        ORDER BY is_default DESC, created_at, id
        "#,
    )
    .bind(source_id)
    .fetch_all(pool)
    .await?)
}

async fn load_saved_query(
    pool: &SqlitePool,
    saved_query_id: &str,
    workspace_id: &str,
) -> AppResult<SavedQueryExecution> {
    sqlx::query_as::<_, SavedQueryExecution>(
        r#"
        SELECT q.id, q.source_id, q.name, q.sql_text, q.post_js, q.updated_at
        FROM saved_queries q
        JOIN data_sources d ON d.id = q.source_id
        WHERE q.id = ? AND d.workspace_id = ?
        "#,
    )
    .bind(saved_query_id)
    .bind(workspace_id)
    .fetch_optional(pool)
    .await?
    .ok_or_else(|| AppError::NotFound("保存的查询不存在".to_owned()))
}

async fn load_saved_query_on_connection(
    connection: &mut SqliteConnection,
    saved_query_id: &str,
    workspace_id: &str,
) -> AppResult<SavedQueryExecution> {
    sqlx::query_as::<_, SavedQueryExecution>(
        r#"
        SELECT q.id, q.source_id, q.name, q.sql_text, q.post_js, q.updated_at
        FROM saved_queries q
        JOIN data_sources d ON d.id = q.source_id
        WHERE q.id = ? AND d.workspace_id = ?
        "#,
    )
    .bind(saved_query_id)
    .bind(workspace_id)
    .fetch_optional(&mut *connection)
    .await?
    .ok_or_else(|| AppError::NotFound("保存的查询不存在".to_owned()))
}

async fn load_saved_query_bindings(
    connection: &mut SqliteConnection,
    saved_query_id: &str,
) -> AppResult<Vec<QueryTableBinding>> {
    Ok(sqlx::query_as::<_, QueryTableBinding>(
        "SELECT source_table_id AS table_id, alias FROM saved_query_tables WHERE saved_query_id = ? ORDER BY ordinal",
    )
    .bind(saved_query_id)
    .fetch_all(&mut *connection)
    .await?)
}

async fn validate_saved_query_source(
    pool: &SqlitePool,
    saved_query: &SavedQueryExecution,
    source_id: &str,
) -> AppResult<()> {
    let bindings = sqlx::query_as::<_, QueryTableBinding>(
        "SELECT source_table_id AS table_id, alias FROM saved_query_tables WHERE saved_query_id = ? ORDER BY ordinal",
    )
    .bind(&saved_query.id)
    .fetch_all(pool)
    .await?;
    if bindings.is_empty() {
        return Err(AppError::BadRequest(
            "保存的查询没有可执行表绑定".to_owned(),
        ));
    }
    let mismatches: i64 = sqlx::query_scalar(
        r#"
        SELECT COUNT(*)
        FROM saved_query_tables qt
        JOIN source_tables t ON t.id = qt.source_table_id
        WHERE qt.saved_query_id = ? AND t.source_id <> ?
        "#,
    )
    .bind(&saved_query.id)
    .bind(source_id)
    .fetch_one(pool)
    .await?;
    if mismatches > 0 || saved_query.source_id != source_id {
        return Err(AppError::BadRequest(
            "本地自动化首版只支持查询同一物理文件中的逻辑表".to_owned(),
        ));
    }
    Ok(())
}

async fn validate_bindings_belong_to_source(
    connection: &mut SqliteConnection,
    bindings: &[QueryTableBinding],
    source_id: &str,
) -> AppResult<()> {
    if bindings.is_empty() {
        return Err(AppError::BadRequest(
            "保存的查询没有可执行表绑定".to_owned(),
        ));
    }
    for binding in bindings {
        let owner: Option<String> =
            sqlx::query_scalar("SELECT source_id FROM source_tables WHERE id = ?")
                .bind(&binding.table_id)
                .fetch_optional(&mut *connection)
                .await?;
        if owner.as_deref() != Some(source_id) {
            return Err(AppError::Conflict(
                "保存的查询绑定在刷新期间发生变化，请重新采集".to_owned(),
            ));
        }
    }
    Ok(())
}

async fn find_receipt(
    pool: &SqlitePool,
    source_id: &str,
    idempotency_key: &str,
) -> AppResult<Option<RefreshReceiptRow>> {
    Ok(sqlx::query_as::<_, RefreshReceiptRow>(
        r#"
        SELECT id, content_sha256, saved_query_id, saved_query_updated_at,
               revision_id, job_id, unchanged
        FROM source_refresh_runs
        WHERE source_id = ? AND idempotency_key = ?
        "#,
    )
    .bind(source_id)
    .bind(idempotency_key)
    .fetch_optional(pool)
    .await?)
}

async fn find_receipt_on_connection(
    connection: &mut SqliteConnection,
    source_id: &str,
    idempotency_key: &str,
) -> AppResult<Option<RefreshReceiptRow>> {
    Ok(sqlx::query_as::<_, RefreshReceiptRow>(
        r#"
        SELECT id, content_sha256, saved_query_id, saved_query_updated_at,
               revision_id, job_id, unchanged
        FROM source_refresh_runs
        WHERE source_id = ? AND idempotency_key = ?
        "#,
    )
    .bind(source_id)
    .bind(idempotency_key)
    .fetch_optional(&mut *connection)
    .await?)
}

fn validate_existing_receipt(
    existing: &RefreshReceiptRow,
    content_sha256: &str,
    saved_query: Option<&SavedQueryExecution>,
) -> AppResult<()> {
    let expected_query_id = saved_query.map(|query| query.id.as_str());
    let expected_updated_at = saved_query.map(|query| query.updated_at.as_str());
    if existing.content_sha256 != content_sha256
        || existing.saved_query_id.as_deref() != expected_query_id
        || existing.saved_query_updated_at.as_deref() != expected_updated_at
    {
        return Err(AppError::Conflict(
            "同一个幂等键不能用于不同的文件或查询".to_owned(),
        ));
    }
    Ok(())
}

fn validate_idempotency_key(value: &str) -> AppResult<()> {
    let value = value.trim();
    if value.is_empty() || value.len() > 128 {
        return Err(AppError::BadRequest(
            "Idempotency-Key 长度必须为 1 到 128 字节".to_owned(),
        ));
    }
    if !value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(AppError::BadRequest(
            "Idempotency-Key 包含不允许的字符".to_owned(),
        ));
    }
    Ok(())
}

fn validate_sha256(value: &str) -> AppResult<()> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(AppError::Internal("上传文件摘要无效".to_owned()));
    }
    Ok(())
}

async fn remove_staged_file(path: &Path) {
    if let Err(error) = tokio::fs::remove_file(path).await
        && error.kind() != std::io::ErrorKind::NotFound
    {
        tracing::warn!(?error, path = %path.display(), "failed to remove refresh staging file");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_bounded_idempotency_keys() {
        assert!(validate_idempotency_key("desktop:attempt-1").is_ok());
        assert!(validate_idempotency_key("").is_err());
        assert!(validate_idempotency_key("contains space").is_err());
        assert!(validate_idempotency_key(&"a".repeat(129)).is_err());
    }

    #[test]
    fn candidate_cache_guard_removes_unpublished_cache_files() {
        let directory = tempfile::tempdir().unwrap();
        let key = "a".repeat(64);
        let cache_path = directory.path().join(format!("{key}.duckdb"));
        std::fs::write(&cache_path, b"candidate").unwrap();
        {
            let mut guard = CandidateCacheGuard::new(
                directory.path().to_path_buf(),
                Arc::new(AtomicUsize::new(0)),
            );
            guard.track(key);
        }
        assert!(!cache_path.exists());
    }

    #[test]
    fn candidate_cache_guard_preserves_published_cache_files() {
        let directory = tempfile::tempdir().unwrap();
        let key = "b".repeat(64);
        let cache_path = directory.path().join(format!("{key}.duckdb"));
        std::fs::write(&cache_path, b"published").unwrap();
        {
            let mut guard = CandidateCacheGuard::new(
                directory.path().to_path_buf(),
                Arc::new(AtomicUsize::new(0)),
            );
            guard.track(key);
            guard.disarm();
        }
        assert!(cache_path.exists());
    }

    #[test]
    fn receipt_rejects_reused_key_for_changed_query() {
        let existing = RefreshReceiptRow {
            id: "r".to_owned(),
            content_sha256: "a".repeat(64),
            saved_query_id: Some("q".to_owned()),
            saved_query_updated_at: Some("v1".to_owned()),
            revision_id: Some("rev".to_owned()),
            job_id: Some("job".to_owned()),
            unchanged: false,
        };
        let query = SavedQueryExecution {
            id: "q".to_owned(),
            source_id: "s".to_owned(),
            name: "n".to_owned(),
            sql_text: "select 1".to_owned(),
            post_js: None,
            updated_at: "v2".to_owned(),
        };
        assert!(validate_existing_receipt(&existing, &"a".repeat(64), Some(&query)).is_err());
    }
}
