use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

use reqwest::{Url, header::CONTENT_TYPE};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::FromRow;
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::{
    error::{AppError, AppResult},
    models::SharedState,
    services::secrets,
};

const MAX_STREAM_LINE_BYTES: usize = 8_000_000;
const STREAM_EMIT_INTERVAL: Duration = Duration::from_millis(100);

#[derive(FromRow)]
struct SettingsRow {
    enabled: bool,
    base_url: String,
    model: String,
    api_key_ciphertext: Option<String>,
}

/// Agent 运行期间使用的不可变模型配置，API Key 只存在于当前进程内存中。
pub struct AgentModelSettings {
    pub base_url: String,
    pub model: String,
    api_key: Option<String>,
    reasoning_effort_supported: AtomicBool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ModelMessage {
    role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    content: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    tool_calls: Vec<ModelToolCall>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_call_id: Option<String>,
}

impl ModelMessage {
    /// 构造系统指令消息，规则和不受信任的数据上下文因此保持独立。
    pub fn system(content: impl Into<String>) -> Self {
        Self::plain("system", content)
    }

    /// 构造用户消息，既用于真实输入，也用于明确标记的工作区上下文。
    pub fn user(content: impl Into<String>) -> Self {
        Self::plain("user", content)
    }

    /// 构造历史助手消息，不携带旧 Run 的原生工具调用，避免重放已完成动作。
    pub fn assistant_text(content: impl Into<String>) -> Self {
        Self::plain("assistant", content)
    }

    /// 把当前模型决策原样加入同一 Run，后续工具结果可通过 call id 精确对应。
    pub fn assistant_turn(turn: &AssistantTurn) -> Self {
        Self {
            role: "assistant".to_owned(),
            content: (!turn.content.is_empty()).then(|| turn.content.clone()),
            tool_calls: turn.tool_calls.clone(),
            tool_call_id: None,
        }
    }

    /// 构造标准 Chat Completions 工具消息，模型可以在下一步观察结构化结果。
    pub fn tool(tool_call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: "tool".to_owned(),
            content: Some(content.into()),
            tool_calls: Vec::new(),
            tool_call_id: Some(tool_call_id.into()),
        }
    }

