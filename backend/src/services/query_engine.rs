use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::Instant,
};

use anyhow::{Context, Result, bail};
use chrono::{DateTime, NaiveDate, NaiveDateTime};
use duckdb::{
    Connection, appender_params_from_iter,
    types::{TimeUnit, Value as DuckValue},
};
use regex::Regex;
use serde_json::{Number, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::{
    models::{CacheBuildLocks, FieldDefinition, QueryControl, QueryResponse, QueryRuntimeLimits},
    services::{maintenance, spreadsheet},
};

const SCHEMA_SAMPLE_ROWS: usize = 2_000;
const CANCEL_CHECK_INTERVAL: usize = 1_024;
/// DuckDB rejects zero-column tables; empty post-process results use this marker.
const EMPTY_RESULT_MARKER_COLUMN: &str = "__anydatas_empty";

#[derive(Debug, Clone)]
pub struct QuerySource {
    pub table_id: String,
    pub config_version: i64,
    pub path: PathBuf,
    pub file_kind: String,
    pub sheet: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
    pub alias: String,
    pub columns: Vec<FieldDefinition>,
    pub row_count: usize,
}

#[derive(Debug)]
pub struct QueryCacheUpdate {
    pub table_id: String,
    pub config_version: i64,
    pub cache_key: String,
    pub columns: Vec<FieldDefinition>,
    pub row_count: usize,
}

#[derive(Debug)]
pub struct QueryExecution {
    pub response: QueryResponse,
    pub cache_updates: Vec<QueryCacheUpdate>,
}

#[derive(Debug)]
pub struct QueryArtifactExecution {
    pub sample: QueryResponse,
    pub cache_updates: Vec<QueryCacheUpdate>,
    pub total_rows: usize,
    pub artifact_size_bytes: u64,
    /// Captured `console.*` lines from optional post-process JS (empty when unused).
    pub console: Vec<String>,
}

pub struct QueryExecutionContext<'a> {
    pub cache_root: &'a Path,
    pub work_root: &'a Path,
    pub cache_build_locks: &'a CacheBuildLocks,
    pub runtime: &'a QueryRuntimeLimits,
    pub execution_control: Option<(&'a std::sync::Mutex<QueryControl>, &'a str)>,
}

struct PreparedSource {
    alias: String,
    cache_path: PathBuf,
}

/// 执行只读多表查询；先准备不可变缓存再挂载别名，可让重复查询跳过 Excel/CSV 解析。
pub fn execute_query(
    sources: Vec<QuerySource>,
    sql: &str,
    limit: usize,
    context: QueryExecutionContext<'_>,
) -> Result<QueryExecution> {
    validate_read_only_sql(sql)?;
    validate_sources(&sources)?;
    let active_query = context
        .execution_control
        .map(|(control, execution_id)| ActiveQueryGuard::register(control, execution_id))
        .transpose()?;
    if let Some(query) = &active_query {
        query.ensure_running()?;
    }
    let started = Instant::now();
    let mut prepared_sources = Vec::with_capacity(sources.len());
    let mut cache_updates = Vec::with_capacity(sources.len());
    for source in &sources {
        ensure_query_running(context.execution_control)?;
        let (prepared, update) = prepare_source_cache(
            source,
            context.cache_root,
            context.cache_build_locks,
            context.runtime,
            context.execution_control,
        )
        .with_context(|| format!("逻辑表 {} 缓存构建失败", source.alias))?;
        prepared_sources.push(prepared);
        cache_updates.push(update);
    }

    let workspace = QueryWorkspace::create(context.work_root)?;
    let connection = Connection::open(workspace.database_path()).context("无法初始化 DuckDB")?;
    configure_connection(&connection, context.runtime, Some(&workspace.temp_path()))?;
    attach_cached_sources(&connection, &prepared_sources)?;
    connection.execute_batch("SET enable_external_access = false;")?;
    if let Some(query) = &active_query {
        query.attach(&connection)?;
        query.ensure_running()?;
    }
    let clean_sql = sql.trim().trim_end_matches(';').trim();
    let query_sql = format!(
        "SELECT * FROM ({clean_sql}) AS __anydatas_result LIMIT {}",
        limit.saturating_add(1)
    );
    let response = collect_query_response(&connection, &query_sql, limit, started)?;

    Ok(QueryExecution {
        response,
        cache_updates,
    })
}

/// 将后台查询的完整结果物化为独立 DuckDB 文件，同时只返回有界样本供任务详情展示。
///
/// 结果文件先写入同一目录的临时路径并在 CHECKPOINT 后原子改名，进程中断不会产生
/// 看似成功的半成品；SQLite 因此只承担任务元数据，不再承载大量结果 JSON。
pub fn execute_query_to_artifact(
    sources: Vec<QuerySource>,
    sql: &str,
    sample_limit: usize,
    artifact_path: &Path,
    context: QueryExecutionContext<'_>,
) -> Result<QueryArtifactExecution> {
    validate_read_only_sql(sql)?;
    validate_sources(&sources)?;
    let active_query = context
        .execution_control
        .map(|(control, execution_id)| ActiveQueryGuard::register(control, execution_id))
        .transpose()?;
    if let Some(query) = &active_query {
        query.ensure_running()?;
    }
    let started = Instant::now();
    let mut prepared_sources = Vec::with_capacity(sources.len());
    let mut cache_updates = Vec::with_capacity(sources.len());
    for source in &sources {
        ensure_query_running(context.execution_control)?;
        let (prepared, update) = prepare_source_cache(
            source,
            context.cache_root,
            context.cache_build_locks,
            context.runtime,
            context.execution_control,
        )
        .with_context(|| format!("逻辑表 {} 缓存构建失败", source.alias))?;
        prepared_sources.push(prepared);
        cache_updates.push(update);
    }

    let parent = artifact_path.parent().context("后台结果路径缺少父目录")?;
    fs::create_dir_all(parent)?;
    maintenance::ensure_free_space(parent, context.runtime.min_free_space_bytes, 0)?;
    let artifact_id = Uuid::new_v4();
    let temporary_path = artifact_path.with_extension(format!("{artifact_id}.tmp"));
    let temporary_directory = parent.join(format!(".result-temp-{artifact_id}"));
    let result = (|| -> Result<(QueryResponse, usize)> {
        let connection = Connection::open(&temporary_path).context("无法初始化后台结果数据库")?;
        configure_connection(&connection, context.runtime, Some(&temporary_directory))?;
        attach_cached_sources(&connection, &prepared_sources)?;
        connection.execute_batch("SET enable_external_access = false;")?;
        if let Some(query) = &active_query {
            query.attach(&connection)?;
            query.ensure_running()?;
        }
        let clean_sql = sql.trim().trim_end_matches(';').trim();
        connection
            .execute_batch(&format!(
                "CREATE TABLE {} AS SELECT * FROM ({clean_sql}) AS __anydatas_result;",
                quote_identifier("result")
            ))
            .map_err(|error| anyhow::anyhow!("SQL 执行失败: {error}"))?;
        let total_rows: i64 = connection.query_row(
            &format!("SELECT COUNT(*) FROM {}", quote_identifier("result")),
            [],
            |row| row.get(0),
        )?;
        let total_rows = usize::try_from(total_rows).context("结果行数超出平台范围")?;
        let query_sql = format!(
            "SELECT * FROM {} LIMIT {}",
            quote_identifier("result"),
            sample_limit.saturating_add(1)
        );
        let mut sample = collect_query_response(&connection, &query_sql, sample_limit, started)?;
        sample.truncated = total_rows > sample.rows.len();
        connection.execute_batch("CHECKPOINT;")?;
        drop(connection);
        Ok((sample, total_rows))
    })();
    let _ = fs::remove_dir_all(&temporary_directory);
    match result {
        Ok((sample, total_rows)) => {
            let temporary_size = fs::metadata(&temporary_path)?.len();
            if temporary_size > context.runtime.max_artifact_bytes {
                let _ = fs::remove_file(&temporary_path);
                bail!(
                    "后台结果产物大小 {:.2} MB 超过单任务上限 {:.2} MB",
                    temporary_size as f64 / 1024.0 / 1024.0,
                    context.runtime.max_artifact_bytes as f64 / 1024.0 / 1024.0
                );
            }
            if artifact_path.exists() {
                fs::remove_file(artifact_path)?;
            }
            fs::rename(&temporary_path, artifact_path)?;
            let artifact_size_bytes = fs::metadata(artifact_path)?.len();
            if let Err(error) =
                maintenance::ensure_free_space(parent, context.runtime.min_free_space_bytes, 0)
            {
                let _ = fs::remove_file(artifact_path);
                return Err(error.context("后台结果已删除"));
            }
            Ok(QueryArtifactExecution {
                sample,
                cache_updates,
                total_rows,
                artifact_size_bytes,
                console: Vec::new(),
            })
        }
        Err(error) => {
            let _ = fs::remove_file(&temporary_path);
            Err(error)
        }
    }
}

