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

use axum::{Json, Router, extract::DefaultBodyLimit, routing::get};
use tower_http::{
    services::{ServeDir, ServeFile},
    trace::TraceLayer,
};

use crate::{
    config::Config,
    models::{AppState, HealthResponse},
};

pub fn router(state: Arc<AppState>, config: &Config) -> Router {
    let api = Router::new()
        .route("/health", get(health))
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

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: "anydatas-api",
    })
}
