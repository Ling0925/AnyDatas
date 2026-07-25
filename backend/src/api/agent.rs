use std::{convert::Infallible, time::Duration};

use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::sse::{Event, KeepAlive, Sse},
    routing::{get, post, put},
};
use futures_util::{Stream, stream};
use tokio::time::sleep;

use crate::{
    api::auth::AuthContext,
    error::AppResult,
    models::SharedState,
    services::agent::{
        self, AgentConversationDetail, AgentConversationSummary, AgentIdentity, AgentRun,
        CreateConversationRequest, RegenerateAgentRunRequest, StartAgentRunRequest,
        UpdateConversationContextRequest,
    },
};

/// 挂载服务端 Agent API；旧 `/ai/chat` 保留兼容，新工作台只使用这些持久化资源。
pub fn router() -> Router<SharedState> {
    Router::new()
        .route(
            "/ai/agent/conversations",
            get(list_conversations).post(create_conversation),
        )
        .route(
            "/ai/agent/conversations/{id}",
            get(get_conversation).delete(archive_conversation),
        )
        .route(
            "/ai/agent/conversations/{id}/context",
            put(update_conversation_context),
        )
        .route("/ai/agent/conversations/{id}/runs", post(start_run))
        .route(
            "/ai/agent/conversations/{id}/regenerate",
            post(regenerate_run),
        )
        .route("/ai/agent/runs/{id}", get(get_run))
        .route("/ai/agent/runs/{id}/events", get(stream_run))
        .route("/ai/agent/runs/{id}/cancel", post(cancel_run))
        .route("/ai/agent/runs/{id}/retry", post(retry_run))
}

/// 返回当前用户的活跃会话列表，其他工作区或成员的记录不会进入结果集。
async fn list_conversations(
    State(state): State<SharedState>,
    auth: AuthContext,
) -> AppResult<Json<Vec<AgentConversationSummary>>> {
    auth.require_analyst()?;
    Ok(Json(
        agent::list_conversations(&state, &identity(&auth)).await?,
    ))
}

/// 创建一个绑定当前逻辑表快照的空会话，随后发送消息时沿用该上下文签名。
async fn create_conversation(
    State(state): State<SharedState>,
    auth: AuthContext,
    Json(request): Json<CreateConversationRequest>,
) -> AppResult<(StatusCode, Json<AgentConversationDetail>)> {
    auth.require_analyst()?;
    Ok((
        StatusCode::CREATED,
        Json(agent::create_conversation(&state, &identity(&auth), request).await?),
    ))
}

