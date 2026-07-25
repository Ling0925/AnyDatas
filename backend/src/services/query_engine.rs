use std::{
    collections::{HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
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
    models::{FieldDefinition, QueryControl, QueryResponse},
    services::spreadsheet,
};

const SCHEMA_SAMPLE_ROWS: usize = 2_000;
const CANCEL_CHECK_INTERVAL: usize = 1_024;

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

struct PreparedSource {
    alias: String,
    cache_path: PathBuf,
}

/// 执行只读多表查询；先准备不可变缓存再挂载别名，可让重复查询跳过 Excel/CSV 解析。
pub fn execute_query(
    sources: Vec<QuerySource>,
    sql: &str,
    limit: usize,
    cache_root: &Path,
    work_root: &Path,
    cache_build_lock: &Mutex<()>,
    job_control: Option<(&std::sync::Mutex<QueryControl>, &str)>,
) -> Result<QueryExecution> {
    validate_read_only_sql(sql)?;
    validate_sources(&sources)?;
    let started = Instant::now();
    let mut prepared_sources = Vec::with_capacity(sources.len());
    let mut cache_updates = Vec::with_capacity(sources.len());
    for source in &sources {
        ensure_job_running(job_control)?;
        let (prepared, update) =
            prepare_source_cache(source, cache_root, cache_build_lock, job_control)?;
        prepared_sources.push(prepared);
        cache_updates.push(update);
    }

    let workspace = QueryWorkspace::create(work_root)?;
    let connection = Connection::open(workspace.database_path()).context("无法初始化 DuckDB")?;
    connection.execute_batch(
        "SET autoinstall_known_extensions = false; SET autoload_known_extensions = false;",
    )?;
    attach_cached_sources(&connection, &prepared_sources)?;
    connection.execute_batch("SET enable_external_access = false;")?;
    let active_query = job_control
        .map(|(control, job_id)| ActiveQueryGuard::register(control, job_id, &connection))
        .transpose()?;
    if let Some(query) = &active_query {
        query.ensure_running()?;
    }
    let clean_sql = sql.trim().trim_end_matches(';').trim();
    let query_sql = format!(
        "SELECT * FROM ({clean_sql}) AS __anydatas_result LIMIT {}",
        limit.saturating_add(1)
    );
    let mut statement = connection
        .prepare(&query_sql)
        .map_err(|error| anyhow::anyhow!("SQL 编译失败: {error}"))?;
    let mut rows = statement
        .query([])
        .map_err(|error| anyhow::anyhow!("SQL 执行失败: {error}"))?;
    let names = rows
        .as_ref()
        .context("DuckDB 未返回结果结构")?
        .column_names();
    let mut result_rows = Vec::new();
    let mut result_types: Vec<Option<String>> = vec![None; names.len()];
    while let Some(row) = rows.next()? {
        let mut values = Vec::with_capacity(names.len());
        for (index, result_type) in result_types.iter_mut().enumerate() {
            let value = row.get_ref(index)?;
            if result_type.is_none() && !matches!(value, duckdb::types::ValueRef::Null) {
                *result_type = Some(duck_type_label(value.data_type()));
            }
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
            data_type: result_types[index]
                .clone()
                .unwrap_or_else(|| "文本".to_owned()),
            nullable: true,
        })
        .collect();

    Ok(QueryExecution {
        response: QueryResponse {
            columns,
            row_count: result_rows.len(),
            rows: result_rows,
            elapsed_ms: started.elapsed().as_millis(),
            truncated,
        },
        cache_updates,
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
fn source_cache_key(source: &QuerySource) -> String {
    let mut digest = Sha256::new();
    digest.update(source.table_id.as_bytes());
    digest.update(source.config_version.to_le_bytes());
    digest.update(source.sheet.as_bytes());
    digest.update(source.start_cell.as_bytes());
    digest.update(source.end_cell.as_deref().unwrap_or("").as_bytes());
    digest.update([u8::from(source.first_row_as_header)]);
    hex::encode(digest.finalize())
}

/// 在全局构建锁内检查并生成单表缓存，避免多个请求同时重复导入同一大文件。
fn prepare_source_cache(
    source: &QuerySource,
    cache_root: &Path,
    cache_build_lock: &Mutex<()>,
    job_control: Option<(&std::sync::Mutex<QueryControl>, &str)>,
) -> Result<(PreparedSource, QueryCacheUpdate)> {
    fs::create_dir_all(cache_root)
        .with_context(|| format!("无法创建表缓存目录 {}", cache_root.display()))?;
    let cache_key = source_cache_key(source);
    let cache_path = cache_root.join(format!("{cache_key}.duckdb"));
    let mut columns = source.columns.clone();
    let mut row_count = source.row_count;
    {
        let _guard = cache_build_lock
            .lock()
            .map_err(|_| anyhow::anyhow!("表缓存构建器不可用"))?;
        if !cache_path.exists() || columns.is_empty() {
            if cache_path.exists() {
                fs::remove_file(&cache_path)?;
            }
            let metadata = build_source_cache(source, &cache_path, job_control)?;
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
    job_control: Option<(&std::sync::Mutex<QueryControl>, &str)>,
) -> Result<(Vec<FieldDefinition>, usize)> {
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
        connection.execute_batch(
            "SET enable_external_access = false; SET autoinstall_known_extensions = false; SET autoload_known_extensions = false;",
        )?;
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
                    ensure_job_running(job_control)?;
                }
                let values = row
                    .iter()
                    .zip(&columns)
                    .map(|(value, column)| json_to_duck(value, &column.data_type));
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

/// 检查后台任务取消标记，缓存导入期间也能及时停止而不必等待 SQL 阶段。
fn ensure_job_running(job_control: Option<(&std::sync::Mutex<QueryControl>, &str)>) -> Result<()> {
    if let Some((control, job_id)) = job_control {
        let queries = control
            .lock()
            .map_err(|_| anyhow::anyhow!("任务控制器不可用"))?;
        if queries.canceled.contains(job_id) {
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
    fn register(
        control: &'a std::sync::Mutex<QueryControl>,
        job_id: &str,
        connection: &Connection,
    ) -> Result<Self> {
        let mut queries = control
            .lock()
            .map_err(|_| anyhow::anyhow!("任务控制器不可用"))?;
        queries
            .active
            .insert(job_id.to_owned(), connection.interrupt_handle());
        Ok(Self {
            control,
            job_id: job_id.to_owned(),
        })
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
    if clean.contains(';') {
        bail!("一次只能执行一条查询");
    }
    let forbidden = Regex::new(
        r"(?i)\b(attach|copy|install|load|call|pragma|create|insert|update|delete|drop|alter|export|import|set|read_csv|read_csv_auto|read_parquet|read_json|read_ndjson|glob)\b",
    )?;
    if forbidden.is_match(clean) {
        bail!("查询包含不允许的文件或数据库操作");
    }
    Ok(())
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
}

impl Drop for QueryWorkspace {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.path) {
            tracing::warn!(?error, path = %self.path.display(), "failed to remove query workspace");
        }
    }
}

/// 创建缓存数据表并根据采样类型选择 DuckDB 列类型，兼顾聚合性能与原始值兼容性。
fn create_cache_table(connection: &Connection, columns: &[FieldDefinition]) -> Result<()> {
    let definitions = columns
        .iter()
        .map(|column| {
            format!(
                "{} {}",
                quote_identifier(&column.name),
                duck_column_type(&column.data_type)
            )
        })
        .collect::<Vec<_>>()
        .join(", ");
    connection.execute_batch(&format!("CREATE TABLE cached_data ({definitions});"))?;
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

fn json_to_duck(value: &Value, data_type: &str) -> DuckValue {
    if value.is_null() {
        return DuckValue::Null;
    }
    match data_type {
        "整数" => integer_value(value)
            .map(DuckValue::BigInt)
            .unwrap_or(DuckValue::Null),
        "小数" => decimal_value(value)
            .map(DuckValue::Double)
            .unwrap_or(DuckValue::Null),
        "布尔" => boolean_value(value)
            .map(DuckValue::Boolean)
            .unwrap_or(DuckValue::Null),
        "日期" => date_value(value)
            .map(DuckValue::Date32)
            .unwrap_or(DuckValue::Null),
        "日期时间" => datetime_value(value)
            .map(|value| DuckValue::Timestamp(TimeUnit::Microsecond, value))
            .unwrap_or(DuckValue::Null),
        _ => value
            .as_str()
            .map(str::to_owned)
            .map(DuckValue::Text)
            .unwrap_or_else(|| DuckValue::Text(value.to_string())),
    }
}

fn integer_value(value: &Value) -> Option<i64> {
    if let Some(value) = value.as_i64() {
        return Some(value);
    }
    let number = value.as_str()?.trim().parse::<f64>().ok()?;
    (number.is_finite()
        && number.fract() == 0.0
        && number >= i64::MIN as f64
        && number <= i64::MAX as f64)
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
        let error = execute_query(
            vec![csv_source(&source_path, "compile-error", "data")],
            "SELECT missing_column FROM data",
            100,
            &test_dir.join("cache"),
            &test_dir.join("work"),
            &Mutex::new(()),
            None,
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
        execute_query(
            sources,
            sql,
            100,
            &cache_root,
            &work_root,
            &Mutex::new(()),
            None,
        )
        .unwrap()
    }
}
