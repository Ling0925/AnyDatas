mod api;
mod config;
mod db;
mod error;
mod models;
mod services;
mod workers;

use std::sync::Arc;

use anyhow::Context;
use config::Config;
use models::AppState;
use tokio::net::TcpListener;
use tracing::info;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok();
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "anydatas_api=info,tower_http=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    let config = Config::from_env()?;
    tokio::fs::create_dir_all(config.upload_dir()).await?;
    tokio::fs::create_dir_all(config.staging_dir()).await?;
    let secret_key = services::secrets::load_or_create(&config.data_dir)?;
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(90))
        .build()?;
    let pool = db::connect(&config.database_url).await?;
    db::recover_interrupted_jobs(&pool).await?;
    db::recover_interrupted_agent_runs(&pool).await?;

    let state = Arc::new(AppState {
        pool,
        data_dir: config.data_dir.clone(),
        max_upload_bytes: config.max_upload_bytes,
        session_ttl_days: config.session_ttl_days,
        cookie_secure: config.cookie_secure,
        secret_key,
        http_client,
        query_control: Default::default(),
        cache_build_lock: Default::default(),
        agent_control: Default::default(),
        agent_max_steps: config.agent_max_steps,
        agent_timeout_seconds: config.agent_timeout_seconds,
        agent_context_chars: config.agent_context_chars,
    });

    workers::spawn_job_worker(state.clone());
    workers::spawn_schedule_worker(state.clone());

    let app = api::router(state, &config);
    let listener = TcpListener::bind(&config.bind)
        .await
        .with_context(|| format!("failed to bind {}", config.bind))?;
    info!(address = %config.bind, "AnyDatas API started");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl+C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install signal handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}
