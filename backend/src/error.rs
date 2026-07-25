use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("{0}")]
    BadRequest(String),
    #[error("{0}")]
    NotFound(String),
    #[error("{0}")]
    Conflict(String),
    #[error("{0}")]
    Unauthorized(String),
    #[error("{0}")]
    Forbidden(String),
    #[error("{0}")]
    RateLimited(String),
    #[error("数据库操作失败")]
    Database(#[from] sqlx::Error),
    #[error("文件操作失败")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Internal(String),
}

#[derive(Serialize)]
struct ErrorEnvelope {
    error: ErrorBody,
}

#[derive(Serialize)]
struct ErrorBody {
    code: &'static str,
    message: String,
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, code, message) = match self {
            Self::BadRequest(message) => (StatusCode::BAD_REQUEST, "bad_request", message),
            Self::NotFound(message) => (StatusCode::NOT_FOUND, "not_found", message),
            Self::Conflict(message) => (StatusCode::CONFLICT, "conflict", message),
            Self::Unauthorized(message) => (StatusCode::UNAUTHORIZED, "unauthorized", message),
            Self::Forbidden(message) => (StatusCode::FORBIDDEN, "forbidden", message),
            Self::RateLimited(message) => (StatusCode::TOO_MANY_REQUESTS, "rate_limited", message),
            Self::Database(error) => {
                tracing::error!(?error, "database request failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "database_error",
                    "数据库操作失败".to_owned(),
                )
            }
            Self::Io(error) => {
                tracing::error!(?error, "file request failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "io_error",
                    "文件操作失败".to_owned(),
                )
            }
            Self::Internal(message) => {
                tracing::error!(%message, "request failed");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal_error", message)
            }
        };
        (
            status,
            Json(ErrorEnvelope {
                error: ErrorBody { code, message },
            }),
        )
            .into_response()
    }
}

pub type AppResult<T> = Result<T, AppError>;