/// 从持久化结果文件读取一页数据；分页参数由服务端生成为整数，不拼接用户 SQL。
pub fn read_artifact_page(
    artifact_path: &Path,
    offset: usize,
    limit: usize,
    runtime: &QueryRuntimeLimits,
    work_root: &Path,
) -> Result<(QueryResponse, usize)> {
    let started = Instant::now();
    let workspace = QueryWorkspace::create(work_root)?;
    let connection = Connection::open(artifact_path).context("无法打开后台结果")?;
    configure_connection(&connection, runtime, Some(&workspace.temp_path()))?;
    connection.execute_batch("SET enable_external_access = false;")?;
    let total_rows: i64 = connection.query_row(
        &format!("SELECT COUNT(*) FROM {}", quote_identifier("result")),
        [],
        |row| row.get(0),
    )?;
    let total_rows = usize::try_from(total_rows).context("结果行数超出平台范围")?;
    let query_sql = format!(
        "SELECT * FROM {} LIMIT {} OFFSET {}",
        quote_identifier("result"),
        limit.saturating_add(1),
        offset
    );
    let mut response = collect_query_response(&connection, &query_sql, limit, started)?;
    response = normalize_empty_marker_response(response);
    let total_rows = if response.columns.is_empty() && response.rows.is_empty() {
        0
    } else {
        total_rows
    };
    response.truncated = offset.saturating_add(response.rows.len()) < total_rows;
    Ok((response, total_rows))
}

/// Read every row from a persisted artifact for post-process input.
///
/// Fails when `total_rows > max_rows` so callers can map the error to
/// `post_js_limit_input_rows` without loading an oversized result set.
pub fn read_artifact_all_rows(
    artifact_path: &Path,
    max_rows: usize,
    runtime: &QueryRuntimeLimits,
    work_root: &Path,
) -> Result<(Vec<FieldDefinition>, Vec<Vec<Value>>, usize)> {
    let started = Instant::now();
    let workspace = QueryWorkspace::create(work_root)?;
    let connection = Connection::open(artifact_path).context("无法打开后台结果")?;
    configure_connection(&connection, runtime, Some(&workspace.temp_path()))?;
    connection.execute_batch("SET enable_external_access = false;")?;
    let total_rows: i64 = connection.query_row(
        &format!("SELECT COUNT(*) FROM {}", quote_identifier("result")),
        [],
        |row| row.get(0),
    )?;
    let total_rows = usize::try_from(total_rows).context("结果行数超出平台范围")?;
    if total_rows > max_rows {
        bail!("后处理输入行数超过限制（{} > {}）", total_rows, max_rows);
    }
    let query_sql = format!("SELECT * FROM {}", quote_identifier("result"));
    // limit == total_rows keeps collect from treating a full read as truncated.
    let response = collect_query_response(&connection, &query_sql, total_rows.max(1), started)?;
    let response = normalize_empty_marker_response(response);
    let total_rows = if response.columns.is_empty() && response.rows.is_empty() {
        0
    } else {
        total_rows
    };
    Ok((response.columns, response.rows, total_rows))
}

/// Replace the artifact `result` table with post-process output columns/rows.
///
/// Writes via a temp file + atomic rename so a failed rewrite never leaves a
/// half-applied post-process table in place of the SQL result.
pub fn replace_artifact_with_rows(
    artifact_path: &Path,
    columns: &[FieldDefinition],
    rows: &[Vec<Value>],
    runtime: &QueryRuntimeLimits,
) -> Result<u64> {
    let parent = artifact_path.parent().context("后台结果路径缺少父目录")?;
    fs::create_dir_all(parent)?;
    maintenance::ensure_free_space(parent, runtime.min_free_space_bytes, 0)?;
    let artifact_id = Uuid::new_v4();
    let temporary_path = artifact_path.with_extension(format!("{artifact_id}.rewrite.tmp"));
    let temporary_directory = parent.join(format!(".result-rewrite-{artifact_id}"));
    let result = (|| -> Result<()> {
        let connection = Connection::open(&temporary_path).context("无法初始化后处理结果数据库")?;
        configure_connection(&connection, runtime, Some(&temporary_directory))?;
        connection.execute_batch("SET enable_external_access = false;")?;
        // DuckDB requires ≥1 column; empty process() results use an internal marker.
        let write_columns = if columns.is_empty() {
            vec![FieldDefinition {
                name: EMPTY_RESULT_MARKER_COLUMN.to_owned(),
                data_type: "布尔".to_owned(),
                nullable: true,
            }]
        } else {
            columns.to_vec()
        };
        create_named_table(&connection, "result", &write_columns)?;
        if !columns.is_empty() {
            let mut appender = connection.appender("result")?;
            for (row_index, row) in rows.iter().enumerate() {
                let values = columns
                    .iter()
                    .enumerate()
                    .map(|(index, column)| {
                        let value = row.get(index).unwrap_or(&Value::Null);
                        json_to_duck(value, &column.data_type).with_context(|| {
                            format!(
                                "后处理结果第 {} 行字段“{}”无法写入产物",
                                row_index + 1,
                                column.name
                            )
                        })
                    })
                    .collect::<Result<Vec<_>>>()?;
                appender.append_row(appender_params_from_iter(values))?;
            }
            appender.flush()?;
            drop(appender);
        }
        connection.execute_batch("CHECKPOINT;")?;
        drop(connection);
        Ok(())
    })();
    let _ = fs::remove_dir_all(&temporary_directory);
    match result {
        Ok(()) => {
            let temporary_size = fs::metadata(&temporary_path)?.len();
            if temporary_size > runtime.max_artifact_bytes {
                let _ = fs::remove_file(&temporary_path);
                bail!(
                    "后台结果产物大小 {:.2} MB 超过单任务上限 {:.2} MB",
                    temporary_size as f64 / 1024.0 / 1024.0,
                    runtime.max_artifact_bytes as f64 / 1024.0 / 1024.0
                );
            }
            if artifact_path.exists() {
                fs::remove_file(artifact_path)?;
            }
            fs::rename(&temporary_path, artifact_path)?;
            let artifact_size_bytes = fs::metadata(artifact_path)?.len();
            if let Err(error) =
                maintenance::ensure_free_space(parent, runtime.min_free_space_bytes, 0)
            {
                let _ = fs::remove_file(artifact_path);
                return Err(error.context("后台结果已删除"));
            }
            Ok(artifact_size_bytes)
        }
        Err(error) => {
            let _ = fs::remove_file(&temporary_path);
            Err(error)
        }
    }
}

