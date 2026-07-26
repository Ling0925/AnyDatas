use std::{env, path::PathBuf};

use anyhow::{Context, Result};

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub data_dir: PathBuf,
    pub database_url: String,
    pub web_dir: PathBuf,
    pub max_upload_bytes: usize,
    pub session_ttl_days: i64,
    pub cookie_secure: bool,
    pub metrics_token: Option<String>,
    pub allow_private_ai_endpoints: bool,
    pub query_max_concurrency: usize,
    pub file_parse_max_concurrency: usize,
    pub resource_queue_timeout_seconds: u64,
    pub query_timeout_seconds: u64,
    pub background_query_timeout_seconds: u64,
    pub file_parse_timeout_seconds: u64,
    pub duckdb_memory_limit_mb: usize,
    pub duckdb_threads: usize,
    pub duckdb_temp_limit_mb: usize,
    pub min_free_space_mb: usize,
    pub job_result_max_mb: usize,
    pub job_result_retention_days: i64,
    pub agent_max_steps: usize,
    pub agent_timeout_seconds: u64,
    pub agent_context_chars: usize,
}

impl Config {
    /// 从环境变量构建单机运行配置，并在启动前拒绝可能耗尽主机资源的无效参数。
    ///
    /// 把边界集中在这里的好处是 Docker、裸机和测试环境共享同一套校验规则，
    /// 不会等到第一条大查询进入 DuckDB 后才暴露配置错误。
    pub fn from_env() -> Result<Self> {
        let data_dir =
            PathBuf::from(env::var("ANYDATAS_DATA_DIR").unwrap_or_else(|_| "var-rust".to_owned()));
        let database_url = env::var("ANYDATAS_DATABASE_URL")
            .unwrap_or_else(|_| format!("sqlite://{}", data_dir.join("anydatas.db").display()));

        let query_max_concurrency = parse_usize("ANYDATAS_QUERY_MAX_CONCURRENCY", 2)?;
        let file_parse_max_concurrency = parse_usize("ANYDATAS_FILE_PARSE_MAX_CONCURRENCY", 1)?;
        let resource_queue_timeout_seconds =
            parse_usize("ANYDATAS_RESOURCE_QUEUE_TIMEOUT_SECONDS", 30)?;
        let query_timeout_seconds = parse_usize("ANYDATAS_QUERY_TIMEOUT_SECONDS", 120)?;
        let background_query_timeout_seconds =
            parse_usize("ANYDATAS_BACKGROUND_QUERY_TIMEOUT_SECONDS", 3_600)?;
        let file_parse_timeout_seconds = parse_usize("ANYDATAS_FILE_PARSE_TIMEOUT_SECONDS", 1_800)?;
        let duckdb_memory_limit_mb = parse_usize("ANYDATAS_DUCKDB_MEMORY_LIMIT_MB", 1_024)?;
        let duckdb_threads = parse_usize(
            "ANYDATAS_DUCKDB_THREADS",
            std::thread::available_parallelism()
                .map(usize::from)
                .unwrap_or(2)
                .saturating_sub(1)
                .clamp(1, 4),
        )?;
        let duckdb_temp_limit_mb = parse_usize("ANYDATAS_DUCKDB_TEMP_LIMIT_MB", 10_240)?;
        let min_free_space_mb = parse_usize("ANYDATAS_MIN_FREE_SPACE_MB", 1_024)?;
        let job_result_max_mb = parse_usize("ANYDATAS_JOB_RESULT_MAX_MB", 20_480)?;
        let job_result_retention_days = parse_usize("ANYDATAS_JOB_RESULT_RETENTION_DAYS", 30)?;
        let agent_max_steps = parse_usize("ANYDATAS_AGENT_MAX_STEPS", 6)?;
        let agent_timeout_seconds = parse_usize("ANYDATAS_AGENT_TIMEOUT_SECONDS", 300)?;
        let agent_context_chars = parse_usize("ANYDATAS_AGENT_CONTEXT_CHARS", 80_000)?;
        if !(1..=32).contains(&query_max_concurrency) {
            anyhow::bail!("ANYDATAS_QUERY_MAX_CONCURRENCY must be between 1 and 32");
        }
        if !(1..=8).contains(&file_parse_max_concurrency) {
            anyhow::bail!("ANYDATAS_FILE_PARSE_MAX_CONCURRENCY must be between 1 and 8");
        }
        if !(1..=600).contains(&resource_queue_timeout_seconds) {
            anyhow::bail!("ANYDATAS_RESOURCE_QUEUE_TIMEOUT_SECONDS must be between 1 and 600");
        }
        if !(5..=7_200).contains(&query_timeout_seconds) {
            anyhow::bail!("ANYDATAS_QUERY_TIMEOUT_SECONDS must be between 5 and 7200");
        }
        if !(30..=86_400).contains(&background_query_timeout_seconds) {
            anyhow::bail!("ANYDATAS_BACKGROUND_QUERY_TIMEOUT_SECONDS must be between 30 and 86400");
        }
        if !(30..=86_400).contains(&file_parse_timeout_seconds) {
            anyhow::bail!("ANYDATAS_FILE_PARSE_TIMEOUT_SECONDS must be between 30 and 86400");
        }
        if !(128..=262_144).contains(&duckdb_memory_limit_mb) {
            anyhow::bail!("ANYDATAS_DUCKDB_MEMORY_LIMIT_MB must be between 128 and 262144");
        }
        if !(1..=128).contains(&duckdb_threads) {
            anyhow::bail!("ANYDATAS_DUCKDB_THREADS must be between 1 and 128");
        }
        if !(256..=1_048_576).contains(&duckdb_temp_limit_mb) {
            anyhow::bail!("ANYDATAS_DUCKDB_TEMP_LIMIT_MB must be between 256 and 1048576");
        }
        if !(64..=1_048_576).contains(&min_free_space_mb) {
            anyhow::bail!("ANYDATAS_MIN_FREE_SPACE_MB must be between 64 and 1048576");
        }
        if !(64..=1_048_576).contains(&job_result_max_mb) {
            anyhow::bail!("ANYDATAS_JOB_RESULT_MAX_MB must be between 64 and 1048576");
        }
        if !(1..=3_650).contains(&job_result_retention_days) {
            anyhow::bail!("ANYDATAS_JOB_RESULT_RETENTION_DAYS must be between 1 and 3650");
        }
        if !(2..=20).contains(&agent_max_steps) {
            anyhow::bail!("ANYDATAS_AGENT_MAX_STEPS must be between 2 and 20");
        }
        if !(30..=1_800).contains(&agent_timeout_seconds) {
            anyhow::bail!("ANYDATAS_AGENT_TIMEOUT_SECONDS must be between 30 and 1800");
        }
        if !(20_000..=500_000).contains(&agent_context_chars) {
            anyhow::bail!("ANYDATAS_AGENT_CONTEXT_CHARS must be between 20000 and 500000");
        }

        Ok(Self {
            bind: env::var("ANYDATAS_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_owned()),
            web_dir: PathBuf::from(
                env::var("ANYDATAS_WEB_DIR").unwrap_or_else(|_| "frontend/dist".to_owned()),
            ),
            max_upload_bytes: parse_usize("ANYDATAS_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)?,
            session_ttl_days: parse_usize("ANYDATAS_SESSION_TTL_DAYS", 7)? as i64,
            cookie_secure: parse_bool("ANYDATAS_COOKIE_SECURE", false)?,
            metrics_token: read_optional_secret(
                "ANYDATAS_METRICS_TOKEN",
                "ANYDATAS_METRICS_TOKEN_FILE",
            )?,
            allow_private_ai_endpoints: parse_bool("ANYDATAS_AI_ALLOW_PRIVATE_NETWORK", false)?,
            query_max_concurrency,
            file_parse_max_concurrency,
            resource_queue_timeout_seconds: resource_queue_timeout_seconds as u64,
            query_timeout_seconds: query_timeout_seconds as u64,
            background_query_timeout_seconds: background_query_timeout_seconds as u64,
            file_parse_timeout_seconds: file_parse_timeout_seconds as u64,
            duckdb_memory_limit_mb,
            duckdb_threads,
            duckdb_temp_limit_mb,
            min_free_space_mb,
            job_result_max_mb,
            job_result_retention_days: job_result_retention_days as i64,
            agent_max_steps,
            agent_timeout_seconds: agent_timeout_seconds as u64,
            agent_context_chars,
            data_dir,
            database_url,
        })
    }

    pub fn upload_dir(&self) -> PathBuf {
        self.data_dir.join("uploads")
    }

    /// 暂存目录与正式上传目录位于同一数据卷，确认导入时可以原子移动大文件。
    pub fn staging_dir(&self) -> PathBuf {
        self.data_dir.join("staging")
    }
}

fn parse_bool(name: &str, default: bool) -> Result<bool> {
    match env::var(name) {
        Ok(value) => match value.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => Ok(true),
            "0" | "false" | "no" | "off" => Ok(false),
            _ => anyhow::bail!("{name} must be a boolean"),
        },
        Err(_) => Ok(default),
    }
}

fn parse_usize(name: &str, default: usize) -> Result<usize> {
    match env::var(name) {
        Ok(value) => value
            .parse::<usize>()
            .with_context(|| format!("{name} must be a positive integer")),
        Err(_) => Ok(default),
    }
}

/// 从环境变量或挂载文件读取可选密钥，文件方式可避免令牌出现在 Compose 进程环境中。
fn read_optional_secret(value_name: &str, file_name: &str) -> Result<Option<String>> {
    if let Ok(value) = env::var(value_name) {
        let value = value.trim().to_owned();
        if !value.is_empty() {
            return Ok(Some(value));
        }
    }
    let Ok(path) = env::var(file_name) else {
        return Ok(None);
    };
    let path = path.trim();
    if path.is_empty() {
        return Ok(None);
    }
    let value = std::fs::read_to_string(path)
        .with_context(|| format!("failed to read secret file configured by {file_name}"))?;
    let value = value.trim().to_owned();
    if value.is_empty() {
        anyhow::bail!("{file_name} points to an empty secret file");
    }
    Ok(Some(value))
}