/// 读取会话、消息及最近 Run，供工作台刷新恢复和运行状态同步。
async fn get_conversation(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<AgentConversationDetail>> {
    auth.require_analyst()?;
    Ok(Json(
        agent::get_conversation(&state, &identity(&auth), &id).await?,
    ))
}

/// 归档指定会话；采用 DELETE 语义简化客户端，但后端保留完整审计数据。
async fn archive_conversation(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<StatusCode> {
    auth.require_analyst()?;
    agent::archive_conversation(&state, &identity(&auth), &id).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 显式切换会话数据上下文，避免表绑定或读取配置变化后静默沿用旧 Schema。
async fn update_conversation_context(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(request): Json<UpdateConversationContextRequest>,
) -> AppResult<Json<AgentConversationDetail>> {
    auth.require_analyst()?;
    Ok(Json(
        agent::update_conversation_context(&state, &identity(&auth), &id, request).await?,
    ))
}

/// 创建异步 Run 并立即返回 202，模型和工具步骤由后台运行时持续写入数据库。
async fn start_run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(request): Json<StartAgentRunRequest>,
) -> AppResult<(StatusCode, Json<AgentRun>)> {
    auth.require_analyst()?;
    Ok((
        StatusCode::ACCEPTED,
        Json(agent::start_run(&state, &identity(&auth), &id, request).await?),
    ))
}

/// 从指定助手消息创建新分支，历史分支在服务端标记而无需浏览器重发整段对话。
async fn regenerate_run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
    Json(request): Json<RegenerateAgentRunRequest>,
) -> AppResult<(StatusCode, Json<AgentRun>)> {
    auth.require_analyst()?;
    Ok((
        StatusCode::ACCEPTED,
        Json(agent::regenerate_run(&state, &identity(&auth), &id, request).await?),
    ))
}

/// 获取一个 Run 的终态和结构化步骤，用于短轮询和历史执行轨迹展示。
async fn get_run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<AgentRun>> {
    auth.require_analyst()?;
    Ok(Json(agent::get_run(&state, &identity(&auth), &id).await?))
}

struct RunStreamCursor {
    state: SharedState,
    identity: AgentIdentity,
    run_id: String,
    pending_run: Option<AgentRun>,
    last_updated_at: Option<String>,
    finished: bool,
}

/**
 * 持续推送已持久化的 Run 快照；上游分片和工具状态使用同一事件格式，断线后可安全重连。
 * 只有 updated_at 变化时才发送完整快照，避免空闲模型等待期间重复传输工具结果。
 */
async fn stream_run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Sse<impl Stream<Item = Result<Event, Infallible>>>> {
    auth.require_analyst()?;
    let identity = identity(&auth);
    let initial_run = agent::get_run(&state, &identity, &id).await?;
    let cursor = RunStreamCursor {
        state,
        identity,
        run_id: id,
        pending_run: Some(initial_run),
        last_updated_at: None,
        finished: false,
    };
    let events = stream::unfold(cursor, |mut cursor| async move {
        if cursor.finished {
            return None;
        }
        loop {
            let run = match cursor.pending_run.take() {
                Some(run) => Ok(run),
                None => {
                    sleep(Duration::from_millis(250)).await;
                    agent::get_run(&cursor.state, &cursor.identity, &cursor.run_id).await
                }
            };
            let run = match run {
                Ok(run) => run,
                Err(error) => {
                    cursor.finished = true;
                    let payload = serde_json::json!({ "message": error.to_string() }).to_string();
                    return Some((
                        Ok(Event::default().event("run-error").data(payload)),
                        cursor,
                    ));
                }
            };
            let changed = cursor.last_updated_at.as_deref() != Some(run.updated_at.as_str());
            let terminal = !matches!(run.status.as_str(), "queued" | "running");
            if changed {
                cursor.last_updated_at = Some(run.updated_at.clone());
                cursor.finished = terminal;
                let payload = serde_json::to_string(&run)
                    .unwrap_or_else(|_| "{\"errorMessage\":\"Run 序列化失败\"}".to_owned());
                return Some((Ok(Event::default().event("run").data(payload)), cursor));
            }
            if terminal {
                return None;
            }
        }
    });
    Ok(Sse::new(events).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(10))
            .text("keep-alive"),
    ))
}

/// 停止模型等待和当前 DuckDB 工具查询，并返回已经固化的取消状态。
async fn cancel_run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<Json<AgentRun>> {
    auth.require_analyst()?;
    Ok(Json(
        agent::cancel_run(&state, &identity(&auth), &id).await?,
    ))
}

/// 重试最近失败或停止的 Run，不新增重复用户消息。
async fn retry_run(
    State(state): State<SharedState>,
    auth: AuthContext,
    Path(id): Path<String>,
) -> AppResult<(StatusCode, Json<AgentRun>)> {
    auth.require_analyst()?;
    Ok((
        StatusCode::ACCEPTED,
        Json(agent::retry_run(&state, &identity(&auth), &id).await?),
    ))
}

/// 把认证上下文收缩为 Runtime 所需字段，避免后台任务意外依赖会话 Cookie 生命周期。
fn identity(auth: &AuthContext) -> AgentIdentity {
    AgentIdentity {
        user_id: auth.user_id.clone(),
        workspace_id: auth.workspace_id.clone(),
        workspace_name: auth.workspace_name.clone(),
    }
}