/// Hide the internal empty-result marker column from API consumers.
fn normalize_empty_marker_response(mut response: QueryResponse) -> QueryResponse {
    if response.columns.len() == 1
        && response.columns[0].name == EMPTY_RESULT_MARKER_COLUMN
        && response.rows.is_empty()
    {
        response.columns.clear();
        response.row_count = 0;
    }
    response
}

/// 将完整后台结果逐行写为 CSV，调用方可以把 Writer 接到 HTTP 流而无需中间大文件。
///
/// 文本型公式前缀会添加单引号，避免用户在 Excel 中打开下载文件时触发 CSV 公式；
/// 数值列保持原值，分析结果不会因安全转义改变数值语义。
pub fn write_artifact_csv(
    artifact_path: &Path,
    runtime: &QueryRuntimeLimits,
    output: impl Write,
) -> Result<()> {
    let connection = Connection::open(artifact_path).context("无法打开后台结果")?;
    configure_connection(&connection, runtime, None)?;
    connection.execute_batch("SET enable_external_access = false;")?;
    let mut statement =
        connection.prepare(&format!("SELECT * FROM {}", quote_identifier("result")))?;
    let mut rows = statement.query([])?;
    let names = rows
        .as_ref()
        .context("DuckDB 未返回结果结构")?
        .column_names();
    let mut writer = csv::WriterBuilder::new().from_writer(output);
    if names.len() == 1 && names[0] == EMPTY_RESULT_MARKER_COLUMN {
        // Empty post-process result: emit a headerless empty CSV.
        writer.flush()?;
        return Ok(());
    }
    // 结果列名来自上传文件的表头，同样可能以公式字符开头（如 =HYPERLINK(...)），必须与数据值
    // 一样中和，否则他人下载 CSV 后在 Excel 打开表头即被当作公式执行。
    let header = names
        .iter()
        .map(|name| neutralize_csv_formula(name))
        .collect::<Vec<_>>();
    writer.write_record(&header)?;
    while let Some(row) = rows.next()? {
        let record = (0..names.len())
            .map(|index| {
                row.get_ref(index)
                    .map(|value| duck_value_to_csv(value.to_owned()))
            })
            .collect::<duckdb::Result<Vec<_>>>()?;
        writer.write_record(record)?;
    }
    writer.flush()?;
    Ok(())
}

/// 把 DuckDB 行集转换为有界 JSON 响应，交互查询、后台样本和分页共用同一类型语义。
fn collect_query_response(
    connection: &Connection,
    query_sql: &str,
    limit: usize,
    started: Instant,
) -> Result<QueryResponse> {
    let mut statement = connection
        .prepare(query_sql)
        .map_err(|error| anyhow::anyhow!("SQL 编译失败: {error}"))?;
    let mut rows = statement
        .query([])
        .map_err(|error| anyhow::anyhow!("SQL 执行失败: {error}"))?;
    let result_schema = rows.as_ref().context("DuckDB 未返回结果结构")?;
    let names = result_schema.column_names();
    let result_types = (0..names.len())
        .map(|index| {
            let data_type = result_schema.column_type(index);
            duck_type_label((&data_type).into())
        })
        .collect::<Vec<_>>();
    let mut result_rows = Vec::new();
    while let Some(row) = rows.next()? {
        let mut values = Vec::with_capacity(names.len());
        for index in 0..names.len() {
            let value = row.get_ref(index)?;
            values.push(duck_value_to_json(value.to_owned()));
        }
        result_rows.push(values);
    }
    let truncated = result_rows.len() > limit;
    result_rows.truncate(limit);
    let columns = names
        .into_iter()
        .enumerate()
        .map(|(index, name)| FieldDefinition {
            name,
            data_type: result_types[index].clone(),
            nullable: true,
        })
        .collect();

    Ok(QueryResponse {
        columns,
        row_count: result_rows.len(),
        rows: result_rows,
        elapsed_ms: started.elapsed().as_millis(),
        truncated,
        post_processed: false,
        post_process_ms: None,
    })
}

/// 校验绑定数量和别名，提前阻止重复表名或保留名称进入 DuckDB 语句。
fn validate_sources(sources: &[QuerySource]) -> Result<()> {
    if sources.is_empty() {
        bail!("至少需要绑定一张逻辑表");
    }
    if sources.len() > 16 {
        bail!("单次查询最多绑定 16 张逻辑表");
    }
    let mut aliases = HashSet::new();
    for source in sources {
        validate_alias(&source.alias)?;
        if !aliases.insert(source.alias.to_ascii_lowercase()) {
            bail!("查询表别名不能重复: {}", source.alias);
        }
    }
    Ok(())
}

/// 校验 SQL 表别名，仅允许稳定的标识符，既减少转义风险也方便编辑器补全。
pub fn validate_alias(alias: &str) -> Result<()> {
    let bytes = alias.as_bytes();
    if bytes.is_empty() || bytes.len() > 63 {
        bail!("表别名长度必须为 1 到 63 个字符");
    }
    if !(bytes[0].is_ascii_alphabetic() || bytes[0] == b'_')
        || !bytes[1..]
            .iter()
            .all(|value| value.is_ascii_alphanumeric() || *value == b'_')
    {
        bail!("表别名只能包含字母、数字和下划线，且不能以数字开头");
    }
    if matches!(
        alias.to_ascii_lowercase().as_str(),
        "main" | "temp" | "information_schema" | "pg_catalog" | "__anydatas_result"
    ) {
        bail!("该表别名为系统保留名称");
    }
    Ok(())
}

/// 计算配置内容的稳定缓存键；同一逻辑表更新版本后自然生成新文件，旧查询不会读到脏缓存。
///
/// 每个变长字段前置其字节长度做域分隔：否则无分隔拼接会让 (sheet="AB", start="C1") 与
/// (sheet="A", start="BC1") 得到相同哈希，第二次查询命中第一次的缓存并静默返回错误区间数据。
fn source_cache_key(source: &QuerySource) -> String {
    let mut digest = Sha256::new();
    for field in [
        source.table_id.as_bytes(),
        source.sheet.as_bytes(),
        source.start_cell.as_bytes(),
        source.end_cell.as_deref().unwrap_or("").as_bytes(),
    ] {
        digest.update((field.len() as u64).to_le_bytes());
        digest.update(field);
    }
    digest.update(source.config_version.to_le_bytes());
    digest.update([u8::from(source.first_row_as_header)]);
    hex::encode(digest.finalize())
}

