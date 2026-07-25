use std::collections::HashSet;

use sqlx::{SqliteConnection, SqlitePool};

use crate::{
    db,
    error::{AppError, AppResult},
    models::QueryTableBinding,
    services::query_engine,
};

#[derive(Debug)]
pub struct ValidatedBindings {
    pub primary_source_id: String,
    pub tables: Vec<QueryTableBinding>,
}

#[derive(Debug, Clone, Copy)]
pub enum BindingTarget {
    SavedQuery,
    Job,
    Schedule,
}

/// 校验并规范化新旧绑定请求，首张表决定兼容 source_id，旧客户端则自动绑定默认 data 表。
pub async fn validate_bindings(
    pool: &SqlitePool,
    workspace_id: &str,
    source_id: Option<&str>,
    tables: &[QueryTableBinding],
) -> AppResult<ValidatedBindings> {
    if tables.is_empty() {
        let source_id =
            source_id.ok_or_else(|| AppError::BadRequest("至少需要选择一张逻辑表".to_owned()))?;
        let table = db::get_default_source_table(pool, source_id, Some(workspace_id))
            .await?
            .ok_or_else(|| AppError::NotFound("数据文件的默认逻辑表不存在".to_owned()))?;
        return Ok(ValidatedBindings {
            primary_source_id: source_id.to_owned(),
            tables: vec![QueryTableBinding {
                table_id: table.id,
                alias: "data".to_owned(),
            }],
        });
    }
    if tables.len() > 16 {
        return Err(AppError::BadRequest(
            "单次查询最多绑定 16 张逻辑表".to_owned(),
        ));
    }
    let mut aliases = HashSet::new();
    let mut primary_source_id = None;
    for binding in tables {
        query_engine::validate_alias(&binding.alias)
            .map_err(|error| AppError::BadRequest(error.to_string()))?;
        if !aliases.insert(binding.alias.to_ascii_lowercase()) {
            return Err(AppError::BadRequest(format!(
                "查询表别名不能重复: {}",
                binding.alias
            )));
        }
        let table = db::get_source_table(pool, &binding.table_id, Some(workspace_id))
            .await?
            .ok_or_else(|| AppError::NotFound("绑定的逻辑表不存在".to_owned()))?;
        primary_source_id.get_or_insert(table.source_id);
    }
    Ok(ValidatedBindings {
        primary_source_id: primary_source_id.expect("non-empty bindings have a source"),
        tables: tables.to_vec(),
    })
}

/// 读取某个业务对象的有序表绑定，列表与执行端因此始终使用同一份别名快照。
pub async fn load_bindings(
    pool: &SqlitePool,
    target: BindingTarget,
    owner_id: &str,
) -> AppResult<Vec<QueryTableBinding>> {
    let (table_name, owner_column) = relation_names(target);
    let sql = format!(
        "SELECT source_table_id AS table_id, alias FROM {table_name} WHERE {owner_column} = ? ORDER BY ordinal"
    );
    Ok(sqlx::query_as::<_, QueryTableBinding>(&sql)
        .bind(owner_id)
        .fetch_all(pool)
        .await?)
}

/// 在所属对象事务内替换全部绑定，SQL 与别名不会出现只更新一半的不一致状态。
pub async fn replace_bindings(
    connection: &mut SqliteConnection,
    target: BindingTarget,
    owner_id: &str,
    tables: &[QueryTableBinding],
) -> AppResult<()> {
    let (table_name, owner_column) = relation_names(target);
    let delete_sql = format!("DELETE FROM {table_name} WHERE {owner_column} = ?");
    sqlx::query(&delete_sql)
        .bind(owner_id)
        .execute(&mut *connection)
        .await?;
    let insert_sql = format!(
        "INSERT INTO {table_name} ({owner_column}, source_table_id, alias, ordinal) VALUES (?, ?, ?, ?)"
    );
    for (ordinal, binding) in tables.iter().enumerate() {
        sqlx::query(&insert_sql)
            .bind(owner_id)
            .bind(&binding.table_id)
            .bind(&binding.alias)
            .bind(ordinal as i64)
            .execute(&mut *connection)
            .await?;
    }
    Ok(())
}

/// 将枚举映射到固定表名，动态 SQL 只使用程序常量，不接收任何用户输入。
fn relation_names(target: BindingTarget) -> (&'static str, &'static str) {
    match target {
        BindingTarget::SavedQuery => ("saved_query_tables", "saved_query_id"),
        BindingTarget::Job => ("job_tables", "job_id"),
        BindingTarget::Schedule => ("schedule_tables", "schedule_id"),
    }
}
