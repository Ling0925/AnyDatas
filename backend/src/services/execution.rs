use std::{collections::HashSet, path::PathBuf, time::Duration};

use uuid::Uuid;

use crate::{
    db,
    error::{AppError, AppResult},
    models::{QueryRequest, QueryResponse, QueryTableBinding, SharedState, SourceTableRow},
    services::{query_engine, resource_control},
};

/// 执行当前工作区的交互式查询，工作区参数确保每个绑定都经过租户权限校验。
pub async fn execute_request(
    state: SharedState,
    request: &QueryRequest,
    workspace_id: &str,
) -> AppResult<QueryResponse> {
    execute_request_inner(state, request, Some(workspace_id.to_owned()), None).await
}

/// 执行已入队任务；绑定在创建时已经校验并固化，因此工作线程只需按任务快照读取。
pub async fn execute_job_request(
    state: SharedState,
    request: &QueryRequest,
    job_id: String,
) -> AppResult<QueryResponse> {
    execute_request_inner(state, request, None, Some(job_id)).await
}

/// 解析新旧请求为统一逻辑表列表，再把阻塞的文件导入和 DuckDB 查询移到专用线程。
async fn execute_request_inner(
    state: SharedState,
    request: &QueryRequest,
    workspace_id: Option<String>,
    job_id: Option<String>,
) -> AppResult<QueryResponse> {
    let sources = resolve_query_sources(&state, request, workspace_id.as_deref()).await?;
    let sql = request.sql.clone();
    let limit = request.limit.unwrap_or(1_000).clamp(1, 5_000);
    let cache_root = state.data_dir.join("table-cache");
    let work_root = state.data_dir.join("query-work");
    let query_state = state.clone();
    let execution_id = job_id
        .clone()
        .unwrap_or_else(|| format!("interactive-{}", Uuid::new_v4()));
    let timeout_seconds = if job_id.is_some() {
        state.background_query_timeout_seconds
    } else {
        state.query_timeout_seconds
    };
    let query_execution_id = execution_id.clone();
    let permit = resource_control::acquire_permit(
        state.query_semaphore.clone(),
        state.resource_queue_timeout_seconds,
        "查询执行器",
    )
    .await?;

    let handle = tokio::task::spawn_blocking(move || {
        // 许可必须由真实查询线程持有，HTTP 超时返回后也不会错误释放并发名额。
        let _permit = permit;
        query_engine::execute_query(
            sources,
            &sql,
            limit,
            query_engine::QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &query_state.cache_build_locks,
                runtime: &query_state.query_runtime,
                execution_control: Some((&query_state.query_control, &query_execution_id)),
            },
        )
    });
    let result = match tokio::time::timeout(Duration::from_secs(timeout_seconds), handle).await {
        Ok(result) => {
            result.map_err(|error| AppError::Internal(format!("查询线程异常: {error}")))?
        }
        Err(_) => {
            cancel_execution(&state, &execution_id);
            let advice = if job_id.is_some() {
                "后台任务已发送中断信号"
            } else {
                "已发送中断信号；可改为后台任务执行"
            };
            return Err(AppError::Timeout(format!(
                "查询超过 {timeout_seconds} 秒，{advice}"
            )));
        }
    };

    let execution = result.map_err(|error| AppError::BadRequest(error.to_string()))?;
    for update in execution.cache_updates {
        let schema_json = serde_json::to_string(&update.columns)
            .map_err(|error| AppError::Internal(error.to_string()))?;
        sqlx::query(
            r#"
            UPDATE source_tables
            SET cache_key = ?, cache_status = 'ready', cache_error = NULL,
                schema_json = ?, row_count = ?, column_count = ?, updated_at = ?
            WHERE id = ? AND config_version = ?
            "#,
        )
        .bind(update.cache_key)
        .bind(schema_json)
        .bind(update.row_count as i64)
        .bind(update.columns.len() as i64)
        .bind(chrono::Utc::now().to_rfc3339())
        .bind(update.table_id)
        .bind(update.config_version)
        .execute(&state.pool)
        .await?;
    }
    Ok(execution.response)
}

/// 同时记录取消状态并中断已建立的 DuckDB 连接，覆盖缓存导入和 SQL 两个阶段。
fn cancel_execution(state: &SharedState, execution_id: &str) {
    let handle = state.query_control.lock().ok().and_then(|mut control| {
        control.canceled.insert(execution_id.to_owned());
        control.active.get(execution_id).cloned()
    });
    if let Some(handle) = handle {
        handle.interrupt();
    }
}

/// 将兼容的 sourceId 或新的 tables 绑定解析为执行源，并阻止重复及非法别名。
async fn resolve_query_sources(
    state: &SharedState,
    request: &QueryRequest,
    workspace_id: Option<&str>,
) -> AppResult<Vec<query_engine::QuerySource>> {
    let mut resolved = Vec::new();
    if request.tables.is_empty() {
        let source_id = request
            .source_id
            .as_deref()
            .ok_or_else(|| AppError::BadRequest("至少需要选择一张逻辑表".to_owned()))?;
        let mut table = db::get_default_source_table(&state.pool, source_id, workspace_id)
            .await?
            .ok_or_else(|| AppError::NotFound("数据文件的默认逻辑表不存在".to_owned()))?;
        if let Some(sheet) = &request.sheet {
            table.sheet_name.clone_from(sheet);
        }
        if let Some(start_cell) = &request.start_cell {
            table.start_cell = start_cell.to_ascii_uppercase();
        }
        if let Some(first_row_as_header) = request.first_row_as_header {
            table.first_row_as_header = first_row_as_header;
        }
        resolved.push(to_query_source(
            table,
            QueryTableBinding {
                table_id: String::new(),
                alias: "data".to_owned(),
            },
        ));
    } else {
        if request.tables.len() > 16 {
            return Err(AppError::BadRequest(
                "单次查询最多绑定 16 张逻辑表".to_owned(),
            ));
        }
        let mut aliases = HashSet::new();
        for binding in &request.tables {
            query_engine::validate_alias(&binding.alias)
                .map_err(|error| AppError::BadRequest(error.to_string()))?;
            if !aliases.insert(binding.alias.to_ascii_lowercase()) {
                return Err(AppError::BadRequest(format!(
                    "查询表别名不能重复: {}",
                    binding.alias
                )));
            }
            let table = db::get_source_table(&state.pool, &binding.table_id, workspace_id)
                .await?
                .ok_or_else(|| AppError::NotFound("绑定的逻辑表不存在".to_owned()))?;
            resolved.push(to_query_source(table, binding.clone()));
        }
    }
    Ok(resolved)
}

/// 把数据库模型转换为阻塞执行所需的拥有型结构，线程切换后无需持有数据库或请求引用。
fn to_query_source(
    table: SourceTableRow,
    mut binding: QueryTableBinding,
) -> query_engine::QuerySource {
    if binding.table_id.is_empty() {
        binding.table_id.clone_from(&table.id);
    }
    query_engine::QuerySource {
        table_id: table.id,
        config_version: table.config_version,
        path: PathBuf::from(table.stored_path),
        file_kind: table.file_kind,
        sheet: table.sheet_name,
        start_cell: table.start_cell,
        end_cell: table.end_cell,
        first_row_as_header: table.first_row_as_header,
        alias: binding.alias,
        columns: serde_json::from_str(&table.schema_json).unwrap_or_default(),
        row_count: table.row_count.max(0) as usize,
    }
}
