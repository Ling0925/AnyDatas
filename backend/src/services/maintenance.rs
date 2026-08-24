use std::{collections::HashSet, fs, path::Path, time::Duration};

use anyhow::{Context, Result, bail};
use chrono::Utc;

use crate::models::SharedState;

#[derive(Debug, Default)]
pub struct CleanupReport {
    pub query_directories: usize,
    pub temporary_caches: usize,
    pub orphaned_caches: usize,
    pub orphaned_revisions: usize,
    pub expired_revisions: usize,
    pub expired_imports: usize,
    pub temporary_results: usize,
    pub orphaned_results: usize,
    pub expired_results: usize,
}

/// 启动服务前清理无法继续使用的临时产物，并只保留数据库仍引用的表缓存。
///
/// 这一步在监听端口和启动 Worker 之前完成，因此不会误删正在执行的查询文件；
/// 服务异常退出后再次启动即可自动恢复干净的数据卷状态。
pub async fn cleanup_startup_storage(state: &SharedState) -> Result<CleanupReport> {
    let mut report = CleanupReport::default();
    let query_root = state.data_dir.join("query-work");
    let cache_root = state.data_dir.join("table-cache");
    let upload_root = state.data_dir.join("uploads");
    let staging_root = state.data_dir.join("staging");
    let result_root = state.data_dir.join("job-results");
    fs::create_dir_all(&query_root)?;
    fs::create_dir_all(&cache_root)?;
    fs::create_dir_all(&upload_root)?;
    fs::create_dir_all(&staging_root)?;
    fs::create_dir_all(&result_root)?;

    report.query_directories = remove_directory_children(&query_root)?;
    let referenced = sqlx::query_scalar::<_, String>(
        "SELECT cache_key FROM source_tables WHERE cache_key IS NOT NULL",
    )
    .fetch_all(&state.pool)
    .await?
    .into_iter()
    .collect::<HashSet<_>>();
    for entry in fs::read_dir(&cache_root)? {
        let path = entry?.path();
        if !path.is_file() {
            continue;
        }
        if path.extension().and_then(|value| value.to_str()) == Some("tmp") {
            fs::remove_file(&path)?;
            report.temporary_caches += 1;
            continue;
        }
        if path.extension().and_then(|value| value.to_str()) != Some("duckdb") {
            continue;
        }
        let key = path.file_stem().and_then(|value| value.to_str());
        if key.is_none_or(|key| !referenced.contains(key)) {
            fs::remove_file(&path)?;
            report.orphaned_caches += 1;
        }
    }

    let revision_cleanup = cleanup_source_revisions(state, &upload_root).await?;
    report.orphaned_revisions = revision_cleanup.0;
    report.expired_revisions = revision_cleanup.1;

    let expired = sqlx::query_as::<_, (String, String)>(
        "SELECT id, stored_path FROM staged_imports WHERE expires_at < ?",
    )
    .bind(Utc::now().to_rfc3339())
    .fetch_all(&state.pool)
    .await?;
    for (id, stored_path) in expired {
        sqlx::query("DELETE FROM staged_imports WHERE id = ?")
            .bind(id)
            .execute(&state.pool)
            .await?;
        remove_file_if_present(Path::new(&stored_path))?;
        report.expired_imports += 1;
    }
    let referenced_staging =
        sqlx::query_scalar::<_, String>("SELECT stored_path FROM staged_imports")
            .fetch_all(&state.pool)
            .await?
            .into_iter()
            .collect::<HashSet<_>>();
    for entry in fs::read_dir(&staging_root)? {
        let path = entry?.path();
        if path.is_file() && !referenced_staging.contains(&path.to_string_lossy().to_string()) {
            remove_file_if_present(&path)?;
            report.expired_imports += 1;
        }
    }
    report.expired_results = cleanup_expired_job_results(state).await?;
    let referenced_results = sqlx::query_scalar::<_, String>(
        "SELECT result_artifact_key FROM jobs WHERE result_artifact_key IS NOT NULL",
    )
    .fetch_all(&state.pool)
    .await?
    .into_iter()
    .collect::<HashSet<_>>();
    for entry in fs::read_dir(&result_root)? {
        let path = entry?.path();
        if path.is_dir() {
            fs::remove_dir_all(&path)?;
            report.temporary_results += 1;
            continue;
        }
        if path.extension().and_then(|value| value.to_str()) == Some("tmp") {
            fs::remove_file(&path)?;
            report.temporary_results += 1;
            continue;
        }
        if path.extension().and_then(|value| value.to_str()) != Some("duckdb") {
            continue;
        }
        let key = path.file_stem().and_then(|value| value.to_str());
        if key.is_none_or(|key| !referenced_results.contains(key)) {
            fs::remove_file(&path)?;
            report.orphaned_results += 1;
        }
    }
    Ok(report)
}

