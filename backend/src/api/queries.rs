use axum::{Json, Router, extract::State, routing::post};

use crate::{
    api::auth::AuthContext,
    error::AppResult,
    models::{QueryRequest, QueryResponse, SharedState},
    services::execution,
};

pub fn router() -> Router<SharedState> {
    Router::new().route("/query", post(run))
}

async fn run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(request): Json<QueryRequest>,
) -> AppResult<Json<QueryResponse>> {
    auth.require_analyst()?;
    Ok(Json(
        execution::execute_request(state, &request, &auth.workspace_id).await?,
    ))
}