/// 在全局构建锁内检查并生成单表缓存，避免多个请求同时重复导入同一大文件。
fn prepare_source_cache(
    source: &QuerySource,
    cache_root: &Path,
    cache_build_locks: &CacheBuildLocks,
    runtime: &QueryRuntimeLimits,
    execution_control: Option<(&std::sync::Mutex<QueryControl>, &str)>,
) -> Result<(PreparedSource, QueryCacheUpdate)> {
    fs::create_dir_all(cache_root)
        .with_context(|| format!("无法创建表缓存目录 {}", cache_root.display()))?;
    let cache_key = source_cache_key(source);
    let cache_path = cache_root.join(format!("{cache_key}.duckdb"));
    let mut columns = source.columns.clone();
    let mut row_count = source.row_count;
    {
        let cache_lock = cache_build_locks
            .lock_for(&cache_key)
            .map_err(anyhow::Error::msg)?;
        let _guard = cache_lock
            .lock()
            .map_err(|_| anyhow::anyhow!("表缓存构建器不可用"))?;
        if !cache_path.exists() || columns.is_empty() {
            if cache_path.exists() {
                fs::remove_file(&cache_path)?;
            }
            let metadata = build_source_cache(source, &cache_path, runtime, execution_control)?;
            columns = metadata.0;
            row_count = metadata.1;
        }
    }
    Ok((
        PreparedSource {
            alias: source.alias.clone(),
            cache_path,
        },
        QueryCacheUpdate {
            table_id: source.table_id.clone(),
            config_version: source.config_version,
            cache_key,
            columns,
            row_count,
        },
    ))
}

/// 将一个逻辑表流式导入临时 DuckDB 后原子改名，进程中断时不会留下半成品缓存。
fn build_source_cache(
    source: &QuerySource,
    cache_path: &Path,
    runtime: &QueryRuntimeLimits,
    execution_control: Option<(&std::sync::Mutex<QueryControl>, &str)>,
) -> Result<(Vec<FieldDefinition>, usize)> {
    maintenance::ensure_free_space(
        cache_path.parent().unwrap_or_else(|| Path::new(".")),
        runtime.min_free_space_bytes,
        0,
    )?;
    let sample = spreadsheet::read_table_range(
        &source.path,
        &source.file_kind,
        &source.sheet,
        &source.start_cell,
        source.end_cell.as_deref(),
        source.first_row_as_header,
        Some(SCHEMA_SAMPLE_ROWS),
    )?;
    if sample.columns.is_empty() {
        bail!("逻辑表 {} 的读取范围没有可查询字段", source.alias);
    }
    let columns = spreadsheet::apply_field_overrides(
        &sample.columns,
        (!source.columns.is_empty()).then_some(source.columns.as_slice()),
    )?;
    let temporary_path = cache_path.with_extension(format!("{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<usize> {
        let connection = Connection::open(&temporary_path).context("无法初始化表缓存")?;
        configure_connection(&connection, runtime, None)?;
        connection.execute_batch("SET enable_external_access = false;")?;
        create_cache_table(&connection, &columns)?;
        let mut appender = connection.appender("cached_data")?;
        let mut imported_rows = 0usize;
        spreadsheet::stream_table_rows_range(
            &source.path,
            &source.file_kind,
            spreadsheet::TableRangeOptions {
                sheet: &source.sheet,
                start_cell: &source.start_cell,
                end_cell: source.end_cell.as_deref(),
                first_row_as_header: source.first_row_as_header,
            },
            columns.len(),
            |row| {
                if imported_rows.is_multiple_of(CANCEL_CHECK_INTERVAL) {
                    ensure_query_running(execution_control)?;
                }
                let values = row
                    .iter()
                    .zip(&columns)
                    .map(|(value, column)| {
                        json_to_duck(value, &column.data_type).with_context(|| {
                            format!(
                                "第 {} 条数据的字段“{}”无法转换为{}，原始值为 {}",
                                imported_rows + 1,
                                column.name,
                                column.data_type,
                                display_value(value)
                            )
                        })
                    })
                    .collect::<Result<Vec<_>>>()?;
                appender.append_row(appender_params_from_iter(values))?;
                imported_rows += 1;
                Ok(())
            },
        )?;
        appender.flush()?;
        drop(appender);
        connection.execute_batch("CHECKPOINT;")?;
        drop(connection);
        Ok(imported_rows)
    })();
    match result {
        Ok(imported_rows) => {
            fs::rename(&temporary_path, cache_path)?;
            Ok((columns, imported_rows))
        }
        Err(error) => {
            let _ = fs::remove_file(&temporary_path);
            Err(error)
        }
    }
}

/// 挂载去重后的缓存数据库并为每个绑定创建临时视图，支持同表多别名自连接。
fn attach_cached_sources(connection: &Connection, sources: &[PreparedSource]) -> Result<()> {
    let mut attached = HashMap::<PathBuf, String>::new();
    for source in sources {
        let schema = if let Some(schema) = attached.get(&source.cache_path) {
            schema.clone()
        } else {
            let schema = format!("__cache_{}", attached.len());
            connection.execute_batch(&format!(
                "ATTACH {} AS {} (READ_ONLY);",
                quote_string_literal(&source.cache_path.to_string_lossy()),
                quote_identifier(&schema)
            ))?;
            attached.insert(source.cache_path.clone(), schema.clone());
            schema
        };
        connection.execute_batch(&format!(
            "CREATE TEMP VIEW {} AS SELECT * FROM {}.{};",
            quote_identifier(&source.alias),
            quote_identifier(&schema),
            quote_identifier("cached_data")
        ))?;
    }
    Ok(())
}

/// 检查查询取消标记，缓存导入期间也能及时停止而不必等待 SQL 阶段。
fn ensure_query_running(
    execution_control: Option<(&std::sync::Mutex<QueryControl>, &str)>,
) -> Result<()> {
    if let Some((control, execution_id)) = execution_control {
        let queries = control
            .lock()
            .map_err(|_| anyhow::anyhow!("任务控制器不可用"))?;
        if queries.canceled.contains(execution_id) {
            bail!("任务已取消");
        }
    }
    Ok(())
}

struct ActiveQueryGuard<'a> {
    control: &'a std::sync::Mutex<QueryControl>,
    job_id: String,
}

impl<'a> ActiveQueryGuard<'a> {
    /// 在读取源文件前注册执行 id，超时发生在缓存构建阶段时也能传播取消信号。
    fn register(control: &'a std::sync::Mutex<QueryControl>, execution_id: &str) -> Result<Self> {
        let queries = control
            .lock()
            .map_err(|_| anyhow::anyhow!("任务控制器不可用"))?;
        if queries.canceled.contains(execution_id) {
            bail!("任务已取消");
        }
        Ok(Self {
            control,
            job_id: execution_id.to_owned(),
        })
    }

    /// DuckDB 连接建立后注册中断句柄，使 API 超时和用户取消能立即停止正在执行的 SQL。
    fn attach(&self, connection: &Connection) -> Result<()> {
        let mut queries = self
            .control
            .lock()
            .map_err(|_| anyhow::anyhow!("任务控制器不可用"))?;
        queries
            .active
            .insert(self.job_id.clone(), connection.interrupt_handle());
        Ok(())
    }

    fn ensure_running(&self) -> Result<()> {
        let queries = self
            .control
            .lock()
            .map_err(|_| anyhow::anyhow!("任务控制器不可用"))?;
        if queries.canceled.contains(&self.job_id) {
            bail!("任务已取消");
        }
        Ok(())
    }
}

impl Drop for ActiveQueryGuard<'_> {
    fn drop(&mut self) {
        if let Ok(mut queries) = self.control.lock() {
            queries.active.remove(&self.job_id);
            queries.canceled.remove(&self.job_id);
        }
    }
}

