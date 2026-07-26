use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, Ordering},
    },
};

use duckdb::InterruptHandle;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{FromRow, SqlitePool};
use tokio::sync::{Notify, Semaphore};

pub struct AppState {
    pub pool: SqlitePool,
    pub data_dir: PathBuf,
    pub max_upload_bytes: usize,
    pub session_ttl_days: i64,
    pub cookie_secure: bool,
    pub secret_key: [u8; 32],
    pub http_client: reqwest::Client,
    pub query_control: Mutex<QueryControl>,
    pub cache_build_locks: CacheBuildLocks,
    pub query_semaphore: Arc<Semaphore>,
    pub file_parse_semaphore: Arc<Semaphore>,
    pub resource_queue_timeout_seconds: u64,
    pub query_timeout_seconds: u64,
    pub background_query_timeout_seconds: u64,
    pub file_parse_timeout_seconds: u64,
    pub query_runtime: QueryRuntimeLimits,
    pub agent_control: Mutex<HashMap<String, Arc<AgentRunControl>>>,
    pub agent_max_steps: usize,
    pub agent_timeout_seconds: u64,
    pub agent_context_chars: usize,
}

pub type SharedState = Arc<AppState>;

#[derive(Debug, Clone)]
pub struct QueryRuntimeLimits {
    pub memory_limit_mb: usize,
    pub threads: usize,
    pub temp_limit_mb: usize,
    pub min_free_space_bytes: u64,
}

/// 按缓存键维护互斥量，避免不同 Sheet 的首次查询被一把全局锁串行化。
#[derive(Default)]
pub struct CacheBuildLocks {
    locks: Mutex<HashMap<String, Weak<Mutex<()>>>>,
}

impl CacheBuildLocks {
    /// 获取指定缓存键的共享锁；弱引用可让不再使用的键自动退出注册表。
    ///
    /// 同一缓存仍只构建一次，而互不相关的文件可以并行准备，提升多文件工作区的吞吐。
    pub fn lock_for(&self, key: &str) -> Result<Arc<Mutex<()>>, &'static str> {
        let mut locks = self.locks.lock().map_err(|_| "缓存锁注册表不可用")?;
        locks.retain(|_, lock| lock.strong_count() > 0);
        if let Some(lock) = locks.get(key).and_then(Weak::upgrade) {
            return Ok(lock);
        }
        let lock = Arc::new(Mutex::new(()));
        locks.insert(key.to_owned(), Arc::downgrade(&lock));
        Ok(lock)
    }
}

#[derive(Default)]
pub struct QueryControl {
    pub active: HashMap<String, Arc<InterruptHandle>>,
    pub canceled: HashSet<String>,
}

/// 运行控制对象同时提供无锁状态检查和异步唤醒，使取消可以立即中断正在等待的模型请求。
pub struct AgentRunControl {
    canceled: AtomicBool,
    notify: Notify,
}

impl AgentRunControl {
    /// 创建尚未取消的运行控制对象，随后由共享状态按 Run id 管理。
    pub fn new() -> Self {
        Self {
            canceled: AtomicBool::new(false),
            notify: Notify::new(),
        }
    }

    /// 标记取消并唤醒等待中的 Agent，重复调用保持幂等。
    pub fn cancel(&self) {
        self.canceled.store(true, Ordering::SeqCst);
        self.notify.notify_one();
    }

    /// 快速检查取消状态，工具执行前后均可使用而无需进入异步等待。
    pub fn is_canceled(&self) -> bool {
        self.canceled.load(Ordering::SeqCst)
    }

    /// 等待取消信号；单等待者许可会被保留，避免检查与订阅之间丢失事件。
    pub async fn cancelled(&self) {
        if self.is_canceled() {
            return;
        }
        self.notify.notified().await;
    }
}

