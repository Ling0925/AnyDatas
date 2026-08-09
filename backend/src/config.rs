use std::{env, path::PathBuf};

use anyhow::{Context, Result};

use crate::services::net_guard::{self, AllowlistEntry};

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
    pub js_http_enabled: bool,
    pub js_http_allowlist: Vec<AllowlistEntry>,
    pub js_allow_private_network: bool,
    pub js_max_script_bytes: usize,
    pub js_max_input_rows: usize,
    pub js_max_output_rows: usize,
    pub js_timeout_ms: u64,
    pub js_job_timeout_ms: u64,
    pub js_memory_mb: usize,
    pub js_max_console_lines: usize,
    pub js_max_input_payload_bytes: usize,
    pub js_http_max_requests: usize,
    pub js_http_timeout_ms: u64,
    pub js_http_max_timeout_ms: u64,
    pub js_http_max_body_bytes: usize,
    pub js_http_max_request_body_bytes: usize,
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
        let max_upload_bytes = parse_usize("ANYDATAS_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)?;
        // 下界拒绝会让上传永远失败的过小值；上界放到 1 TiB，只用于挡住明显笔误/溢出，
        // 不限制真实部署（例如面向大文件分析时配置的 10 GiB 上传上限）。
        if !(1_048_576..=1_099_511_627_776).contains(&max_upload_bytes) {
            anyhow::bail!(
                "ANYDATAS_MAX_UPLOAD_BYTES must be between 1048576 and 1099511627776 (1 TiB)"
            );
        }
        let session_ttl_days = parse_usize("ANYDATAS_SESSION_TTL_DAYS", 7)?;
        if !(1..=3_650).contains(&session_ttl_days) {
            anyhow::bail!("ANYDATAS_SESSION_TTL_DAYS must be between 1 and 3650");
        }

        let js_http_enabled = parse_bool("ANYDATAS_JS_HTTP", true)?;
        let js_allow_private_network = parse_bool("ANYDATAS_JS_ALLOW_PRIVATE_NETWORK", false)?;
        let js_max_script_bytes = parse_usize("ANYDATAS_JS_MAX_SCRIPT_BYTES", 65_536)?;
        let js_max_input_rows = parse_usize("ANYDATAS_JS_MAX_INPUT_ROWS", 20_000)?;
        let js_max_output_rows = parse_usize("ANYDATAS_JS_MAX_OUTPUT_ROWS", 20_000)?;
        let js_timeout_ms = parse_usize("ANYDATAS_JS_TIMEOUT_MS", 5_000)?;
        let js_job_timeout_ms = parse_usize("ANYDATAS_JS_JOB_TIMEOUT_MS", 30_000)?;
        let js_memory_mb = parse_usize("ANYDATAS_JS_MEMORY_MB", 64)?;
        let js_max_console_lines = parse_usize("ANYDATAS_JS_MAX_CONSOLE_LINES", 50)?;
        let js_max_input_payload_bytes =
            parse_usize("ANYDATAS_JS_MAX_INPUT_PAYLOAD_BYTES", 33_554_432)?;
        let js_http_max_requests = parse_usize("ANYDATAS_JS_HTTP_MAX_REQUESTS", 8)?;
        let js_http_timeout_ms = parse_usize("ANYDATAS_JS_HTTP_TIMEOUT_MS", 3_000)?;
        let js_http_max_timeout_ms = parse_usize("ANYDATAS_JS_HTTP_MAX_TIMEOUT_MS", 10_000)?;
        let js_http_max_body_bytes = parse_usize("ANYDATAS_JS_HTTP_MAX_BODY_BYTES", 2_097_152)?;
        let js_http_max_request_body_bytes =
            parse_usize("ANYDATAS_JS_HTTP_MAX_REQUEST_BODY_BYTES", 1_048_576)?;
        let js_http_allowlist = load_js_http_allowlist()?;

        if !(1_024..=1_048_576).contains(&js_max_script_bytes) {
            anyhow::bail!("ANYDATAS_JS_MAX_SCRIPT_BYTES must be between 1024 and 1048576");
        }
        if !(1..=1_000_000).contains(&js_max_input_rows) {
            anyhow::bail!("ANYDATAS_JS_MAX_INPUT_ROWS must be between 1 and 1000000");
        }
        if !(1..=1_000_000).contains(&js_max_output_rows) {
            anyhow::bail!("ANYDATAS_JS_MAX_OUTPUT_ROWS must be between 1 and 1000000");
        }
        if !(100..=120_000).contains(&js_timeout_ms) {
            anyhow::bail!("ANYDATAS_JS_TIMEOUT_MS must be between 100 and 120000");
        }
        if !(100..=600_000).contains(&js_job_timeout_ms) {
            anyhow::bail!("ANYDATAS_JS_JOB_TIMEOUT_MS must be between 100 and 600000");
        }
        if js_job_timeout_ms < js_timeout_ms {
            anyhow::bail!(
                "ANYDATAS_JS_JOB_TIMEOUT_MS must be greater than or equal to ANYDATAS_JS_TIMEOUT_MS"
            );
        }
        if !(8..=4_096).contains(&js_memory_mb) {
            anyhow::bail!("ANYDATAS_JS_MEMORY_MB must be between 8 and 4096");
        }
        if !(0..=1_000).contains(&js_max_console_lines) {
            anyhow::bail!("ANYDATAS_JS_MAX_CONSOLE_LINES must be between 0 and 1000");
        }
        if !(1_024..=268_435_456).contains(&js_max_input_payload_bytes) {
            anyhow::bail!("ANYDATAS_JS_MAX_INPUT_PAYLOAD_BYTES must be between 1024 and 268435456");
        }
        if !(0..=100).contains(&js_http_max_requests) {
            anyhow::bail!("ANYDATAS_JS_HTTP_MAX_REQUESTS must be between 0 and 100");
        }
        if !(100..=120_000).contains(&js_http_timeout_ms) {
            anyhow::bail!("ANYDATAS_JS_HTTP_TIMEOUT_MS must be between 100 and 120000");
        }
        if !(100..=120_000).contains(&js_http_max_timeout_ms) {
            anyhow::bail!("ANYDATAS_JS_HTTP_MAX_TIMEOUT_MS must be between 100 and 120000");
        }
        if js_http_max_timeout_ms < js_http_timeout_ms {
            anyhow::bail!(
                "ANYDATAS_JS_HTTP_MAX_TIMEOUT_MS must be greater than or equal to ANYDATAS_JS_HTTP_TIMEOUT_MS"
            );
        }
        if !(1_024..=33_554_432).contains(&js_http_max_body_bytes) {
            anyhow::bail!("ANYDATAS_JS_HTTP_MAX_BODY_BYTES must be between 1024 and 33554432");
        }
        if !(1_024..=16_777_216).contains(&js_http_max_request_body_bytes) {
            anyhow::bail!(
                "ANYDATAS_JS_HTTP_MAX_REQUEST_BODY_BYTES must be between 1024 and 16777216"
            );
        }

        Ok(Self {
            bind: env::var("ANYDATAS_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_owned()),
            web_dir: PathBuf::from(
                env::var("ANYDATAS_WEB_DIR").unwrap_or_else(|_| "frontend/dist".to_owned()),
            ),
            max_upload_bytes,
            session_ttl_days: session_ttl_days as i64,
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
            js_http_enabled,
            js_http_allowlist,
            js_allow_private_network,
            js_max_script_bytes,
            js_max_input_rows,
            js_max_output_rows,
            js_timeout_ms: js_timeout_ms as u64,
            js_job_timeout_ms: js_job_timeout_ms as u64,
            js_memory_mb,
            js_max_console_lines,
            js_max_input_payload_bytes,
            js_http_max_requests,
            js_http_timeout_ms: js_http_timeout_ms as u64,
            js_http_max_timeout_ms: js_http_max_timeout_ms as u64,
            js_http_max_body_bytes,
            js_http_max_request_body_bytes,
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

/// 合并 env 文本与可选文件后解析 JS HTTP 白名单；非法条目 fail-fast，避免带错配置启动。
fn load_js_http_allowlist() -> Result<Vec<AllowlistEntry>> {
    let mut combined = String::new();
    if let Ok(value) = env::var("ANYDATAS_JS_HTTP_ALLOWLIST") {
        combined.push_str(&value);
    }
    if let Ok(path) = env::var("ANYDATAS_JS_HTTP_ALLOWLIST_FILE") {
        let path = path.trim();
        if !path.is_empty() {
            let file_text = std::fs::read_to_string(path).with_context(|| {
                format!("failed to read JS HTTP allowlist file configured by ANYDATAS_JS_HTTP_ALLOWLIST_FILE ({path})")
            })?;
            if !combined.is_empty() && !combined.ends_with('\n') {
                combined.push('\n');
            }
            combined.push_str(&file_text);
        }
    }
    net_guard::parse_allowlist(&combined)
        .map_err(|error| anyhow::anyhow!("ANYDATAS_JS_HTTP_ALLOWLIST invalid: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, OnceLock};

    /// Serialize env-mutating tests so parallel cargo test workers do not clobber each other.
    fn env_lock() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(())).lock().unwrap()
    }

    fn clear_js_env() {
        const KEYS: &[&str] = &[
            "ANYDATAS_JS_HTTP",
            "ANYDATAS_JS_HTTP_ALLOWLIST",
            "ANYDATAS_JS_HTTP_ALLOWLIST_FILE",
            "ANYDATAS_JS_ALLOW_PRIVATE_NETWORK",
            "ANYDATAS_JS_MAX_SCRIPT_BYTES",
            "ANYDATAS_JS_MAX_INPUT_ROWS",
            "ANYDATAS_JS_MAX_OUTPUT_ROWS",
            "ANYDATAS_JS_TIMEOUT_MS",
            "ANYDATAS_JS_JOB_TIMEOUT_MS",
            "ANYDATAS_JS_MEMORY_MB",
            "ANYDATAS_JS_MAX_CONSOLE_LINES",
            "ANYDATAS_JS_MAX_INPUT_PAYLOAD_BYTES",
            "ANYDATAS_JS_HTTP_MAX_REQUESTS",
            "ANYDATAS_JS_HTTP_TIMEOUT_MS",
            "ANYDATAS_JS_HTTP_MAX_TIMEOUT_MS",
            "ANYDATAS_JS_HTTP_MAX_BODY_BYTES",
            "ANYDATAS_JS_HTTP_MAX_REQUEST_BODY_BYTES",
        ];
        for key in KEYS {
            unsafe { env::remove_var(key) };
        }
    }

    #[test]
    fn js_defaults_match_spec() {
        let _guard = env_lock();
        clear_js_env();
        let config = Config::from_env().expect("defaults should parse");
        assert!(config.js_http_enabled);
        assert!(config.js_http_allowlist.is_empty());
        assert!(!config.js_allow_private_network);
        assert_eq!(config.js_max_script_bytes, 65_536);
        assert_eq!(config.js_max_input_rows, 20_000);
        assert_eq!(config.js_max_output_rows, 20_000);
        assert_eq!(config.js_timeout_ms, 5_000);
        assert_eq!(config.js_job_timeout_ms, 30_000);
        assert_eq!(config.js_memory_mb, 64);
        assert_eq!(config.js_max_console_lines, 50);
        assert_eq!(config.js_max_input_payload_bytes, 33_554_432);
        assert_eq!(config.js_http_max_requests, 8);
        assert_eq!(config.js_http_timeout_ms, 3_000);
        assert_eq!(config.js_http_max_timeout_ms, 10_000);
        assert_eq!(config.js_http_max_body_bytes, 2_097_152);
        assert_eq!(config.js_http_max_request_body_bytes, 1_048_576);
    }

    #[test]
    fn js_allowlist_merges_env_and_file_and_fails_fast() {
        let _guard = env_lock();
        clear_js_env();
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("allowlist.txt");
        std::fs::write(&path, "localhost:11434\n# comment\n").unwrap();
        unsafe {
            env::set_var("ANYDATAS_JS_HTTP_ALLOWLIST", "api.example.com");
            env::set_var(
                "ANYDATAS_JS_HTTP_ALLOWLIST_FILE",
                path.to_string_lossy().as_ref(),
            );
        }
        let config = Config::from_env().expect("merged allowlist should parse");
        assert_eq!(
            config.js_http_allowlist,
            vec![
                AllowlistEntry::Host("api.example.com".into()),
                AllowlistEntry::HostPort {
                    host: "localhost".into(),
                    port: 11434,
                },
            ]
        );

        clear_js_env();
        unsafe {
            env::set_var("ANYDATAS_JS_HTTP_ALLOWLIST", "not a valid entry");
        }
        let err = Config::from_env().unwrap_err().to_string();
        assert!(
            err.contains("ANYDATAS_JS_HTTP_ALLOWLIST"),
            "unexpected error: {err}"
        );
        clear_js_env();
    }

    #[test]
    fn js_timeout_out_of_range_is_rejected() {
        let _guard = env_lock();
        clear_js_env();
        unsafe {
            env::set_var("ANYDATAS_JS_TIMEOUT_MS", "50");
        }
        let err = Config::from_env().unwrap_err().to_string();
        assert!(err.contains("ANYDATAS_JS_TIMEOUT_MS"), "unexpected: {err}");
        clear_js_env();
    }
}
