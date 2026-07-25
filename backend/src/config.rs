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
    pub agent_max_steps: usize,
    pub agent_timeout_seconds: u64,
    pub agent_context_chars: usize,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let data_dir =
            PathBuf::from(env::var("ANYDATAS_DATA_DIR").unwrap_or_else(|_| "var-rust".to_owned()));
        let database_url = env::var("ANYDATAS_DATABASE_URL")
            .unwrap_or_else(|_| format!("sqlite://{}", data_dir.join("anydatas.db").display()));

        let agent_max_steps = parse_usize("ANYDATAS_AGENT_MAX_STEPS", 6)?;
        let agent_timeout_seconds = parse_usize("ANYDATAS_AGENT_TIMEOUT_SECONDS", 300)?;
        let agent_context_chars = parse_usize("ANYDATAS_AGENT_CONTEXT_CHARS", 80_000)?;
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