#[derive(Debug, Clone, FromRow)]
pub struct DataSourceRow {
    pub id: String,
    pub name: String,
    pub original_filename: String,
    pub stored_path: String,
    pub media_type: String,
    pub file_kind: String,
    pub size_bytes: i64,
    pub selected_sheet: String,
    pub start_cell: String,
    pub first_row_as_header: bool,
    pub sheet_names_json: String,
    pub row_count: i64,
    pub column_count: i64,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DataSource {
    pub id: String,
    pub name: String,
    pub original_filename: String,
    pub media_type: String,
    pub file_kind: String,
    pub size_bytes: i64,
    pub selected_sheet: String,
    pub start_cell: String,
    pub first_row_as_header: bool,
    pub sheet_names: Vec<String>,
    pub row_count: i64,
    pub column_count: i64,
    pub sql_table_name: &'static str,
    pub created_at: String,
    pub updated_at: String,
}

impl From<DataSourceRow> for DataSource {
    fn from(row: DataSourceRow) -> Self {
        Self {
            id: row.id,
            name: row.name,
            original_filename: row.original_filename,
            media_type: row.media_type,
            file_kind: row.file_kind,
            size_bytes: row.size_bytes,
            selected_sheet: row.selected_sheet,
            start_cell: row.start_cell,
            first_row_as_header: row.first_row_as_header,
            sheet_names: serde_json::from_str(&row.sheet_names_json).unwrap_or_default(),
            row_count: row.row_count,
            column_count: row.column_count,
            sql_table_name: "data",
            created_at: row.created_at,
            updated_at: row.updated_at,
        }
    }
}

#[derive(Debug, Clone, FromRow)]
pub struct SourceTableRow {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub original_filename: String,
    pub stored_path: String,
    pub file_kind: String,
    pub name: String,
    pub sheet_name: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
    pub row_count: i64,
    pub column_count: i64,
    pub schema_json: String,
    pub config_version: i64,
    pub cache_status: String,
    pub cache_error: Option<String>,
    pub is_default: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceTable {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub original_filename: String,
    pub file_kind: String,
    pub name: String,
    pub sheet_name: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
    pub row_count: i64,
    pub column_count: i64,
    pub fields: Vec<FieldDefinition>,
    pub config_version: i64,
    pub cache_status: String,
    pub cache_error: Option<String>,
    pub is_default: bool,
    pub created_at: String,
    pub updated_at: String,
}

impl From<SourceTableRow> for SourceTable {
    /// 将数据库行转换为公开模型，隐藏文件路径和缓存键，避免泄露服务器内部信息。
    fn from(row: SourceTableRow) -> Self {
        Self {
            id: row.id,
            source_id: row.source_id,
            source_name: row.source_name,
            original_filename: row.original_filename,
            file_kind: row.file_kind,
            name: row.name,
            sheet_name: row.sheet_name,
            start_cell: row.start_cell,
            end_cell: row.end_cell,
            first_row_as_header: row.first_row_as_header,
            row_count: row.row_count,
            column_count: row.column_count,
            fields: serde_json::from_str(&row.schema_json).unwrap_or_default(),
            config_version: row.config_version,
            cache_status: row.cache_status,
            cache_error: row.cache_error,
            is_default: row.is_default,
            created_at: row.created_at,
            updated_at: row.updated_at,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct FieldDefinition {
    pub name: String,
    pub data_type: String,
    pub nullable: bool,
}

#[derive(Debug, Clone)]
pub struct TableData {
    pub columns: Vec<FieldDefinition>,
    pub rows: Vec<Vec<Value>>,
    pub total_rows: usize,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PreviewResponse {
    pub columns: Vec<FieldDefinition>,
    pub rows: Vec<Vec<Value>>,
    pub total_rows: usize,
    pub truncated: bool,
    pub sheet: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateSourceConfig {
    pub selected_sheet: String,
    pub start_cell: String,
    pub first_row_as_header: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportSheetInspection {
    pub name: String,
    pub row_count: usize,
    pub column_count: usize,
    pub fields: Vec<FieldDefinition>,
    pub rows: Vec<Vec<Value>>,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportInspection {
    pub token: String,
    pub original_filename: String,
    pub file_kind: String,
    pub size_bytes: usize,
    pub sheets: Vec<ImportSheetInspection>,
    pub expires_at: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ImportTableConfig {
    pub name: String,
    pub sheet_name: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
    pub fields: Vec<FieldDefinition>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InspectImportTableRequest {
    pub sheet_name: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CommitImportRequest {
    pub token: String,
    pub tables: Vec<ImportTableConfig>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PreviewParams {
    pub sheet: Option<String>,
    pub start_cell: Option<String>,
    pub first_row_as_header: Option<bool>,
    pub limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateSourceTableRequest {
    pub name: String,
    pub sheet_name: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
    #[serde(default)]
    pub fields: Option<Vec<FieldDefinition>>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateSourceTableRequest {
    pub name: String,
    pub sheet_name: String,
    pub start_cell: String,
    pub end_cell: Option<String>,
    pub first_row_as_header: bool,
    #[serde(default)]
    pub fields: Option<Vec<FieldDefinition>>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceTableListParams {
    pub source_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, FromRow)]
#[serde(rename_all = "camelCase")]
pub struct QueryTableBinding {
    pub table_id: String,
    pub alias: String,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct QueryRequest {
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub tables: Vec<QueryTableBinding>,
    pub sql: String,
    pub sheet: Option<String>,
    pub start_cell: Option<String>,
    pub first_row_as_header: Option<bool>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct QueryResponse {
    pub columns: Vec<FieldDefinition>,
    pub rows: Vec<Vec<Value>>,
    pub row_count: usize,
    pub elapsed_ms: u128,
    pub truncated: bool,
}

#[derive(Debug, Clone, FromRow)]
pub struct SavedQueryRow {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub name: String,
    pub sql_text: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SavedQuery {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub name: String,
    pub sql: String,
    pub tables: Vec<QueryTableBinding>,
    pub created_at: String,
    pub updated_at: String,
}

impl From<SavedQueryRow> for SavedQuery {
    fn from(row: SavedQueryRow) -> Self {
        Self {
            id: row.id,
            source_id: row.source_id,
            source_name: row.source_name,
            name: row.name,
            sql: row.sql_text,
            tables: Vec::new(),
            created_at: row.created_at,
            updated_at: row.updated_at,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SavedQueryPayload {
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub tables: Vec<QueryTableBinding>,
    pub name: String,
    pub sql: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SavedQueryListParams {
    pub source_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub service: &'static str,
}

#[derive(Debug, Clone, FromRow)]
pub struct JobRow {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub schedule_id: Option<String>,
    pub name: String,
    pub kind: String,
    pub sql_text: String,
    pub status: String,
    pub progress: i64,
    pub trigger_type: String,
    pub result_json: Option<String>,
    pub result_row_count: Option<i64>,
    pub error_message: Option<String>,
    pub logs_json: String,
    pub created_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct JobLog {
    pub at: String,
    pub level: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Job {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub schedule_id: Option<String>,
    pub name: String,
    pub kind: String,
    pub sql: String,
    pub tables: Vec<QueryTableBinding>,
    pub status: String,
    pub progress: i64,
    pub trigger_type: String,
    pub result: Option<QueryResponse>,
    pub result_row_count: Option<i64>,
    pub error_message: Option<String>,
    pub logs: Vec<JobLog>,
    pub created_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub updated_at: String,
}

impl From<JobRow> for Job {
    fn from(row: JobRow) -> Self {
        Self {
            id: row.id,
            source_id: row.source_id,
            source_name: row.source_name,
            schedule_id: row.schedule_id,
            name: row.name,
            kind: row.kind,
            sql: row.sql_text,
            tables: Vec::new(),
            status: row.status,
            progress: row.progress,
            trigger_type: row.trigger_type,
            result: row
                .result_json
                .as_deref()
                .and_then(|value| serde_json::from_str(value).ok()),
            result_row_count: row.result_row_count,
            error_message: row.error_message,
            logs: serde_json::from_str(&row.logs_json).unwrap_or_default(),
            created_at: row.created_at,
            started_at: row.started_at,
            finished_at: row.finished_at,
            updated_at: row.updated_at,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateJobRequest {
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub tables: Vec<QueryTableBinding>,
    pub name: String,
    pub sql: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct JobListParams {
    pub status: Option<String>,
    pub query: Option<String>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Serialize, FromRow)]
#[serde(rename_all = "camelCase")]
pub struct JobSummary {
    pub total: i64,
    pub queued: i64,
    pub running: i64,
    pub succeeded: i64,
    pub failed: i64,
    pub canceled: i64,
}

#[derive(Debug, Clone, FromRow)]
pub struct ScheduleRow {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub name: String,
    pub sql_text: String,
    pub cron_expression: String,
    pub timezone: String,
    pub enabled: bool,
    pub next_run_at: Option<String>,
    pub last_run_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ScheduleItem {
    pub id: String,
    pub source_id: String,
    pub source_name: String,
    pub name: String,
    pub sql: String,
    pub tables: Vec<QueryTableBinding>,
    pub cron_expression: String,
    pub timezone: String,
    pub enabled: bool,
    pub next_run_at: Option<String>,
    pub last_run_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

impl From<ScheduleRow> for ScheduleItem {
    fn from(row: ScheduleRow) -> Self {
        Self {
            id: row.id,
            source_id: row.source_id,
            source_name: row.source_name,
            name: row.name,
            sql: row.sql_text,
            tables: Vec::new(),
            cron_expression: row.cron_expression,
            timezone: row.timezone,
            enabled: row.enabled,
            next_run_at: row.next_run_at,
            last_run_at: row.last_run_at,
            created_at: row.created_at,
            updated_at: row.updated_at,
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpsertScheduleRequest {
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub tables: Vec<QueryTableBinding>,
    pub name: String,
    pub sql: String,
    pub cron_expression: String,
    pub timezone: String,
    pub enabled: bool,
}

#[derive(Debug, Deserialize)]
pub struct ToggleScheduleRequest {
    pub enabled: bool,
}