pub fn validate_read_only_sql(sql: &str) -> Result<()> {
    let clean = sql.trim().trim_end_matches(';').trim();
    let lowered = clean.to_ascii_lowercase();
    if !(lowered.starts_with("select") || lowered.starts_with("with")) {
        bail!("仅允许 SELECT 或 WITH 查询");
    }
    // 关键字黑名单与分号检查只作用于剥离了字符串字面量、引用标识符和注释后的“结构性 SQL”，
    // 避免把出现在数据值或列名里的英文单词（'update'、'load'、'set' 等）误判为文件/DDL 操作。
    // 真正的隔离由引擎层 enable_external_access=false + 只读 ATTACH 保证，黑名单只是二次防御。
    let structural = strip_sql_literals_and_comments(clean);
    if structural.contains(';') {
        bail!("一次只能执行一条查询");
    }
    let forbidden = Regex::new(
        r"(?i)\b(attach|copy|install|load|call|pragma|create|insert|update|delete|drop|alter|export|import|set|read_csv|read_csv_auto|read_parquet|read_json|read_ndjson|glob)\b",
    )?;
    if forbidden.is_match(&structural) {
        bail!("查询包含不允许的文件或数据库操作");
    }
    Ok(())
}

/// 去除单引号字符串字面量、双引号标识符与 SQL 注释，仅保留可安全应用关键字黑名单的结构骨架。
/// 引号内的 `''` / `""` 转义会被正确跳过；结果只用于校验，不会被执行。
fn strip_sql_literals_and_comments(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len());
    let mut chars = sql.chars().peekable();
    while let Some(current) = chars.next() {
        match current {
            '\'' | '"' => {
                out.push(' ');
                while let Some(inner) = chars.next() {
                    if inner == current {
                        if chars.peek() == Some(&current) {
                            chars.next();
                            continue;
                        }
                        break;
                    }
                }
            }
            '-' if chars.peek() == Some(&'-') => {
                for inner in chars.by_ref() {
                    if inner == '\n' {
                        out.push('\n');
                        break;
                    }
                }
            }
            '/' if chars.peek() == Some(&'*') => {
                chars.next();
                let mut previous = '\0';
                for inner in chars.by_ref() {
                    if previous == '*' && inner == '/' {
                        break;
                    }
                    previous = inner;
                }
                out.push(' ');
            }
            other => out.push(other),
        }
    }
    out
}

struct QueryWorkspace {
    path: PathBuf,
}

impl QueryWorkspace {
    fn create(root: &Path) -> Result<Self> {
        fs::create_dir_all(root)
            .with_context(|| format!("无法创建查询工作目录 {}", root.display()))?;
        let path = root.join(format!("query-{}", Uuid::new_v4()));
        fs::create_dir(&path)
            .with_context(|| format!("无法创建查询临时目录 {}", path.display()))?;
        Ok(Self { path })
    }

    fn database_path(&self) -> PathBuf {
        self.path.join("query.duckdb")
    }

    fn temp_path(&self) -> PathBuf {
        self.path.join("temp")
    }
}

impl Drop for QueryWorkspace {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.path) {
            tracing::warn!(?error, path = %self.path.display(), "failed to remove query workspace");
        }
    }
}

/// 为每个 DuckDB 连接应用同一组单机资源边界，并关闭自动扩展下载。
///
/// 内存、线程和临时盘限制都由服务端生成，用户 SQL 无法通过 SET 覆盖这些值；
/// 查询超出预算时会明确失败，而不会把整台服务器拖入交换或磁盘占满状态。
fn configure_connection(
    connection: &Connection,
    runtime: &QueryRuntimeLimits,
    temp_directory: Option<&Path>,
) -> Result<()> {
    let mut statements = format!(
        "SET memory_limit = '{}MB';\
         SET threads = {};\
         SET max_temp_directory_size = '{}MB';\
         SET autoinstall_known_extensions = false;\
         SET autoload_known_extensions = false;",
        runtime.memory_limit_mb, runtime.threads, runtime.temp_limit_mb
    );
    if let Some(temp_directory) = temp_directory {
        fs::create_dir_all(temp_directory)
            .with_context(|| format!("无法创建 DuckDB 临时目录 {}", temp_directory.display()))?;
        statements.push_str(&format!(
            "SET temp_directory = {};",
            quote_string_literal(&temp_directory.to_string_lossy())
        ));
    }
    connection.execute_batch(&statements)?;
    Ok(())
}

/// 创建缓存数据表并根据采样类型选择 DuckDB 列类型，兼顾聚合性能与原始值兼容性。
fn create_cache_table(connection: &Connection, columns: &[FieldDefinition]) -> Result<()> {
    create_named_table(connection, "cached_data", columns)
}

/// Create a DuckDB table with the given name and field definitions.
///
/// Empty column lists produce a zero-column table so post-process `return []`
/// can still materialize a valid artifact.
fn create_named_table(
    connection: &Connection,
    table_name: &str,
    columns: &[FieldDefinition],
) -> Result<()> {
    let definitions = if columns.is_empty() {
        String::new()
    } else {
        columns
            .iter()
            .map(|column| {
                format!(
                    "{} {}",
                    quote_identifier(&column.name),
                    duck_column_type(&column.data_type)
                )
            })
            .collect::<Vec<_>>()
            .join(", ")
    };
    connection.execute_batch(&format!(
        "CREATE TABLE {} ({definitions});",
        quote_identifier(table_name)
    ))?;
    Ok(())
}

/// 安全引用 DuckDB 标识符，字段名和用户别名即使含特殊字符也不会改变 SQL 结构。
fn quote_identifier(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

/// 安全引用服务器生成的文件路径，ATTACH 语句不会因单引号路径而截断。
fn quote_string_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn duck_column_type(data_type: &str) -> &'static str {
    match data_type {
        "整数" => "BIGINT",
        "小数" => "DOUBLE",
        "布尔" => "BOOLEAN",
        "日期" => "DATE",
        "日期时间" => "TIMESTAMP",
        _ => "VARCHAR",
    }
}

/// 将原始值转换为用户确认的字段类型，失败必须上抛而不能静默替换为 NULL。
///
/// 明确失败可以防止聚合结果在不知情的情况下少算数据，并让用户回到导入配置把
/// 混合列改为文本类型。
fn json_to_duck(value: &Value, data_type: &str) -> Result<DuckValue> {
    if value.is_null() {
        return Ok(DuckValue::Null);
    }
    let converted = match data_type {
        "整数" => integer_value(value)
            .map(DuckValue::BigInt)
            .context("不是有效整数")?,
        "小数" => decimal_value(value)
            .map(DuckValue::Double)
            .context("不是有效小数")?,
        "布尔" => boolean_value(value)
            .map(DuckValue::Boolean)
            .context("不是有效布尔值")?,
        "日期" => date_value(value)
            .map(DuckValue::Date32)
            .context("不是有效日期")?,
        "日期时间" => datetime_value(value)
            .map(|value| DuckValue::Timestamp(TimeUnit::Microsecond, value))
            .context("不是有效日期时间")?,
        _ => value
            .as_str()
            .map(str::to_owned)
            .map(DuckValue::Text)
            .unwrap_or_else(|| DuckValue::Text(value.to_string())),
    };
    Ok(converted)
}

/// 限制错误消息中的原始值长度，保留定位信息同时避免异常单元格撑大响应和日志。
fn display_value(value: &Value) -> String {
    let value = match value {
        Value::String(value) => value.clone(),
        _ => value.to_string(),
    };
    let mut characters = value.chars();
    let preview = characters.by_ref().take(120).collect::<String>();
    if characters.next().is_some() {
        format!("{preview}...")
    } else {
        preview
    }
}

/// f64 尾数只有 53 位，超过 2^53 的整数无法被精确表示。
const MAX_EXACT_F64_INTEGER: f64 = 9_007_199_254_740_992.0;

