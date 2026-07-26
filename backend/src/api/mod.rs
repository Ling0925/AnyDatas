mod agent;
mod ai;
pub(crate) mod auth;
mod data_sources;
pub(crate) mod jobs;
mod queries;
mod saved_queries;
pub(crate) mod schedules;
pub(crate) mod source_tables;

use std::sync::Arc;

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, State},
    routing::get,
};
use tower_http::{
    services::{ServeDir, ServeFile},
    trace::TraceLayer,
};

use crate::{
    config::Config,
    error::{AppError, AppResult},
    models::{AppState, HealthResponse},
    services::maintenance,
};

pub fn router(state: Arc<AppState>, config: &Config) -> Router {
    let api = Router::new()
        .route("/health", get(readiness))
        .route("/livez", get(liveness))
        .route("/readyz", get(readiness))
        .merge(auth::router())
        .merge(ai::router())
        .merge(agent::router())
        .merge(data_sources::router())
        .merge(source_tables::router())
        .merge(queries::router())
        .merge(saved_queries::router())
        .merge(jobs::router())
        .merge(schedules::router())
        .with_state(state);
    let static_files =
        ServeDir::new(&config.web_dir).fallback(ServeFile::new(config.web_dir.join("index.html")));

    Router::new()
        .nest("/api", api)
        .fallback_service(static_files)
        .layer(DefaultBodyLimit::max(config.max_upload_bytes))
        .layer(TraceLayer::new_for_http())
}

/// 存活探针只证明进程事件循环仍可响应，避免数据库短暂忙碌触发无意义重启。
async fn liveness() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: "anydatas-api",
    })
}

/// 就绪探针验证 SQLite 和数据卷可读写，负载均衡器只会把真实可用实例投入服务。
async fn readiness(State(state): State<Arc<AppState>>) -> AppResult<Json<HealthResponse>> {
    tokio::time::timeout(
        std::time::Duration::from_secs(2),
        sqlx::query_scalar::<_, i64>("SELECT 1").fetch_one(&state.pool),
    )
    .await
    .map_err(|_| AppError::Unavailable("数据库就绪检查超时".to_owned()))?
    .map_err(|_| AppError::Unavailable("数据库当前不可用".to_owned()))?;

    maintenance::ensure_free_space(&state.data_dir, state.query_runtime.min_free_space_bytes, 0)
        .map_err(|_| AppError::Unavailable("数据卷剩余空间不足".to_owned()))?;
    let probe = state
        .data_dir
        .join(format!(".ready-{}", uuid::Uuid::new_v4()));
    tokio::fs::write(&probe, b"ready")
        .await
        .map_err(|_| AppError::Unavailable("数据卷不可写".to_owned()))?;
    tokio::fs::remove_file(&probe)
        .await
        .map_err(|_| AppError::Unavailable("数据卷清理失败".to_owned()))?;
    Ok(Json(HealthResponse {
        status: "ok",
        service: "anydatas-api",
    }))
}