    /// 统一构造不带工具字段的普通消息，减少各角色序列化差异。
    fn plain(role: &str, content: impl Into<String>) -> Self {
        Self {
            role: role.to_owned(),
            content: Some(content.into()),
            tool_calls: Vec::new(),
            tool_call_id: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelToolCall {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub function: ModelFunctionCall,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelFunctionCall {
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ToolDefinition {
    #[serde(rename = "type")]
    kind: &'static str,
    function: FunctionDefinition,
}

#[derive(Debug, Clone, Serialize)]
struct FunctionDefinition {
    name: &'static str,
    description: &'static str,
    parameters: Value,
}

impl ToolDefinition {
    /// 从受信任的静态名称、说明和 JSON Schema 构造 OpenAI function tool。
    pub fn function(name: &'static str, description: &'static str, parameters: Value) -> Self {
        Self {
            kind: "function",
            function: FunctionDefinition {
                name,
                description,
                parameters,
            },
        }
    }
}

#[derive(Debug, Clone)]
pub struct AssistantTurn {
    pub content: String,
    pub tool_calls: Vec<ModelToolCall>,
    pub finish_reason: Option<String>,
}

/// 上游文本流的可公开快照；Runtime 会节流持久化，前端断线后仍可恢复当前内容。
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AssistantStreamUpdate {
    pub content: String,
}

#[derive(Serialize)]
struct ChatCompletionRequest<'a> {
    model: &'a str,
    messages: &'a [ModelMessage],
    stream: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    reasoning_effort: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tools: Option<&'a [ToolDefinition]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_choice: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    parallel_tool_calls: Option<bool>,
}

#[derive(Deserialize)]
struct ChatCompletionResponse {
    choices: Vec<ChatChoice>,
}

#[derive(Deserialize)]
struct ChatChoice {
    message: ChatResponseMessage,
    finish_reason: Option<String>,
}

#[derive(Deserialize)]
struct ChatResponseMessage {
    content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<ModelToolCall>,
}

#[derive(Deserialize)]
struct ChatCompletionChunk {
    #[serde(default)]
    choices: Vec<ChatChunkChoice>,
    error: Option<ChatStreamError>,
}

#[derive(Deserialize)]
struct ChatChunkChoice {
    delta: ChatDelta,
    finish_reason: Option<String>,
}

#[derive(Default, Deserialize)]
struct ChatDelta {
    content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<ToolCallDelta>,
}

#[derive(Deserialize)]
struct ToolCallDelta {
    index: usize,
    id: Option<String>,
    #[serde(rename = "type")]
    kind: Option<String>,
    function: Option<FunctionCallDelta>,
}

#[derive(Deserialize)]
struct FunctionCallDelta {
    name: Option<String>,
    arguments: Option<String>,
}

#[derive(Deserialize)]
struct ChatStreamError {
    message: String,
}

#[derive(Default)]
struct ToolCallBuilder {
    id: String,
    kind: String,
    name: String,
    arguments: String,
}

#[derive(Default)]
struct StreamAccumulator {
    content: String,
    tool_calls: BTreeMap<usize, ToolCallBuilder>,
    finish_reason: Option<String>,
}

impl StreamAccumulator {
    /// 合并一个流事件中的文字和分片工具参数，同一 index 始终归并为一个函数调用。
    fn append_chunk(&mut self, chunk: ChatCompletionChunk) -> AppResult<()> {
        if let Some(error) = chunk.error {
            return Err(AppError::BadRequest(format!(
                "AI 接口返回错误: {}",
                error.message
            )));
        }
        for choice in chunk.choices {
            if let Some(content) = choice.delta.content {
                self.content.push_str(&content);
            }
            if choice.finish_reason.is_some() {
                self.finish_reason = choice.finish_reason;
            }
            for delta in choice.delta.tool_calls {
                let builder = self.tool_calls.entry(delta.index).or_default();
                if let Some(id) = delta.id {
                    builder.id.push_str(&id);
                }
                if let Some(kind) = delta.kind {
                    builder.kind.push_str(&kind);
                }
                if let Some(function) = delta.function {
                    if let Some(name) = function.name {
                        builder.name.push_str(&name);
                    }
                    if let Some(arguments) = function.arguments {
                        builder.arguments.push_str(&arguments);
                    }
                }
            }
        }
        Ok(())
    }

    /// 完成流式拼接并补齐兼容服务偶尔遗漏的 call id/type，再执行最小有效性校验。
    fn finish(self) -> AppResult<AssistantTurn> {
        let tool_calls = self
            .tool_calls
            .into_values()
            .filter(|call| !call.name.trim().is_empty())
            .map(|call| ModelToolCall {
                id: if call.id.is_empty() {
                    format!("call_{}", Uuid::new_v4().simple())
                } else {
                    call.id
                },
                kind: if call.kind.is_empty() {
                    "function".to_owned()
                } else {
                    call.kind
                },
                function: ModelFunctionCall {
                    name: call.name,
                    arguments: call.arguments,
                },
            })
            .collect::<Vec<_>>();
        if self.content.trim().is_empty() && tool_calls.is_empty() {
            return Err(AppError::BadRequest(
                "AI 接口没有返回文本或工具调用".to_owned(),
            ));
        }
        Ok(AssistantTurn {
            content: self.content,
            tool_calls,
            finish_reason: self.finish_reason,
        })
    }
}

/// 加载并解密工作区启用的模型配置，未启用时阻止创建新的 Agent Run。
pub async fn load_enabled_settings(
    state: &SharedState,
    workspace_id: &str,
) -> AppResult<AgentModelSettings> {
    let settings = load_settings_row(state, workspace_id)
        .await?
        .filter(|settings| settings.enabled)
        .ok_or_else(|| AppError::BadRequest("当前工作区尚未启用 AI".to_owned()))?;
    model_settings(state, settings)
}

/**
 * 使用 Agent 的正式 Provider 发起最小连接测试，配置页与实际运行因此共享地址校验、密钥和兼容降级。
 * 测试允许尚未启用的已保存配置，管理员可在正式开放给工作区成员前先验证模型。
 */
pub async fn test_connection(state: &SharedState, workspace_id: &str) -> AppResult<String> {
    let settings = load_settings_row(state, workspace_id)
        .await?
        .ok_or_else(|| AppError::BadRequest("请先保存 AI 配置".to_owned()))?;
    if settings.model.trim().is_empty() {
        return Err(AppError::BadRequest(
            "测试连接前必须填写模型名称".to_owned(),
        ));
    }
    let model = settings.model.clone();
    let settings = model_settings(state, settings)?;
    call_chat(
        state,
        &settings,
        &[
            ModelMessage::system("Reply with OK only."),
            ModelMessage::user("Connection test"),
        ],
        &[],
        false,
        "low",
        None,
    )
    .await?;
    Ok(model)
}

/// 读取工作区模型记录；是否要求启用由调用场景决定，避免配置测试复制一套查询和解密逻辑。
async fn load_settings_row(
    state: &SharedState,
    workspace_id: &str,
) -> AppResult<Option<SettingsRow>> {
    Ok(sqlx::query_as::<_, SettingsRow>(
        r#"
        SELECT enabled, base_url, model, api_key_ciphertext
        FROM workspace_ai_settings
        WHERE workspace_id = ?
        "#,
    )
    .bind(workspace_id)
    .fetch_optional(&state.pool)
    .await?)
}

/// 将数据库记录转换成仅在当前调用存活的明文配置，API Key 不会进入响应或持久化日志。
fn model_settings(state: &SharedState, settings: SettingsRow) -> AppResult<AgentModelSettings> {
    let api_key = settings
        .api_key_ciphertext
        .as_deref()
        .map(|ciphertext| {
            secrets::decrypt(&state.secret_key, ciphertext)
                .map_err(|error| AppError::Internal(error.to_string()))
        })
        .transpose()?;
    Ok(AgentModelSettings {
        base_url: settings.base_url,
        model: settings.model,
        api_key,
        reasoning_effort_supported: AtomicBool::new(true),
    })
}

/**
 * 调用标准 OpenAI Chat Completions，并同时支持文字与原生 function tool_calls 的 SSE 增量。
 * 工具参数仅在完整拼接后交给 Runtime 校验，避免执行半段 JSON 或模型伪造的未知函数。
 */
pub async fn call_chat(
    state: &SharedState,
    settings: &AgentModelSettings,
    messages: &[ModelMessage],
    tools: &[ToolDefinition],
    allow_tools: bool,
    reasoning_effort: &str,
    stream_sink: Option<mpsc::UnboundedSender<AssistantStreamUpdate>>,
) -> AppResult<AssistantTurn> {
    let resolved = validate_base_url_network(state, &settings.base_url).await?;
    let request_timeout = std::time::Duration::from_secs(state.agent_timeout_seconds);
    let client = resolved.pinned_client(request_timeout)?;
    let request_chars = messages
        .iter()
        .filter_map(|message| message.content.as_deref())
        .map(|content| content.chars().count())
        .sum::<usize>();
    let started_at = Instant::now();
    tracing::info!(
        model = %settings.model,
        message_count = messages.len(),
        request_chars,
        allow_tools,
        reasoning_effort,
        "Agent model request started"
    );
    let mut include_reasoning_effort = settings.reasoning_effort_supported.load(Ordering::Relaxed);
    let response = loop {
        let mut request = client
            .post(resolved.url.clone())
            .timeout(request_timeout)
            .json(&ChatCompletionRequest {
                model: &settings.model,
                messages,
                stream: true,
                reasoning_effort: include_reasoning_effort.then_some(reasoning_effort),
                tools: allow_tools.then_some(tools),
                tool_choice: allow_tools.then_some("auto"),
                parallel_tool_calls: allow_tools.then_some(false),
            });
        if let Some(api_key) = settings
            .api_key
            .as_deref()
            .filter(|value| !value.is_empty())
        {
            request = request.bearer_auth(api_key);
        }
        let response = request.send().await.map_err(|error| {
            if error.is_timeout() {
                AppError::BadRequest("AI 模型请求超时".to_owned())
            } else {
                AppError::BadRequest(format!("AI 接口连接失败: {error}"))
            }
        })?;
        let status = response.status();
        if status.is_success() {
            break response;
        }
        let body = response
            .text()
            .await
            .map_err(|error| AppError::BadRequest(format!("AI 接口响应读取失败: {error}")))?;
        let error_body = serde_json::from_str::<Value>(&body).ok();
        let message = error_body
            .as_ref()
            .and_then(|value| value.pointer("/error/message")?.as_str().map(str::to_owned))
            .unwrap_or_else(|| truncate_chars(&body, 1_000));
        let error_parameter = error_body
            .as_ref()
            .and_then(|value| value.pointer("/error/param")?.as_str());
        if include_reasoning_effort
            && reasoning_effort_is_unsupported(status.as_u16(), &message, error_parameter)
        {
            include_reasoning_effort = false;
            settings
                .reasoning_effort_supported
                .store(false, Ordering::Relaxed);
            tracing::warn!(
                model = %settings.model,
                status = status.as_u16(),
                "AI provider does not support reasoning_effort; retrying with prompt guidance"
            );
            continue;
        }
        return Err(AppError::BadRequest(format!(
            "AI 接口返回 {}: {}",
            status.as_u16(),
            message
        )));
    };
    let is_event_stream = response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.to_ascii_lowercase().contains("text/event-stream"));
    let turn = if is_event_stream {
        read_stream(response, stream_sink.as_ref()).await?
    } else {
        let body = response
            .text()
            .await
            .map_err(|error| AppError::BadRequest(format!("AI 接口响应读取失败: {error}")))?;
        let turn = if body.trim_start().starts_with("data:") {
            parse_stream_body(&body)?
        } else {
            parse_completion_body(&body)?
        };
        emit_stream_update(stream_sink.as_ref(), &turn.content);
        turn
    };
    tracing::info!(
        model = %settings.model,
        elapsed_ms = started_at.elapsed().as_millis(),
        response_chars = turn.content.chars().count(),
        tool_calls = turn.tool_calls.len(),
        "Agent model request finished"
    );
    Ok(turn)
}

/// 按完整 SSE 行增量读取响应，并按固定时间窗口合并文本快照以控制数据库写入频率。
async fn read_stream(
    mut response: reqwest::Response,
    stream_sink: Option<&mpsc::UnboundedSender<AssistantStreamUpdate>>,
) -> AppResult<AssistantTurn> {
    let mut pending = Vec::new();
    let mut accumulator = StreamAccumulator::default();
    let mut done = false;
    let mut last_emitted_bytes = 0usize;
    let mut last_emit_at = Instant::now() - STREAM_EMIT_INTERVAL;
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|error| AppError::BadRequest(format!("AI 流式响应读取失败: {error}")))?
    {
        pending.extend_from_slice(&chunk);
        while let Some(line_end) = pending.iter().position(|byte| *byte == b'\n') {
            let line = pending.drain(..=line_end).collect::<Vec<_>>();
            if append_stream_line(&line, &mut accumulator)? {
                done = true;
                break;
            }
        }
        if accumulator.content.len() != last_emitted_bytes
            && last_emit_at.elapsed() >= STREAM_EMIT_INTERVAL
        {
            emit_stream_update(stream_sink, &accumulator.content);
            last_emitted_bytes = accumulator.content.len();
            last_emit_at = Instant::now();
        }
        if pending.len() > MAX_STREAM_LINE_BYTES {
            return Err(AppError::BadRequest(
                "AI 流式响应中的单个事件异常过大".to_owned(),
            ));
        }
        if done {
            break;
        }
    }
    if !done && !pending.is_empty() {
        append_stream_line(&pending, &mut accumulator)?;
    }
    if accumulator.content.len() != last_emitted_bytes {
        emit_stream_update(stream_sink, &accumulator.content);
    }
    accumulator.finish()
}

/// 兼容错误标记为普通文本的 SSE 响应，完整文本仍复用同一增量解析器。
fn parse_stream_body(body: &str) -> AppResult<AssistantTurn> {
    let mut accumulator = StreamAccumulator::default();
    for line in body.lines() {
        if append_stream_line(line.as_bytes(), &mut accumulator)? {
            break;
        }
    }
    accumulator.finish()
}

/// 解析一行标准 `data:` 事件；心跳、event 名称和空行均安全忽略。
fn append_stream_line(line: &[u8], accumulator: &mut StreamAccumulator) -> AppResult<bool> {
    let line = std::str::from_utf8(line)
        .map_err(|error| AppError::BadRequest(format!("AI 流式响应不是有效 UTF-8: {error}")))?;
    let Some(data) = line.trim().strip_prefix("data:").map(str::trim) else {
        return Ok(false);
    };
    if data == "[DONE]" {
        return Ok(true);
    }
    let chunk = serde_json::from_str::<ChatCompletionChunk>(data)
        .map_err(|error| AppError::BadRequest(format!("AI 流式响应格式无效: {error}")))?;
    accumulator.append_chunk(chunk)?;
    Ok(false)
}

/// 回退解析不支持 SSE 的 OpenAI-compatible 服务返回的完整 JSON。
fn parse_completion_body(body: &str) -> AppResult<AssistantTurn> {
    let completion = serde_json::from_str::<ChatCompletionResponse>(body)
        .map_err(|error| AppError::BadRequest(format!("AI 接口响应格式无效: {error}")))?;
    let choice = completion
        .choices
        .into_iter()
        .next()
        .ok_or_else(|| AppError::BadRequest("AI 接口没有返回候选结果".to_owned()))?;
    let content = choice.message.content.unwrap_or_default();
    if content.trim().is_empty() && choice.message.tool_calls.is_empty() {
        return Err(AppError::BadRequest(
            "AI 接口没有返回文本或工具调用".to_owned(),
        ));
    }
    Ok(AssistantTurn {
        content,
        tool_calls: choice.message.tool_calls,
        finish_reason: choice.finish_reason,
    })
}

/// 将公开回答快照发送给 Runtime；接收端已取消时静默丢弃，模型请求仍可正常收敛。
fn emit_stream_update(
    stream_sink: Option<&mpsc::UnboundedSender<AssistantStreamUpdate>>,
    content: &str,
) {
    if content.is_empty() {
        return;
    }
    if let Some(stream_sink) = stream_sink {
        let _ = stream_sink.send(AssistantStreamUpdate {
            content: content.to_owned(),
        });
    }
}

/// 仅在上游明确拒绝 reasoning_effort 参数时降级，其他 4xx 仍按真实错误返回。
fn reasoning_effort_is_unsupported(status: u16, message: &str, parameter: Option<&str>) -> bool {
    if !matches!(status, 400 | 422) {
        return false;
    }
    if parameter.is_some_and(|value| value.eq_ignore_ascii_case("reasoning_effort")) {
        return true;
    }
    let normalized = message.to_ascii_lowercase();
    normalized.contains("reasoning_effort")
        && [
            "unsupported",
            "not supported",
            "unknown",
            "unrecognized",
            "not permitted",
            "extra field",
        ]
        .iter()
        .any(|marker| normalized.contains(marker))
}

/// 规范 Chat Completions 地址，只接受 HTTP(S) 且拒绝 URL 内嵌凭据。
fn chat_endpoint(base_url: &str) -> AppResult<Url> {
    let normalized = base_url.trim().trim_end_matches('/');
    let url = Url::parse(normalized)
        .map_err(|_| AppError::BadRequest("AI Base URL 格式无效".to_owned()))?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(AppError::BadRequest(
            "AI Base URL 必须是 HTTP 或 HTTPS 地址".to_owned(),
        ));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(AppError::BadRequest(
            "AI Base URL 不能包含账号或密码".to_owned(),
        ));
    }
    let endpoint = if normalized.ends_with("/chat/completions") {
        normalized.to_owned()
    } else {
        format!("{normalized}/chat/completions")
    };
    Url::parse(&endpoint).map_err(|_| AppError::BadRequest("Chat 接口地址无效".to_owned()))
}

/// 经私网校验后的出站目标：保留原始 URL（HTTPS 的 SNI 与证书校验仍用主机名），同时携带
/// 已核验的具体地址，供请求阶段把 DNS 固定到这些 IP。
pub struct ResolvedEndpoint {
    pub url: Url,
    host: String,
    socket_addrs: Vec<SocketAddr>,
}

impl ResolvedEndpoint {
    /// 构建一次性 HTTP 客户端，把目标主机名固定解析到校验时得到的 IP，从而堵住
    /// “校验用一次 DNS、请求时再解析到 169.254.169.254/内网” 的 rebinding TOCTOU；
    /// 同时保留禁止重定向。主机名本身是 IP 字面量时该固定是等价 no-op。
    fn pinned_client(&self, request_timeout: Duration) -> AppResult<reqwest::Client> {
        reqwest::Client::builder()
            .timeout(request_timeout)
            .redirect(reqwest::redirect::Policy::none())
            .resolve_to_addrs(&self.host, &self.socket_addrs)
            .build()
            .map_err(|error| AppError::Internal(format!("无法初始化受限 HTTP 客户端: {error}")))
    }
}

/// 解析 OpenAI-compatible 地址并拒绝默认不可访问的本机、私网和保留网段。
///
/// DNS 校验、禁止重定向与“请求阶段固定到已核验 IP”共同缩小 SSRF 面；确需连接局域网模型时
/// 必须由部署者显式开启环境变量，工作区管理员不能自行扩大服务器网络权限。
pub async fn validate_base_url_network(
    state: &SharedState,
    base_url: &str,
) -> AppResult<ResolvedEndpoint> {
    let endpoint = chat_endpoint(base_url)?;
    let host = endpoint
        .host_str()
        .ok_or_else(|| AppError::BadRequest("Chat 接口地址缺少主机名".to_owned()))?;
    let port = endpoint
        .port_or_known_default()
        .ok_or_else(|| AppError::BadRequest("Chat 接口地址端口无效".to_owned()))?;
    let addresses = if let Ok(address) = host.parse::<IpAddr>() {
        vec![address]
    } else {
        tokio::net::lookup_host((host, port))
            .await
            .map_err(|_| AppError::BadRequest("无法解析 AI 接口主机名".to_owned()))?
            .map(|address| address.ip())
            .collect::<Vec<_>>()
    };
    if addresses.is_empty() {
        return Err(AppError::BadRequest("AI 接口主机名没有可用地址".to_owned()));
    }
    let hostname_is_local = host.eq_ignore_ascii_case("localhost")
        || host.to_ascii_lowercase().ends_with(".localhost")
        || host.to_ascii_lowercase().ends_with(".local");
    let contains_restricted = hostname_is_local
        || addresses
            .iter()
            .copied()
            .any(crate::services::net_guard::is_restricted_address);
    if contains_restricted && !state.allow_private_ai_endpoints {
        return Err(AppError::BadRequest(
            "AI 接口解析到本机或私有网络；部署者需显式开启 ANYDATAS_AI_ALLOW_PRIVATE_NETWORK"
                .to_owned(),
        ));
    }
    if endpoint.scheme() == "http"
        && addresses
            .iter()
            .copied()
            .any(|address| !crate::services::net_guard::is_restricted_address(address))
    {
        return Err(AppError::BadRequest(
            "公网 AI 接口必须使用 HTTPS".to_owned(),
        ));
    }
    let socket_addrs = addresses
        .into_iter()
        .map(|address| SocketAddr::new(address, port))
        .collect();
    let host = host.to_owned();
    Ok(ResolvedEndpoint {
        url: endpoint,
        host,
        socket_addrs,
    })
}

/// 按字符压缩上游错误，避免代理返回整页 HTML 时污染 API 响应。
fn truncate_chars(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        value.to_owned()
    } else {
        value.chars().take(max_chars).collect::<String>() + "..."
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt;

    #[test]
    fn assembles_streamed_native_tool_calls() {
        let body = concat!(
            "data: {\"choices\":[{\"delta\":{\"content\":null,\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"preview_sql\",\"arguments\":\"{\\\"sql\\\":\\\"SELECT\"}}]},\"finish_reason\":null}]}\n\n",
            "data: {\"choices\":[{\"delta\":{\"content\":null,\"tool_calls\":[{\"index\":0,\"id\":null,\"type\":null,\"function\":{\"name\":null,\"arguments\":\" 1\\\"}\"}}]},\"finish_reason\":\"tool_calls\"}]}\n\n",
            "data: [DONE]\n\n"
        );
        let turn = parse_stream_body(body).unwrap();
        assert_eq!(turn.tool_calls.len(), 1);
        assert_eq!(turn.tool_calls[0].id, "call_1");
        assert_eq!(turn.tool_calls[0].function.name, "preview_sql");
        assert_eq!(
            turn.tool_calls[0].function.arguments,
            "{\"sql\":\"SELECT 1\"}"
        );
        assert_eq!(turn.finish_reason.as_deref(), Some("tool_calls"));
    }

    #[test]
    fn parses_non_streaming_tool_calls() {
        let body = r#"{
            "choices": [{
                "message": {
                    "content": null,
                    "tool_calls": [{
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "inspect_table", "arguments": "{\"alias\":\"data\"}"}
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }"#;
        let turn = parse_completion_body(body).unwrap();
        assert_eq!(turn.tool_calls[0].function.name, "inspect_table");
    }

    #[test]
    fn serializes_standard_tool_messages() {
        let message = ModelMessage::tool("call_3", "{\"ok\":true}");
        let value = serde_json::to_value(message).unwrap();
        assert_eq!(value["role"], "tool");
        assert_eq!(value["tool_call_id"], "call_3");
        assert!(value.get("tool_calls").is_none());
    }

    #[test]
    fn builds_chat_completion_endpoint() {
        assert_eq!(
            chat_endpoint("https://api.openai.com/v1").unwrap().as_str(),
            "https://api.openai.com/v1/chat/completions"
        );
    }

    #[test]
    fn classifies_private_and_public_ai_addresses() {
        use crate::services::net_guard::is_restricted_address;
        assert!(is_restricted_address("127.0.0.1".parse().unwrap()));
        assert!(is_restricted_address("10.0.0.8".parse().unwrap()));
        assert!(is_restricted_address("169.254.169.254".parse().unwrap()));
        assert!(is_restricted_address("::1".parse().unwrap()));
        assert!(is_restricted_address("fd00::1".parse().unwrap()));
        assert!(!is_restricted_address("8.8.8.8".parse().unwrap()));
        assert!(!is_restricted_address(
            "2606:4700:4700::1111".parse().unwrap()
        ));
    }

    #[test]
    fn only_downgrades_explicitly_unsupported_reasoning_effort() {
        assert!(reasoning_effort_is_unsupported(
            400,
            "Unknown parameter: reasoning_effort",
            None,
        ));
        assert!(reasoning_effort_is_unsupported(
            422,
            "reasoning_effort is not supported with this model",
            None,
        ));
        assert!(reasoning_effort_is_unsupported(
            400,
            "Unsupported value: medium",
            Some("reasoning_effort"),
        ));
        assert!(!reasoning_effort_is_unsupported(
            401,
            "invalid API key",
            None,
        ));
        assert!(!reasoning_effort_is_unsupported(
            400,
            "invalid messages",
            Some("messages"),
        ));
    }

    /// 模拟真实分段 SSE，确保完整回答结束前就能收到逐步增长的公开文本快照。
    #[tokio::test]
    async fn emits_public_text_snapshots_while_streaming() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            socket
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
                )
                .await
                .unwrap();
            socket
                .write_all(
                    b"data: {\"choices\":[{\"delta\":{\"content\":\"\\u4f60\"},\"finish_reason\":null}]}\n\n",
                )
                .await
                .unwrap();
            socket.flush().await.unwrap();
            tokio::time::sleep(Duration::from_millis(150)).await;
            socket
                .write_all(
                    b"data: {\"choices\":[{\"delta\":{\"content\":\"\\u597d\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n",
                )
                .await
                .unwrap();
        });
        let response = reqwest::get(format!("http://{address}")).await.unwrap();
        let (stream_tx, mut stream_rx) = mpsc::unbounded_channel();
        let turn = read_stream(response, Some(&stream_tx)).await.unwrap();

        assert_eq!(turn.content, "你好");
        assert_eq!(stream_rx.recv().await.unwrap().content, "你");
        assert_eq!(stream_rx.recv().await.unwrap().content, "你好");
        server.await.unwrap();
    }
}
