use axum::{
    Json,
    extract::{Multipart, Path, State},
    http::{HeaderMap, StatusCode},
};
use uuid::Uuid;

use crate::{
    api::{
        auth::AuthContext,
        data_sources::{StoreMultipartOptions, required_source, store_multipart_file},
    },
    error::{AppError, AppResult},
    models::{DataSource, SharedState, SourceRefreshReceipt},
    services::source_refresh::{self, IncomingRevisionFile, RefreshRequest},
};

pub(super) async fn replace(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    multipart: Multipart,
) -> AppResult<Json<DataSource>> {
    auth.require_analyst()?;
    required_source(&state, &id, &auth.workspace_id).await?;
    let stored = store_multipart_file(
        &state,
        multipart,
        StoreMultipartOptions {
            directory: &state.data_dir.join("staging"),
            file_id: &Uuid::new_v4().to_string(),
            reject_tables: true,
        },
    )
    .await?;
    let staged_path = stored.path.clone();
    let result = source_refresh::refresh_source(
        &state,
        RefreshRequest {
            workspace_id: auth.workspace_id.clone(),
            source_id: id.clone(),
            idempotency_key: format!("legacy-replace:{}", Uuid::new_v4()),
            saved_query_id: None,
        },
        into_revision_file(stored),
    )
    .await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(staged_path).await;
    }
    result?;
    Ok(Json(
        required_source(&state, &id, &auth.workspace_id)
            .await?
            .into(),
    ))
}

pub(super) async fn refresh(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    headers: HeaderMap,
    multipart: Multipart,
) -> AppResult<(StatusCode, Json<SourceRefreshReceipt>)> {
    auth.require_analyst()?;
    required_source(&state, &id, &auth.workspace_id).await?;
    let idempotency_key = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AppError::BadRequest("缺少 Idempotency-Key 请求头".to_owned()))?
        .to_owned();
    let stored = store_multipart_file(
        &state,
        multipart,
        StoreMultipartOptions {
            directory: &state.data_dir.join("staging"),
            file_id: &Uuid::new_v4().to_string(),
            reject_tables: true,
        },
    )
    .await?;
    let saved_query_id = stored
        .extra_fields
        .get("savedQueryId")
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AppError::BadRequest("缺少 savedQueryId 字段".to_owned()));
    let saved_query_id = match saved_query_id {
        Ok(value) => value,
        Err(error) => {
            let _ = tokio::fs::remove_file(&stored.path).await;
            return Err(error);
        }
    };
    let staged_path = stored.path.clone();
    let result = source_refresh::refresh_source(
        &state,
        RefreshRequest {
            workspace_id: auth.workspace_id,
            source_id: id,
            idempotency_key,
            saved_query_id: Some(saved_query_id),
        },
        into_revision_file(stored),
    )
    .await;
    if result.is_err() {
        let _ = tokio::fs::remove_file(staged_path).await;
    }
    let receipt = result?;
    let status = if receipt.unchanged {
        StatusCode::OK
    } else {
        StatusCode::CREATED
    };
    Ok((status, Json(receipt)))
}

fn into_revision_file(stored: crate::api::data_sources::StoredUpload) -> IncomingRevisionFile {
    IncomingRevisionFile {
        original_filename: stored.original_filename,
        file_kind: stored.file_kind.to_owned(),
        media_type: stored.media_type.to_owned(),
        staged_path: stored.path,
        size_bytes: stored.size_bytes as u64,
        content_sha256: stored.content_sha256,
    }
}