fn integer_value(value: &Value) -> Option<i64> {
    if let Some(value) = value.as_i64() {
        return Some(value);
    }
    let text = value.as_str()?.trim();
    // 先做精确整数解析，与字段类型推断 (spreadsheet::infer_fields) 使用的 parse::<i64>()
    // 保持一致；否则 18 位身份证号、16-19 位银行卡/订单/雪花 ID 会在经 f64 转换时被静默舍入
    // （例如 110101199003074258 变成 110101199003074256），破坏后续 JOIN/GROUP BY/导出。
    if let Ok(value) = text.parse::<i64>() {
        return Some(value);
    }
    // 仅为兼容 "42.0" 这类以小数写法书写的整数才回退浮点；超过 f64 精确整数范围的值无法
    // 安全表示，宁可让转换显式失败也不静默取近似（旧实现的 `<= i64::MAX as f64` 上界等于
    // 2^63，会把 (i64::MAX, 2^63) 的值饱和成 i64::MAX）。
    let number = text.parse::<f64>().ok()?;
    (number.is_finite() && number.fract() == 0.0 && number.abs() <= MAX_EXACT_F64_INTEGER)
        .then_some(number as i64)
}

fn decimal_value(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str()?.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite())
}

fn boolean_value(value: &Value) -> Option<bool> {
    if let Some(value) = value.as_bool() {
        return Some(value);
    }
    match value.as_str()?.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" | "y" | "是" => Some(true),
        "false" | "0" | "no" | "n" | "否" => Some(false),
        _ => None,
    }
}

fn date_value(value: &Value) -> Option<i32> {
    let value = value.as_str()?.trim();
    let date = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"]
        .iter()
        .find_map(|format| NaiveDate::parse_from_str(value, format).ok())?;
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1)?;
    i32::try_from((date - epoch).num_days()).ok()
}

fn datetime_value(value: &Value) -> Option<i64> {
    let value = value.as_str()?.trim();
    if let Ok(value) = DateTime::parse_from_rfc3339(value) {
        return Some(value.timestamp_micros());
    }
    [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ]
    .iter()
    .find_map(|format| NaiveDateTime::parse_from_str(value, format).ok())
    .map(|value| value.and_utc().timestamp_micros())
}

fn duck_type_label(data_type: duckdb::types::Type) -> String {
    use duckdb::types::Type;
    match data_type {
        Type::Boolean => "布尔",
        Type::TinyInt
        | Type::SmallInt
        | Type::Int
        | Type::BigInt
        | Type::HugeInt
        | Type::UTinyInt
        | Type::USmallInt
        | Type::UInt
        | Type::UBigInt => "整数",
        Type::Float | Type::Double | Type::Decimal => "小数",
        Type::Date32 => "日期",
        Type::Timestamp | Type::Time64 => "日期时间",
        _ => "文本",
    }
    .to_owned()
}

fn duck_value_to_json(value: DuckValue) -> Value {
    match value {
        DuckValue::Null => Value::Null,
        DuckValue::Boolean(value) => Value::Bool(value),
        DuckValue::TinyInt(value) => Value::Number(value.into()),
        DuckValue::SmallInt(value) => Value::Number(value.into()),
        DuckValue::Int(value) => Value::Number(value.into()),
        DuckValue::BigInt(value) => Value::Number(value.into()),
        DuckValue::HugeInt(value) => i64::try_from(value)
            .map(|value| Value::Number(value.into()))
            .unwrap_or_else(|_| Value::String(value.to_string())),
        DuckValue::UTinyInt(value) => Value::Number(value.into()),
        DuckValue::USmallInt(value) => Value::Number(value.into()),
        DuckValue::UInt(value) => Value::Number(value.into()),
        DuckValue::UBigInt(value) => Value::Number(value.into()),
        DuckValue::Float(value) => Number::from_f64(value as f64)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        DuckValue::Double(value) => Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        DuckValue::Decimal(value) => Value::String(value.to_string()),
        DuckValue::Text(value) | DuckValue::Enum(value) => Value::String(value),
        DuckValue::Blob(value) => Value::String(format!("<{} bytes>", value.len())),
        DuckValue::Timestamp(unit, value) => DateTime::from_timestamp_micros(unit.to_micros(value))
            .map(|value| Value::String(value.naive_utc().format("%Y-%m-%d %H:%M:%S").to_string()))
            .unwrap_or(Value::Null),
        DuckValue::Time64(unit, value) => Value::Number(unit.to_micros(value).into()),
        DuckValue::Date32(value) => NaiveDate::from_ymd_opt(1970, 1, 1)
            .and_then(|epoch| epoch.checked_add_signed(chrono::Duration::days(value as i64)))
            .map(|value| Value::String(value.format("%Y-%m-%d").to_string()))
            .unwrap_or(Value::Null),
        DuckValue::Interval {
            months,
            days,
            nanos,
        } => Value::String(format!("{months} months {days} days {nanos} ns")),
        DuckValue::List(values) | DuckValue::Array(values) => {
            Value::Array(values.into_iter().map(duck_value_to_json).collect())
        }
        DuckValue::Struct(values) => Value::Object(
            values
                .iter()
                .map(|(key, value)| (key.clone(), duck_value_to_json(value.clone())))
                .collect(),
        ),
        DuckValue::Map(values) => Value::Array(
            values
                .iter()
                .map(|(key, value)| {
                    Value::Array(vec![
                        duck_value_to_json(key.clone()),
                        duck_value_to_json(value.clone()),
                    ])
                })
                .collect(),
        ),
        DuckValue::Union(value) => duck_value_to_json(*value),
    }
}

/// 中和电子表格公式注入：以公式触发字符开头的文本前置单引号。触发集除 `= + - @` 外，
/// 还包含制表符和回车——它们同样是标准 CSV 注入字符集的一部分。
fn neutralize_csv_formula(value: &str) -> String {
    if value.starts_with(['=', '+', '-', '@', '\t', '\r']) {
        format!("'{value}")
    } else {
        value.to_owned()
    }
}

fn duck_value_to_csv(value: DuckValue) -> String {
    match value {
        DuckValue::Null => String::new(),
        DuckValue::Text(value) | DuckValue::Enum(value) => neutralize_csv_formula(&value),
        value => match duck_value_to_json(value) {
            Value::Null => String::new(),
            Value::String(value) => value,
            value => value.to_string(),
        },
    }
}

#[cfg(test)]
mod tests {
    use std::io::{BufWriter, Write};

    use super::*;

    #[test]
    fn executes_a_select_query() {
        let test_dir = test_directory();
        let source_path = test_dir.join("amounts.csv");
        fs::write(&source_path, "金额\n20\n30\n").unwrap();
        let result = execute_test_query(
            vec![csv_source(&source_path, "amounts", "data")],
            "SELECT SUM(\"金额\") AS total FROM data",
            &test_dir,
        );
        fs::remove_dir_all(test_dir).unwrap();
        assert_eq!(result.response.rows, vec![vec![Value::from(50)]]);
    }

    #[test]
    fn preserves_csv_text_when_user_overrides_inferred_type() {
        let test_dir = test_directory();
        let source_path = test_dir.join("codes.csv");
        fs::write(&source_path, "code\n00123\n00456\n").unwrap();
        let mut source = csv_source(&source_path, "codes", "data");
        source.columns = vec![FieldDefinition {
            name: "code".to_owned(),
            data_type: "文本".to_owned(),
            nullable: false,
        }];
        let result = execute_test_query(
            vec![source],
            "SELECT code FROM data ORDER BY code",
            &test_dir,
        );
        fs::remove_dir_all(test_dir).unwrap();
        assert_eq!(
            result.response.rows,
            vec![vec![Value::from("00123")], vec![Value::from("00456")]]
        );
    }