fn has_active_storage_users(state: &SharedState) -> bool {
    let active_worker = state
        .query_control
        .lock()
        .map(|control| !control.worker_jobs.is_empty())
        .unwrap_or(true);
    active_worker
        || state
            .active_refresh_preparations
            .load(std::sync::atomic::Ordering::SeqCst)
            > 0
}

/// Periodically reclaim cache files left by timed-out or failed refresh preparation. A grace period
/// prevents deletion of a candidate that a blocking validation thread is still finishing.
pub async fn cleanup_orphaned_cache_files(
    state: &SharedState,
    minimum_age: Duration,
) -> Result<usize> {
    if has_active_storage_users(state) {
        return Ok(0);
    }
    let _storage_guard = state.storage_maintenance_lock.lock().await;
    if has_active_storage_users(state) {
        return Ok(0);
    }
    let cache_root = state.data_dir.join("table-cache");
    fs::create_dir_all(&cache_root)?;
    let referenced = sqlx::query_scalar::<_, String>(
        r#"
        SELECT cache_key FROM source_tables WHERE cache_key IS NOT NULL
        UNION
        SELECT ji.cache_key
        FROM job_input_tables ji
        JOIN jobs j ON j.id = ji.job_id
        WHERE ji.cache_key IS NOT NULL AND j.status IN ('queued', 'running')
        "#,
    )
    .fetch_all(&state.pool)
    .await?
    .into_iter()
    .collect::<HashSet<_>>();
    let mut removed = 0usize;
    for entry in fs::read_dir(&cache_root)? {
        let path = entry?.path();
        if !path.is_file() || !file_age_at_least(&path, minimum_age) {
            continue;
        }
        let extension = path.extension().and_then(|value| value.to_str());
        let orphaned_duckdb = extension == Some("duckdb")
            && path
                .file_stem()
                .and_then(|value| value.to_str())
                .is_none_or(|key| !referenced.contains(key));
        if extension == Some("tmp") || orphaned_duckdb {
            remove_file_if_present(&path)?;
            removed += 1;
        }
    }
    Ok(removed)
}

