mod agent;
mod ai;
pub(crate) mod auth;
mod data_source_replacement;
mod data_sources;
pub(crate) mod jobs;
mod metrics;
mod queries;
mod saved_queries;
pub(crate) mod schedules;
pub(crate) mod source_tables;

use std::sync::{Arc, atomic::Ordering};

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Request, State},
    http::{
        HeaderValue,
        header::{HeaderName, X_CONTENT_TYPE_OPTIONS, X_FRAME_OPTIONS},
    },
    middleware::{self, Next},
    response::Response,
    routing::get,
};
use tower_http::{
    request_id::{MakeRequestUuid, PropagateRequestIdLayer, SetRequestIdLayer},
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
        .merge(data_sources::router(config.max_upload_bytes))
        .merge(source_tables::router())
        .merge(queries::router())
        .merge(saved_queries::router())
        .merge(jobs::router())
        .merge(metrics::router())
        .merge(schedules::router())
        .layer(DefaultBodyLimit::max(2 * 1024 * 1024))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            observe_request,
        ))
        .with_state(state);
    let static_files =
        ServeDir::new(&config.web_dir).fallback(ServeFile::new(config.web_dir.join("index.html")));

    Router::new()
        .nest("/api", api)
        .fallback_service(static_files)
        .layer(middleware::from_fn(security_headers))
        .layer(PropagateRequestIdLayer::x_request_id())
        .layer(SetRequestIdLayer::x_request_id(MakeRequestUuid))
        .layer(TraceLayer::new_for_http())
}

/// 记录低基数 HTTP 计数；请求详情继续交给 TraceLayer，指标不会暴露路径或租户信息。
async fn observe_request(
    State(state): State<Arc<AppState>>,
    request: Request,
    next: Next,
) -> Response {
    state
        .metrics
        .http_requests_total
        .fetch_add(1, Ordering::Relaxed);
    let response = next.run(request).await;
    if response.status().is_server_error() {
        state
            .metrics
            .http_server_errors_total
            .fetch_add(1, Ordering::Relaxed);
    }
    response
}

/// 为 API 和静态页面统一添加浏览器安全边界，同时保留 Monaco 所需的 Blob Worker。
async fn security_headers(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    headers.insert(X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(
        HeaderName::from_static("referrer-policy"),
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(
        HeaderName::from_static("permissions-policy"),
        HeaderValue::from_static("camera=(), microphone=(), geolocation=()"),
    );
    headers.insert(
        HeaderName::from_static("content-security-policy"),
        HeaderValue::from_static(
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; \
             form-action 'self'; img-src 'self' data: blob:; font-src 'self' data:; \
             style-src 'self' 'unsafe-inline'; script-src 'self'; worker-src 'self' blob:; \
             connect-src 'self'",
        ),
    );
    response
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
