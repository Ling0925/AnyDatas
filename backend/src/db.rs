use std::str::FromStr;

use anyhow::Result;
use chrono::Utc;
use sqlx::{
    SqlitePool,
    sqlite::{SqliteConnectOptions, SqlitePoolOptions},
};

use crate::models::{DataSourceRow, SourceTableRow};

pub async fn connect(database_url: &str) -> Result<SqlitePool> {
    let options = SqliteConnectOptions::from_str(database_url)?
        .create_if_missing(true)
        .foreign_keys(true)
        .journal_mode(sqlx::sqlite::SqliteJournalMode::Wal)
        .busy_timeout(std::time::Duration::from_secs(5));
    let pool = SqlitePoolOptions::new()
        .max_connections(8)
        .connect_with(options)
        .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    Ok(pool)
}

pub async fn recover_interrupted_jobs(pool: &SqlitePool) -> Result<()> {
    let now = Utc::now().to_rfc3339();
    sqlx::query(
        r#"
        UPDATE jobs
        SET status = 'failed',
            error_message = '服务重启导致任务中断',
            finished_at = ?,
            updated_at = ?
        WHERE status = 'running'
        "#,
    )
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await?;
    Ok(())
}

/// 服务重启后关闭无法继续的 Agent Run 和 Step，持久化状态不会永久停留在运行中。
pub async fn recover_interrupted_agent_runs(pool: &SqlitePool) -> Result<()> {
    let now = Utc::now().to_rfc3339();
    let mut transaction = pool.begin().await?;
    sqlx::query(
        r#"
        UPDATE ai_run_steps
        SET status = 'failed',
            error_message = '服务重启导致步骤中断',
            finished_at = ?
        WHERE status = 'running'
        "#,
    )
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    sqlx::query(
        r#"
        UPDATE ai_runs
        SET status = 'failed',
            finish_reason = 'server_restart',
            error_message = '服务重启导致 Agent 运行中断，请重新发送',
            finished_at = ?,
            updated_at = ?
        WHERE status IN ('queued', 'running')
        "#,
    )
    .bind(&now)
    .bind(&now)
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok(())
}

pub async fn get_data_source(
    pool: &SqlitePool,
    id: &str,
    workspace_id: Option<&str>,
) -> Result<Option<DataSourceRow>, sqlx::Error> {
    sqlx::query_as::<_, DataSourceRow>(
        r#"
        SELECT id, name, original_filename, stored_path, media_type, file_kind,
               size_bytes, selected_sheet, start_cell, first_row_as_header,
               sheet_names_json, row_count, column_count, created_at, updated_at
        FROM data_sources
        WHERE id = ? AND (? IS NULL OR workspace_id = ?)
        "#,
    )
    .bind(id)
    .bind(workspace_id)
    .bind(workspace_id)
    .fetch_optional(pool)
    .await
}

/// 按工作区读取逻辑表及其物理文件信息，集中权限条件可避免执行层绕过租户隔离。
pub async fn get_source_table(
    pool: &SqlitePool,
    id: &str,
    workspace_id: Option<&str>,
) -> Result<Option<SourceTableRow>, sqlx::Error> {
    sqlx::query_as::<_, SourceTableRow>(
        r#"
        SELECT t.id, t.source_id, d.name AS source_name, d.original_filename,
               d.stored_path, d.file_kind, t.name, t.sheet_name, t.start_cell,
               t.end_cell, t.first_row_as_header, t.row_count, t.column_count,
               t.schema_json, t.config_version, t.cache_status,
               t.cache_error, t.is_default, t.created_at, t.updated_at
        FROM source_tables t
        JOIN data_sources d ON d.id = t.source_id
        WHERE t.id = ? AND (? IS NULL OR d.workspace_id = ?)
        "#,
    )
    .bind(id)
    .bind(workspace_id)
    .bind(workspace_id)
    .fetch_optional(pool)
    .await
}

/// 获取文件的默认逻辑表，为旧版单文件请求自动补齐 data 绑定，保障平滑升级。
pub async fn get_default_source_table(
    pool: &SqlitePool,
    source_id: &str,
    workspace_id: Option<&str>,
) -> Result<Option<SourceTableRow>, sqlx::Error> {
    sqlx::query_as::<_, SourceTableRow>(
        r#"
        SELECT t.id, t.source_id, d.name AS source_name, d.original_filename,
               d.stored_path, d.file_kind, t.name, t.sheet_name, t.start_cell,
               t.end_cell, t.first_row_as_header, t.row_count, t.column_count,
               t.schema_json, t.config_version, t.cache_status,
               t.cache_error, t.is_default, t.created_at, t.updated_at
        FROM source_tables t
        JOIN data_sources d ON d.id = t.source_id
        WHERE t.source_id = ? AND t.is_default = 1
          AND (? IS NULL OR d.workspace_id = ?)
        "#,
    )
    .bind(source_id)
    .bind(workspace_id)
    .bind(workspace_id)
    .fetch_optional(pool)
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 在独立临时数据库运行全部迁移，Agent 表和活跃 Run 唯一索引必须可用。
    #[tokio::test]
    async fn migrates_agent_runtime_schema() {
        let directory = tempfile::tempdir().unwrap();
        let database = directory.path().join("agent-migration.db");
        let pool = connect(&format!("sqlite://{}", database.display()))
            .await
            .unwrap();
        let tables: i64 = sqlx::query_scalar(
            r#"
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('ai_conversations', 'ai_messages', 'ai_runs', 'ai_run_steps')
            "#,
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        let active_index: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'idx_ai_runs_one_active'",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        let reasoning_effort_column: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM pragma_table_info('ai_runs') WHERE name = 'reasoning_effort'",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        let job_artifact_columns: i64 = sqlx::query_scalar(
            r#"
            SELECT COUNT(*) FROM pragma_table_info('jobs')
            WHERE name IN (
                'result_artifact_key', 'result_artifact_format',
                'result_size_bytes', 'result_expires_at'
            )
            "#,
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        assert_eq!(tables, 4);
        assert_eq!(active_index, 1);
        assert_eq!(reasoning_effort_column, 1);
        assert_eq!(job_artifact_columns, 4);
    }
}
