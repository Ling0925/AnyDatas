use std::{collections::HashSet, fs, path::Path};

use anyhow::{Context, Result, bail};
use chrono::Utc;

use crate::models::SharedState;

#[derive(Debug, Default)]
pub struct CleanupReport {
    pub query_directories: usize,
    pub temporary_caches: usize,
    pub orphaned_caches: usize,
    pub expired_imports: usize,
}

/// 启动服务前清理无法继续使用的临时产物，并只保留数据库仍引用的表缓存。
///
/// 这一步在监听端口和启动 Worker 之前完成，因此不会误删正在执行的查询文件；
/// 服务异常退出后再次启动即可自动恢复干净的数据卷状态。
pub async fn cleanup_startup_storage(state: &SharedState) -> Result<CleanupReport> {
    let mut report = CleanupReport::default();
    let query_root = state.data_dir.join("query-work");
    let cache_root = state.data_dir.join("table-cache");
    let staging_root = state.data_dir.join("staging");
    fs::create_dir_all(&query_root)?;
    fs::create_dir_all(&cache_root)?;
    fs::create_dir_all(&staging_root)?;

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
    Ok(report)
}

/// 删除已经不被任何逻辑表引用的指定缓存，供配置更新和数据源删除后即时回收空间。
///
/// 删除前再次查询引用计数可以覆盖同一缓存被多个别名复用的情况，避免把仍在使用的
/// 文件误删。
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
        let references: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM source_tables WHERE cache_key = ?")
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
