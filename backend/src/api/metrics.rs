use std::{fmt::Write as FmtWrite, fs, path::Path, sync::atomic::Ordering};

use axum::{
    Router,
    body::Body,
    extract::State,
    http::{
        HeaderMap,
        header::{AUTHORIZATION, CONTENT_TYPE},
    },
    response::Response,
    routing::get,
};
use chrono::Utc;

use crate::{
    error::{AppError, AppResult},
    models::SharedState,
};

pub fn router() -> Router<SharedState> {
    Router::new().route("/metrics", get(metrics))
}

/// 输出单机部署所需的低基数 Prometheus 指标，并用独立 Bearer Token 保护运行信息。
///
/// 指标不包含工作区、文件名或用户标识，避免高基数和敏感信息泄露；数据库聚合仅按
/// 固定状态分组，15 秒抓取周期下对 SQLite 的影响可控。
async fn metrics(State(state): State<SharedState>, headers: HeaderMap) -> AppResult<Response> {
    authorize(&state, &headers)?;
    let jobs =
        sqlx::query_as::<_, (String, i64)>("SELECT status, COUNT(*) FROM jobs GROUP BY status")
            .fetch_all(&state.pool)
            .await?;
    let agent_runs =
        sqlx::query_as::<_, (String, i64)>("SELECT status, COUNT(*) FROM ai_runs GROUP BY status")
            .fetch_all(&state.pool)
            .await?;
    let data_sources: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM data_sources")
        .fetch_one(&state.pool)
        .await?;
    let source_rows: i64 =
        sqlx::query_scalar("SELECT COALESCE(SUM(row_count), 0) FROM source_tables")
            .fetch_one(&state.pool)
            .await?;

    let storage_root = state.data_dir.clone();
    let storage = tokio::task::spawn_blocking(move || storage_metrics(&storage_root))
        .await
        .map_err(|error| AppError::Internal(format!("存储指标线程异常: {error}")))?
        .map_err(|error| AppError::Internal(error.to_string()))?;

    let mut body = String::with_capacity(4_096);
    metric_help(
        &mut body,
        "anydatas_build_info",
        "AnyDatas process build information.",
        "gauge",
    );
    body.push_str("anydatas_build_info{version=\"0.1.0\"} 1\n");
    metric_help(
        &mut body,
        "anydatas_process_uptime_seconds",
        "Seconds since the AnyDatas process started.",
        "gauge",
    );
    let _ = writeln!(
        body,
        "anydatas_process_uptime_seconds {}",
        state.metrics.started_at.elapsed().as_secs()
    );
    metric_help(
        &mut body,
        "anydatas_http_requests_total",
        "HTTP requests handled by AnyDatas.",
        "counter",
    );
    let _ = writeln!(
        body,
        "anydatas_http_requests_total {}",
        state.metrics.http_requests_total.load(Ordering::Relaxed)
    );
    metric_help(
        &mut body,
        "anydatas_http_server_errors_total",
        "HTTP 5xx responses returned by AnyDatas.",
        "counter",
    );
    let _ = writeln!(
        body,
        "anydatas_http_server_errors_total {}",
        state
            .metrics
            .http_server_errors_total
            .load(Ordering::Relaxed)
    );
    metric_help(
        &mut body,
        "anydatas_query_slots",
        "Configured and currently used query execution slots.",
        "gauge",
    );
    let query_used = state
        .query_max_concurrency
        .saturating_sub(state.query_semaphore.available_permits());
    let _ = writeln!(body, "anydatas_query_slots{{state=\"used\"}} {query_used}");
    let _ = writeln!(
        body,
        "anydatas_query_slots{{state=\"capacity\"}} {}",
        state.query_max_concurrency
    );
    let file_parse_used = state
        .file_parse_max_concurrency
        .saturating_sub(state.file_parse_semaphore.available_permits());
    metric_help(
        &mut body,
        "anydatas_file_parse_slots",
        "Configured and currently used file parsing slots.",
        "gauge",
    );
    let _ = writeln!(
        body,
        "anydatas_file_parse_slots{{state=\"used\"}} {file_parse_used}"
    );
    let _ = writeln!(
        body,
        "anydatas_file_parse_slots{{state=\"capacity\"}} {}",
        state.file_parse_max_concurrency
    );
    append_status_metrics(
        &mut body,
        "anydatas_jobs",
        "Background jobs by status.",
        jobs,
    );
    append_status_metrics(
        &mut body,
        "anydatas_agent_runs",
        "AI Agent runs by status.",
        agent_runs,
    );
    metric_help(
        &mut body,
        "anydatas_data_sources",
        "Uploaded data source count.",
        "gauge",
    );
    let _ = writeln!(body, "anydatas_data_sources {data_sources}");
    metric_help(
        &mut body,
        "anydatas_source_rows",
        "Configured logical table row count.",
        "gauge",
    );
    let _ = writeln!(body, "anydatas_source_rows {source_rows}");
    append_worker_metrics(&state, &mut body);
    metric_help(
        &mut body,
        "anydatas_storage_bytes",
        "Bytes used by AnyDatas storage categories.",
        "gauge",
    );
    for (kind, bytes) in storage.categories {
        let _ = writeln!(body, "anydatas_storage_bytes{{kind=\"{kind}\"}} {bytes}");
    }
    metric_help(
        &mut body,
        "anydatas_storage_available_bytes",
        "Available bytes on the AnyDatas data volume.",
        "gauge",
    );
    let _ = writeln!(
        body,
        "anydatas_storage_available_bytes {}",
        storage.available_bytes
    );

    Response::builder()
        .header(CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8")
        .body(Body::from(body))
        .map_err(|error| AppError::Internal(error.to_string()))
}

fn authorize(state: &SharedState, headers: &HeaderMap) -> AppResult<()> {
    let expected = state
        .metrics_token
        .as_deref()
        .ok_or_else(|| AppError::NotFound("指标接口未启用".to_owned()))?;
    let provided = headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .unwrap_or_default();
    if constant_time_eq(expected.as_bytes(), provided.as_bytes()) {
        Ok(())
    } else {
        Err(AppError::Unauthorized("指标访问令牌无效".to_owned()))
    }
}

/// 用固定遍历次数比较指标令牌，避免普通字符串比较提前返回而泄露有效前缀。
fn constant_time_eq(expected: &[u8], provided: &[u8]) -> bool {
    let mut difference = expected.len() ^ provided.len();
    let compared_length = expected.len().max(provided.len());
    for index in 0..compared_length {
        let expected_byte = expected.get(index).copied().unwrap_or_default();
        let provided_byte = provided.get(index).copied().unwrap_or_default();
        difference |= usize::from(expected_byte ^ provided_byte);
    }
    difference == 0
}

fn metric_help(output: &mut String, name: &str, help: &str, kind: &str) {
    let _ = writeln!(output, "# HELP {name} {help}");
    let _ = writeln!(output, "# TYPE {name} {kind}");
}

fn append_status_metrics(output: &mut String, name: &str, help: &str, values: Vec<(String, i64)>) {
    metric_help(output, name, help, "gauge");
    for (status, count) in values {
        if status
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        {
            let _ = writeln!(output, "{name}{{status=\"{status}\"}} {count}");
        }
    }
}

fn append_worker_metrics(state: &SharedState, output: &mut String) {
    metric_help(
        output,
        "anydatas_worker_up",
        "Whether an in-process worker has reported within its expected interval.",
        "gauge",
    );
    let now = Utc::now().timestamp();
    let workers = [
        (
            "jobs",
            state.metrics.job_worker_heartbeat.load(Ordering::Relaxed),
            10,
        ),
        (
            "schedules",
            state
                .metrics
                .schedule_worker_heartbeat
                .load(Ordering::Relaxed),
            30,
        ),
        (
            "maintenance",
            state
                .metrics
                .maintenance_worker_heartbeat
                .load(Ordering::Relaxed),
            7_200,
        ),
    ];
    for (worker, heartbeat, threshold) in workers {
        let up = i32::from(heartbeat > 0 && now.saturating_sub(heartbeat) <= threshold);
        let _ = writeln!(output, "anydatas_worker_up{{worker=\"{worker}\"}} {up}");
    }
}

struct StorageMetrics {
    categories: Vec<(&'static str, u64)>,
    available_bytes: u64,
}

fn storage_metrics(root: &Path) -> anyhow::Result<StorageMetrics> {
    Ok(StorageMetrics {
        categories: vec![
            ("uploads", directory_size(&root.join("uploads"))?),
            ("table_cache", directory_size(&root.join("table-cache"))?),
            ("job_results", directory_size(&root.join("job-results"))?),
            (
                "metadata",
                fs::metadata(root.join("anydatas.db"))
                    .map(|metadata| metadata.len())
                    .unwrap_or(0),
            ),
        ],
        available_bytes: fs2::available_space(root)?,
    })
}

fn directory_size(path: &Path) -> anyhow::Result<u64> {
    if !path.exists() {
        return Ok(0);
    }
    let mut bytes = 0u64;
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let metadata = entry.metadata()?;
        if metadata.is_dir() {
            bytes = bytes.saturating_add(directory_size(&entry.path())?);
        } else if metadata.is_file() {
            bytes = bytes.saturating_add(metadata.len());
        }
    }
    Ok(bytes)
}
