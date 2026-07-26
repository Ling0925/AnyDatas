use axum::{
    Json, Router,
    extract::State,
    routing::{get, post},
};
use chrono::Utc;
use reqwest::Url;
use serde::{Deserialize, Serialize};
use sqlx::FromRow;

use crate::{
    api::auth::AuthContext,
    error::{AppError, AppResult},
    models::SharedState,
    services::{agent_provider, secrets},
};

const DEFAULT_BASE_URL: &str = "https://api.openai.com/v1";

/// 挂载工作区 AI 配置接口；实际对话统一由持久化 Agent Runtime 提供。
pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/ai/settings", get(get_settings).put(update_settings))
        .route("/ai/settings/test", post(test_settings))
}

#[derive(Debug, Clone, FromRow)]
struct AiSettingsRow {
    enabled: bool,
    base_url: String,
    model: String,
    api_key_ciphertext: Option<String>,
    updated_at: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AiSettingsResponse {
    enabled: bool,
    base_url: String,
    model: String,
    api_key_configured: bool,
    updated_at: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UpdateAiSettingsRequest {
    enabled: bool,
    base_url: String,
    model: String,
    #[serde(default)]
    api_key: Option<String>,
    #[serde(default)]
    clear_api_key: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AiConnectionResponse {
    ok: bool,
    model: String,
}

/// 返回工作区 AI 配置摘要；API Key 只暴露是否存在，永不返回密文或明文。
async fn get_settings(
    State(state): State<SharedState>,
    auth: AuthContext,
) -> AppResult<Json<AiSettingsResponse>> {
    Ok(Json(settings_response(
        load_settings(&state, &auth.workspace_id).await?,
    )))
}

/**
 * 保存工作区 OpenAI-compatible Chat 配置，空白 API Key 表示保留已有密钥。
 * 地址在落库前执行网络边界校验，可避免管理员误把 Agent 配置成内部敏感服务。
 */
async fn update_settings(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(request): Json<UpdateAiSettingsRequest>,
) -> AppResult<Json<AiSettingsResponse>> {
    auth.require_admin()?;
    let base_url = normalize_base_url(&request.base_url)?;
    agent_provider::validate_base_url_network(&state, &base_url).await?;
    let model = normalize_model(&request.model, request.enabled)?;
    let existing = load_settings(&state, &auth.workspace_id).await?;
    let api_key_ciphertext = if request.clear_api_key {
        None
    } else if let Some(api_key) = request
        .api_key
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        if api_key.chars().count() > 4_096 {
            return Err(AppError::BadRequest("API Key 长度无效".to_owned()));
        }
        Some(
            secrets::encrypt(&state.secret_key, api_key)
                .map_err(|error| AppError::Internal(error.to_string()))?,
        )
    } else {
        existing.and_then(|settings| settings.api_key_ciphertext)
    };
    let now = Utc::now().to_rfc3339();
    sqlx::query(
        r#"
        INSERT INTO workspace_ai_settings (
            workspace_id, enabled, base_url, model, api_key_ciphertext,
            updated_by_user_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            enabled = excluded.enabled,
            base_url = excluded.base_url,
            model = excluded.model,
            api_key_ciphertext = excluded.api_key_ciphertext,
            updated_by_user_id = excluded.updated_by_user_id,
            updated_at = excluded.updated_at
        "#,
    )
    .bind(&auth.workspace_id)
    .bind(request.enabled)
    .bind(base_url)
    .bind(model)
    .bind(api_key_ciphertext)
    .bind(&auth.user_id)
    .bind(&now)
    .bind(&now)
    .execute(&state.pool)
    .await?;
    Ok(Json(settings_response(
        load_settings(&state, &auth.workspace_id).await?,
    )))
}

/// 复用正式 Agent Provider 测试已保存配置，防止设置页成功而实际运行走另一套协议。
async fn test_settings(
    State(state): State<SharedState>,
    auth: AuthContext,
) -> AppResult<Json<AiConnectionResponse>> {
    auth.require_admin()?;
    let model = agent_provider::test_connection(&state, &auth.workspace_id).await?;
    Ok(Json(AiConnectionResponse { ok: true, model }))
}

/// 从数据库读取工作区设置；没有记录时由前端展示默认地址但保持关闭状态。
async fn load_settings(
    state: &SharedState,
    workspace_id: &str,
) -> AppResult<Option<AiSettingsRow>> {
    Ok(sqlx::query_as::<_, AiSettingsRow>(
        r#"
        SELECT enabled, base_url, model, api_key_ciphertext, updated_at
        FROM workspace_ai_settings
        WHERE workspace_id = ?
        "#,
    )
    .bind(workspace_id)
    .fetch_optional(&state.pool)
    .await?)
}

/// 把数据库记录投影成无密钥响应；新工作区默认关闭，避免意外产生外部模型请求。
fn settings_response(settings: Option<AiSettingsRow>) -> AiSettingsResponse {
    match settings {
        Some(settings) => AiSettingsResponse {
            enabled: settings.enabled,
            base_url: settings.base_url,
            model: settings.model,
            api_key_configured: settings.api_key_ciphertext.is_some(),
            updated_at: Some(settings.updated_at),
        },
        None => AiSettingsResponse {
            enabled: false,
            base_url: DEFAULT_BASE_URL.to_owned(),
            model: String::new(),
            api_key_configured: false,
            updated_at: None,
        },
    }
}

/// 规范并校验 Chat 服务地址，只接受 HTTP(S) 且拒绝在 URL 中嵌入账号密码。
fn normalize_base_url(value: &str) -> AppResult<String> {
    let value = value.trim().trim_end_matches('/');
    if value.is_empty() || value.chars().count() > 500 {
        return Err(AppError::BadRequest("Base URL 长度无效".to_owned()));
    }
    let url =
        Url::parse(value).map_err(|_| AppError::BadRequest("Base URL 格式无效".to_owned()))?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(AppError::BadRequest(
            "Base URL 必须是 HTTP 或 HTTPS 地址".to_owned(),
        ));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(AppError::BadRequest(
            "Base URL 不能包含账号或密码".to_owned(),
        ));
    }
    Ok(value.to_owned())
}

/// 统一模型名称边界；启用时要求明确模型，关闭配置仍可先保存地址。
fn normalize_model(value: &str, required: bool) -> AppResult<String> {
    let value = value.trim();
    if required && value.is_empty() {
        return Err(AppError::BadRequest(
            "启用 AI 前必须填写模型名称".to_owned(),
        ));
    }
    if value.chars().count() > 160 {
        return Err(AppError::BadRequest(
            "模型名称不能超过 160 个字符".to_owned(),
        ));
    }
    Ok(value.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_saved_provider_values() {
        assert_eq!(
            normalize_base_url(" https://api.openai.com/v1/ ").unwrap(),
            "https://api.openai.com/v1"
        );
        assert_eq!(normalize_model(" gpt-test ", true).unwrap(), "gpt-test");
    }

    #[test]
    fn rejects_credentialed_or_incomplete_provider_values() {
        assert!(normalize_base_url("https://user:secret@example.com/v1").is_err());
        assert!(normalize_model("", true).is_err());
        assert!(normalize_model("", false).is_ok());
    }
}