    #[test]
    fn preserves_large_integers_beyond_f64_precision() {
        // 18 位身份证号超过 2^53，旧实现经 f64 会舍入为相邻值。
        assert_eq!(
            integer_value(&Value::String("110101199003074258".to_owned())),
            Some(110_101_199_003_074_258)
        );
        assert_eq!(integer_value(&Value::from(42_i64)), Some(42));
        // 以小数写法书写的整数仍兼容。
        assert_eq!(integer_value(&Value::String("42.0".to_owned())), Some(42));
        // 超过 f64 精确整数范围的值无法安全表示，应显式失败而非静默饱和。
        assert_eq!(
            integer_value(&Value::String("99999999999999999999".to_owned())),
            None
        );
        assert_eq!(
            integer_value(&Value::String("not-a-number".to_owned())),
            None
        );
    }

    #[test]
    fn read_only_validator_ignores_keywords_inside_literals_and_comments() {
        // 关键字出现在字符串字面量里应当放行。
        validate_read_only_sql(
            "SELECT status FROM data WHERE status IN ('create', 'update', 'delete')",
        )
        .unwrap();
        validate_read_only_sql("SELECT * FROM data WHERE note = 'has ; semicolon'").unwrap();
        validate_read_only_sql("SELECT 1 -- drop everything\nFROM data").unwrap();
        // 真正的多语句与危险操作仍被拒绝。
        assert!(validate_read_only_sql("SELECT 1; DROP TABLE data").is_err());
        assert!(validate_read_only_sql("SELECT * FROM read_csv('x.csv')").is_err());
        assert!(validate_read_only_sql("PRAGMA database_list").is_err());
    }

    #[test]
    fn cache_key_is_domain_separated_across_field_boundaries() {
        let path = PathBuf::from("/tmp/anydatas-cache-key-test.xlsx");
        let mut first = csv_source(&path, "same-table", "data");
        first.file_kind = "excel".to_owned();
        first.sheet = "AB".to_owned();
        first.start_cell = "C1".to_owned();
        let mut second = first.clone();
        second.sheet = "A".to_owned();
        second.start_cell = "BC1".to_owned();
        assert_ne!(source_cache_key(&first), source_cache_key(&second));
        // 相同配置仍得到相同键，保证缓存复用。
        assert_eq!(source_cache_key(&first), source_cache_key(&first.clone()));
    }

    #[test]
    fn neutralizes_csv_formula_triggers() {
        assert_eq!(
            neutralize_csv_formula("=cmd|'/C calc'!A1"),
            "'=cmd|'/C calc'!A1"
        );
        assert_eq!(neutralize_csv_formula("\t=1+1"), "'\t=1+1");
        assert_eq!(neutralize_csv_formula("正常列名"), "正常列名");
    }