/// Remove expired non-current revisions and immutable files that no database row references.
/// Current revisions and inputs needed by queued/running jobs always win over retention timestamps.
pub async fn cleanup_source_revisions(
    state: &SharedState,
    upload_root: &Path,
) -> Result<(usize, usize)> {
    if has_active_storage_users(state) {
        return Ok((0, 0));
    }
    let _storage_guard = state.storage_maintenance_lock.lock().await;
    if has_active_storage_users(state) {
        return Ok((0, 0));
    }
    let now = Utc::now().to_rfc3339();
    let expired = sqlx::query_as::<_, (String, String)>(
        r#"
        SELECT r.id, r.stored_path
        FROM source_revisions r
        JOIN data_sources d ON d.id = r.source_id
        WHERE d.current_revision_id <> r.id
          AND r.retained_until IS NOT NULL
          AND r.retained_until < ?
          AND NOT EXISTS (
              SELECT 1
              FROM job_input_tables ji
              JOIN jobs j ON j.id = ji.job_id
              WHERE ji.source_revision_id = r.id
                AND j.status IN ('queued', 'running')
          )
        "#,
    )
    .bind(&now)
    .fetch_all(&state.pool)
    .await?;
    let mut expired_count = 0usize;
    for (revision_id, stored_path) in expired {
        let deleted = sqlx::query(
            r#"
            DELETE FROM source_revisions
            WHERE id = ?
              AND retained_until IS NOT NULL
              AND retained_until < ?
              AND NOT EXISTS (
                  SELECT 1 FROM data_sources WHERE current_revision_id = ?
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM job_input_tables ji
                  JOIN jobs j ON j.id = ji.job_id
                  WHERE ji.source_revision_id = ?
                    AND j.status IN ('queued', 'running')
              )
            "#,
        )
        .bind(&revision_id)
        .bind(&now)
        .bind(&revision_id)
        .bind(&revision_id)
        .execute(&state.pool)
        .await?;
        if deleted.rows_affected() == 1 {
            remove_file_if_present(Path::new(&stored_path))?;
            expired_count += 1;
        }
    }

    let referenced = sqlx::query_scalar::<_, String>("SELECT stored_path FROM source_revisions")
        .fetch_all(&state.pool)
        .await?
        .into_iter()
        .collect::<HashSet<_>>();
    let mut files = Vec::new();
    collect_files(upload_root, &mut files)?;
    let mut orphaned_count = 0usize;
    for path in files {
        let path_text = path.to_string_lossy().to_string();
        let legacy_backup = path
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|name| name.contains(".backup-"));
        let stale = file_age_at_least(&path, Duration::from_secs(60 * 60));
        if stale && (legacy_backup || !referenced.contains(&path_text)) {
            remove_file_if_present(&path)?;
            orphaned_count += 1;
        }
    }
    remove_empty_directories(upload_root)?;
    Ok((orphaned_count, expired_count))
}

fn file_age_at_least(path: &Path, minimum_age: Duration) -> bool {
    fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .and_then(|modified| modified.elapsed().map_err(std::io::Error::other))
        .is_ok_and(|age| age >= minimum_age)
}

fn collect_files(root: &Path, output: &mut Vec<std::path::PathBuf>) -> Result<()> {
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if path.is_dir() {
            collect_files(&path, output)?;
        } else if path.is_file() {
            output.push(path);
        }
    }
    Ok(())
}

fn remove_empty_directories(root: &Path) -> Result<()> {
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if !path.is_dir() {
            continue;
        }
        remove_empty_directories(&path)?;
        if fs::read_dir(&path)?.next().is_none() {
            fs::remove_dir(&path)?;
        }
    }
    Ok(())
}

/// 到期后删除完整结果产物但保留任务审计记录，历史 SQL、耗时和日志仍可查看。
///
/// 数据库字段在文件删除后同一轮清空，前端会明确显示结果已过期，而不是继续提供
/// 一个必然返回 404 的下载入口。
pub async fn cleanup_expired_job_results(state: &SharedState) -> Result<usize> {
    let now = Utc::now().to_rfc3339();
    let expired = sqlx::query_as::<_, (String, String)>(
        r#"
        SELECT id, result_artifact_key
        FROM jobs
        WHERE result_artifact_key IS NOT NULL
          AND result_expires_at IS NOT NULL
          AND result_expires_at < ?
        "#,
    )
    .bind(&now)
    .fetch_all(&state.pool)
    .await?;
    for (job_id, artifact_key) in &expired {
        if is_artifact_key(artifact_key) {
            let path = state
                .data_dir
                .join("job-results")
                .join(format!("{artifact_key}.duckdb"));
            remove_file_if_present(&path)?;
        }
        sqlx::query(
            r#"
            UPDATE jobs
            SET result_json = NULL, result_artifact_key = NULL,
                result_artifact_format = NULL, result_size_bytes = NULL,
                result_expires_at = NULL, updated_at = ?
            WHERE id = ?
            "#,
        )
        .bind(&now)
        .bind(job_id)
        .execute(&state.pool)
        .await?;
    }
    Ok(expired.len())
}

