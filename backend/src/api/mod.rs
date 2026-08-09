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
        HeaderValue, StatusCode,
        header::{HeaderName, X_CONTENT_TYPE_OPTIONS, X_FRAME_OPTIONS},
    },
    middleware::{self, Next},
    response::Response,
    routing::get,
};
use subtle::ConstantTimeEq;
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

const DESKTOP_PROTOCOL_VERSION: u32 = 1;
const DESKTOP_TOKEN_HEADER: &str = "x-anydatas-desktop-token";

#[derive(Debug, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopHandshake {
    service: &'static str,
    server_version: &'static str,
    protocol_version: u32,
    capabilities: &'static [&'static str],
}

pub fn router(state: Arc<AppState>, config: &Config) -> Router {
    let api = Router::new()
        .route("/health", get(readiness))
        .route("/livez", get(liveness))
        .route("/readyz", get(readiness))
        .route("/desktop-handshake", get(desktop_handshake))
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

    let router = Router::new()
        .nest("/api", api)
        .fallback_service(static_files)
        .layer(middleware::from_fn(security_headers))
        .layer(PropagateRequestIdLayer::x_request_id())
        .layer(SetRequestIdLayer::x_request_id(MakeRequestUuid))
        .layer(TraceLayer::new_for_http());

    // 单机模式由 Electron 代理注入高熵令牌；远端部署不配置令牌时保持原有网络接口。
    // 把校验放在最外层的好处是静态页面和 API 都无法绕过同一条本机进程访问规则。
    match config.desktop_token.as_deref() {
        Some(token) => router.layer(middleware::from_fn_with_state(
            Arc::<str>::from(token),
            require_desktop_token,
        )),
        None => router,
    }
}

/// 返回桌面端与服务端之间稳定的兼容协议和能力集合。
///
/// 独立于产品版本的协议号让远端连接可以在登录前快速拒绝不兼容服务端，避免登录后才出现零散接口错误。
async fn desktop_handshake() -> Json<DesktopHandshake> {
    Json(DesktopHandshake {
        service: "anydatas-server",
        server_version: env!("CARGO_PKG_VERSION"),
        protocol_version: DESKTOP_PROTOCOL_VERSION,
        capabilities: &["file-sources", "agent", "post-js"],
    })
}

/// 比较代理传入的桌面令牌，并使用固定时间比较降低令牌前缀侧信道风险。
///
/// 令牌只存在于主进程与子进程之间，统一校验可以阻止同一台电脑上的普通网页直接访问随机回环端口。
async fn require_desktop_token(
    State(expected): State<Arc<str>>,
    request: Request,
    next: Next,
) -> Result<Response, StatusCode> {
    let actual = request
        .headers()
        .get(DESKTOP_TOKEN_HEADER)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if !bool::from(expected.as_bytes().ct_eq(actual.as_bytes())) {
        return Err(StatusCode::UNAUTHORIZED);
    }
    Ok(next.run(request).await)
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

#[cfg(test)]
mod tests {
    use super::*;

    /// 握手必须使用独立协议版本，不把 Cargo 的产品版本误当作接口兼容依据。
    ///
    /// 该断言的好处是后续服务端补丁发布不会无意中破坏桌面端选择逻辑。
    #[tokio::test]
    async fn desktop_handshake_exposes_protocol_and_version() {
        let payload = desktop_handshake().await.0;
        assert_eq!(payload.service, "anydatas-server");
        assert_eq!(payload.server_version, env!("CARGO_PKG_VERSION"));
        assert_eq!(payload.protocol_version, 1);
        assert!(payload.capabilities.contains(&"file-sources"));
    }
}