    #[test]
    fn rejects_late_values_that_do_not_match_the_confirmed_type() {
        let test_dir = test_directory();
        let source_path = test_dir.join("mixed-values.csv");
        let mut writer = BufWriter::new(fs::File::create(&source_path).unwrap());
        writeln!(writer, "amount").unwrap();
        for value in 0..SCHEMA_SAMPLE_ROWS {
            writeln!(writer, "{value}").unwrap();
        }
        writeln!(writer, "not-a-number").unwrap();
        writer.flush().unwrap();
        drop(writer);

        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        let error = execute_query(
            vec![csv_source(&source_path, "mixed-values", "data")],
            "SELECT SUM(amount) FROM data",
            100,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap_err();
        fs::remove_dir_all(test_dir).unwrap();

        let message = format!("{error:#}");
        assert!(message.contains("第 2001 条数据"));
        assert!(message.contains("amount"));
        assert!(message.contains("not-a-number"));
    }

    #[test]
    fn queries_more_than_the_previous_source_row_limit() {
        let test_dir = test_directory();
        let source_path = test_dir.join("large.csv");
        let mut writer = BufWriter::new(fs::File::create(&source_path).unwrap());
        writeln!(writer, "编号").unwrap();
        for value in 0..=200_000 {
            writeln!(writer, "{value}").unwrap();
        }
        writer.flush().unwrap();
        drop(writer);

        let result = execute_test_query(
            vec![csv_source(&source_path, "large", "data")],
            "SELECT COUNT(*) AS total FROM data",
            &test_dir,
        );
        fs::remove_dir_all(test_dir).unwrap();

        assert_eq!(result.response.rows, vec![vec![Value::from(200_001)]]);
    }

    #[test]
    fn persists_full_background_result_and_reads_pages() {
        let test_dir = test_directory();
        let source_path = test_dir.join("background.csv");
        let mut writer = BufWriter::new(fs::File::create(&source_path).unwrap());
        writeln!(writer, "id,value").unwrap();
        for value in 0..1_000 {
            writeln!(writer, "{value},item-{value}").unwrap();
        }
        writer.flush().unwrap();
        drop(writer);

        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let artifact_path = test_dir.join("results").join("job.duckdb");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        let result = execute_query_to_artifact(
            vec![csv_source(&source_path, "background", "data")],
            "SELECT * FROM data ORDER BY id",
            20,
            &artifact_path,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap();
        assert_eq!(result.total_rows, 1_000);
        assert_eq!(result.sample.rows.len(), 20);
        assert!(result.sample.truncated);
        assert!(artifact_path.exists());

        let (page, total_rows) =
            read_artifact_page(&artifact_path, 990, 20, &runtime, &work_root).unwrap();
        assert_eq!(total_rows, 1_000);
        assert_eq!(page.rows.len(), 10);
        assert!(!page.truncated);

        let mut csv = Vec::new();
        write_artifact_csv(&artifact_path, &runtime, &mut csv).unwrap();
        let records = csv::Reader::from_reader(csv.as_slice())
            .records()
            .collect::<csv::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(records.len(), 1_000);
        fs::remove_dir_all(test_dir).unwrap();
    }

    #[test]
    fn replaces_artifact_with_post_process_rows() {
        let test_dir = test_directory();
        let source_path = test_dir.join("replace.csv");
        fs::write(&source_path, "id,amount\n1,10\n2,20\n3,30\n").unwrap();
        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let artifact_path = test_dir.join("results").join("replace.duckdb");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        execute_query_to_artifact(
            vec![csv_source(&source_path, "replace", "data")],
            "SELECT * FROM data ORDER BY id",
            20,
            &artifact_path,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap();

        let (columns, rows, total) =
            read_artifact_all_rows(&artifact_path, 100, &runtime, &work_root).unwrap();
        assert_eq!(total, 3);
        assert_eq!(columns.len(), 2);
        assert_eq!(rows.len(), 3);

        let new_columns = vec![
            FieldDefinition {
                name: "amount".to_owned(),
                data_type: "小数".to_owned(),
                nullable: true,
            },
            FieldDefinition {
                name: "doubled".to_owned(),
                data_type: "小数".to_owned(),
                nullable: true,
            },
        ];
        let new_rows = vec![
            vec![Value::from(20.0), Value::from(40.0)],
            vec![Value::from(30.0), Value::from(60.0)],
        ];
        let size =
            replace_artifact_with_rows(&artifact_path, &new_columns, &new_rows, &runtime).unwrap();
        assert!(size > 0);

        let (page, total_rows) =
            read_artifact_page(&artifact_path, 0, 20, &runtime, &work_root).unwrap();
        assert_eq!(total_rows, 2);
        assert_eq!(page.columns.len(), 2);
        assert_eq!(page.columns[1].name, "doubled");
        assert_eq!(
            page.rows,
            vec![
                vec![Value::from(20.0), Value::from(40.0)],
                vec![Value::from(30.0), Value::from(60.0)]
            ]
        );

        let err = read_artifact_all_rows(&artifact_path, 1, &runtime, &work_root).unwrap_err();
        assert!(err.to_string().contains("后处理输入行数超过限制"));
        fs::remove_dir_all(test_dir).unwrap();
    }

    #[test]
    fn post_process_rewrites_full_artifact_not_sample() {
        use crate::models::JsRuntimeLimits;
        use crate::services::post_process::{self, JsHttpRuntime};

        let test_dir = test_directory();
        let source_path = test_dir.join("post-full.csv");
        let mut writer = BufWriter::new(fs::File::create(&source_path).unwrap());
        writeln!(writer, "amount").unwrap();
        for value in 0..250 {
            writeln!(writer, "{value}").unwrap();
        }
        writer.flush().unwrap();
        drop(writer);

        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let artifact_path = test_dir.join("results").join("post-full.duckdb");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        let result = execute_query_to_artifact(
            vec![csv_source(&source_path, "post-full", "data")],
            "SELECT amount FROM data ORDER BY amount",
            20,
            &artifact_path,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap();
        assert_eq!(result.total_rows, 250);
        assert_eq!(result.sample.rows.len(), 20);

        let (columns, rows, total) =
            read_artifact_all_rows(&artifact_path, 20_000, &runtime, &work_root).unwrap();
        assert_eq!(total, 250);
        assert_eq!(rows.len(), 250);

        let limits = JsRuntimeLimits::test_default();
        let http = JsHttpRuntime::new(&limits).unwrap();
        let script = r#"
            function process(rows) {
              return rows
                .filter(r => r.amount >= 200)
                .map(r => ({ amount: r.amount, doubled: r.amount * 2 }));
            }
        "#;
        let out =
            post_process::run_post_process(script, &columns, &rows, &limits, 5_000, Some(&http))
                .unwrap();
        assert_eq!(out.rows.len(), 50);
        assert!(out.columns.iter().any(|c| c.name == "doubled"));

        replace_artifact_with_rows(&artifact_path, &out.columns, &out.rows, &runtime).unwrap();
        let (page, total_rows) =
            read_artifact_page(&artifact_path, 0, 200, &runtime, &work_root).unwrap();
        assert_eq!(total_rows, 50);
        assert_eq!(page.rows.len(), 50);
        assert!(!page.truncated);
        assert_eq!(page.columns.len(), 2);
        fs::remove_dir_all(test_dir).unwrap();
    }

    #[test]
    fn replaces_artifact_with_empty_result() {
        let test_dir = test_directory();
        let source_path = test_dir.join("empty-post.csv");
        fs::write(&source_path, "amount\n1\n2\n").unwrap();
        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let artifact_path = test_dir.join("results").join("empty-post.duckdb");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        execute_query_to_artifact(
            vec![csv_source(&source_path, "empty-post", "data")],
            "SELECT amount FROM data",
            20,
            &artifact_path,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap();

        replace_artifact_with_rows(&artifact_path, &[], &[], &runtime).unwrap();
        let (page, total_rows) =
            read_artifact_page(&artifact_path, 0, 20, &runtime, &work_root).unwrap();
        assert_eq!(total_rows, 0);
        assert!(page.columns.is_empty());
        assert!(page.rows.is_empty());
        fs::remove_dir_all(test_dir).unwrap();
    }

    #[test]
    fn joins_tables_from_different_files() {
        let test_dir = test_directory();
        let customers_path = test_dir.join("customers.csv");
        let orders_path = test_dir.join("orders.csv");
        fs::write(&customers_path, "id,name\n1,甲公司\n2,乙公司\n").unwrap();
        fs::write(&orders_path, "customer_id,amount\n1,20\n1,30\n2,10\n").unwrap();
        let result = execute_test_query(
            vec![
                csv_source(&customers_path, "customers", "customers"),
                csv_source(&orders_path, "orders", "orders"),
            ],
            r#"SELECT c.name, SUM(o.amount) AS total
               FROM customers c JOIN orders o ON o.customer_id = c.id
               GROUP BY c.name ORDER BY total DESC"#,
            &test_dir,
        );
        fs::remove_dir_all(test_dir).unwrap();
        assert_eq!(
            result.response.rows,
            vec![
                vec![Value::from("甲公司"), Value::from(50)],
                vec![Value::from("乙公司"), Value::from(10)]
            ]
        );
    }

    #[test]
    fn reuses_one_cache_for_self_join_aliases() {
        let test_dir = test_directory();
        let source_path = test_dir.join("employees.csv");
        fs::write(&source_path, "id,manager_id\n1,\n2,1\n3,1\n").unwrap();
        let source = csv_source(&source_path, "employees", "employees");
        let mut managers = source.clone();
        managers.alias = "managers".to_owned();
        let result = execute_test_query(
            vec![source, managers],
            "SELECT COUNT(*) FROM employees e JOIN managers m ON e.manager_id = m.id",
            &test_dir,
        );
        fs::remove_dir_all(test_dir).unwrap();
        assert_eq!(result.response.rows, vec![vec![Value::from(2)]]);
    }

    #[test]
    fn rejects_external_reads() {
        assert!(validate_read_only_sql("SELECT * FROM read_csv('/etc/passwd')").is_err());
        assert!(validate_read_only_sql("DROP TABLE data").is_err());
    }

    #[test]
    fn reports_duckdb_compile_details() {
        let test_dir = test_directory();
        let source_path = test_dir.join("compile-error.csv");
        fs::write(&source_path, "amount\n20\n").unwrap();
        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        let error = execute_query(
            vec![csv_source(&source_path, "compile-error", "data")],
            "SELECT missing_column FROM data",
            100,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap_err();
        fs::remove_dir_all(test_dir).unwrap();

        let message = error.to_string();
        assert!(message.contains("SQL 编译失败"));
        assert!(message.contains("missing_column"));
    }

    fn test_directory() -> PathBuf {
        let path = std::env::temp_dir().join(format!("anydatas-query-{}", Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        path
    }

    fn csv_source(path: &Path, table_id: &str, alias: &str) -> QuerySource {
        QuerySource {
            table_id: table_id.to_owned(),
            config_version: 1,
            path: path.to_path_buf(),
            file_kind: "csv".to_owned(),
            sheet: "数据".to_owned(),
            start_cell: "A1".to_owned(),
            end_cell: None,
            first_row_as_header: true,
            alias: alias.to_owned(),
            columns: Vec::new(),
            row_count: 0,
        }
    }

    fn execute_test_query(sources: Vec<QuerySource>, sql: &str, test_dir: &Path) -> QueryExecution {
        let cache_root = test_dir.join("cache");
        let work_root = test_dir.join("work");
        let locks = CacheBuildLocks::default();
        let runtime = test_runtime();
        execute_query(
            sources,
            sql,
            100,
            QueryExecutionContext {
                cache_root: &cache_root,
                work_root: &work_root,
                cache_build_locks: &locks,
                runtime: &runtime,
                execution_control: None,
            },
        )
        .unwrap()
    }

    fn test_runtime() -> QueryRuntimeLimits {
        QueryRuntimeLimits {
            memory_limit_mb: 256,
            threads: 2,
            temp_limit_mb: 1_024,
            min_free_space_bytes: 16 * 1024 * 1024,
            max_artifact_bytes: 512 * 1024 * 1024,
        }
    }
}