/// 删除已经不被当前逻辑表或活跃任务输入引用的指定缓存。
///
/// 活跃任务可能已经解析旧版本缓存但尚未 ATTACH；将 job_input_tables 纳入引用计数可避免
/// 刷新提交在这个窗口删除其固定输入缓存。终态任务可以从保留的不可变源版本按需重建。
pub async fn remove_cache_keys_if_unreferenced(
    state: &SharedState,
    keys: impl IntoIterator<Item = String>,
) -> Result<usize> {
    let mut removed = 0usize;
    let unique = keys.into_iter().collect::<HashSet<_>>();
    for key in unique {
        if !is_cache_key(&key) {
            tracing::warn!(%key, "ignored malformed cache key during cleanup");
            continue;
        }
        let references: i64 = sqlx::query_scalar(
            r#"
            SELECT
                (SELECT COUNT(*) FROM source_tables WHERE cache_key = ?)
                +
                (SELECT COUNT(*)
                 FROM job_input_tables ji
                 JOIN jobs j ON j.id = ji.job_id
                 WHERE ji.cache_key = ? AND j.status IN ('queued', 'running'))
            "#,
        )
        .bind(&key)
        .bind(&key)
        .fetch_one(&state.pool)
        .await?;
        if references == 0 {
            let path = state
                .data_dir
                .join("table-cache")
                .join(format!("{key}.duckdb"));
            if path.exists() {
                remove_file_if_present(&path)?;
                removed += 1;
            }
        }
    }
    Ok(removed)
}

/// 检查目标数据卷的可用空间，给 SQLite、日志和操作系统保留最低余量。
///
/// `additional_bytes` 表示即将写入的已知大小；上传流未知最终大小时可周期性调用，
/// 从而在磁盘完全写满前主动结束请求。
pub fn ensure_free_space(
    path: &Path,
    minimum_free_bytes: u64,
    additional_bytes: u64,
) -> Result<()> {
    let available = fs2::available_space(path)
        .with_context(|| format!("无法检查数据卷剩余空间 {}", path.display()))?;
    let required = minimum_free_bytes.saturating_add(additional_bytes);
    if available < required {
        bail!(
            "数据卷剩余空间不足：至少需要保留 {} MB，当前可用 {} MB",
            minimum_free_bytes / 1024 / 1024,
            available / 1024 / 1024
        );
    }
    Ok(())
}

/// 删除目录下的启动期临时项，根目录本身保留以维持挂载点权限和 inode 稳定。
fn remove_directory_children(root: &Path) -> Result<usize> {
    let mut removed = 0usize;
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if path.is_dir() {
            fs::remove_dir_all(&path)?;
        } else {
            fs::remove_file(&path)?;
        }
        removed += 1;
    }
    Ok(removed)
}

fn remove_file_if_present(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error).with_context(|| format!("无法删除文件 {}", path.display())),
    }
}

fn is_cache_key(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn is_artifact_key(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_sha256_cache_keys() {
        assert!(is_cache_key(&"a".repeat(64)));
        assert!(!is_cache_key("../uploads/source"));
        assert!(!is_cache_key(&"g".repeat(64)));
    }

    #[test]
    fn removes_children_without_deleting_the_root() {
        let directory = tempfile::tempdir().unwrap();
        fs::write(directory.path().join("file"), "value").unwrap();
        fs::create_dir(directory.path().join("nested")).unwrap();
        fs::write(directory.path().join("nested").join("file"), "value").unwrap();

        assert_eq!(remove_directory_children(directory.path()).unwrap(), 2);
        assert!(directory.path().exists());
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
    }
}
