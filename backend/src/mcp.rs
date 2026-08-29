use std::{
    collections::{BTreeMap, HashMap, HashSet},
    process::Stdio,
    sync::Arc,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, oneshot},
    time::timeout,
};

const MCP_PROTOCOL: &str = "2024-11-05";
const MAX_SERVERS: usize = 8;
const MAX_TOOLS_PER_SERVER: usize = 32;
const MAX_RESULT_CHARS: usize = 24_000;
const RPC_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_MESSAGE_BYTES: usize = 8_000_000;

type PendingReplies = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct McpServerConfig {
    pub name: String,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
}

fn default_enabled() -> bool {
    true
}

#[derive(Debug, Clone)]
pub struct McpMappedTool {
    pub function_name: String,
    pub server: String,
    pub tool: String,
    pub description: String,
    pub parameters: Value,
}

#[derive(Default)]
pub struct McpHub {
    inner: Mutex<HubInner>,
}

#[derive(Default)]
struct HubInner {
    configs: Vec<McpServerConfig>,
    sessions: HashMap<String, McpSession>,
    tools: Vec<McpMappedTool>,
}

struct McpSession {
    child: Child,
    stdin: ChildStdin,
    pending: PendingReplies,
    next_id: u64,
}

impl Drop for McpSession {
    fn drop(&mut self) {
        let _ = self.child.start_kill();
    }
}

impl McpHub {
    /// 配置变化时重连；同配置复用已有 stdio 会话。
    pub async fn sync_and_list(
        &self,
        configs: Vec<McpServerConfig>,
    ) -> Result<Vec<McpMappedTool>, String> {
        let mut inner = self.inner.lock().await;
        if inner.configs != configs {
            inner.sessions.clear();
            inner.tools.clear();
            inner.configs = configs;
        }
        if inner.tools.is_empty() {
            inner.connect_enabled().await;
        }
        Ok(inner.tools.clone())
    }

    pub async fn call(&self, function_name: &str, arguments: &str) -> Result<String, String> {
        let mut inner = self.inner.lock().await;
        let mapped = inner
            .tools
            .iter()
            .find(|tool| tool.function_name == function_name)
            .cloned()
            .ok_or_else(|| format!("未声明的 MCP 工具: {function_name}"))?;
        let session = inner
            .sessions
            .get_mut(&mapped.server)
            .ok_or_else(|| format!("MCP 服务器未连接: {}", mapped.server))?;
        let args: Value = serde_json::from_str(arguments).unwrap_or_else(|_| json!({}));
        let result = session
            .rpc(
                "tools/call",
                json!({
                    "name": mapped.tool,
                    "arguments": if args.is_object() { args } else { json!({}) },
                }),
            )
            .await?;
        Ok(truncate_chars(
            &stringify_mcp_result(result),
            MAX_RESULT_CHARS,
        ))
    }
}

impl HubInner {
    async fn connect_enabled(&mut self) {
        self.sessions.clear();
        self.tools.clear();
        let enabled = self
            .configs
            .iter()
            .filter(|config| config.enabled)
            .cloned()
            .collect::<Vec<_>>();
        let mut used_names = HashSet::new();
        for config in enabled {
            match connect_server(&config).await {
                Ok((session, tools)) => {
                    self.sessions.insert(config.name.clone(), session);
                    for tool in tools.into_iter().take(MAX_TOOLS_PER_SERVER) {
                        let function_name =
                            unique_function_name(&config.name, &tool.name, &mut used_names);
                        self.tools.push(McpMappedTool {
                            function_name,
                            server: config.name.clone(),
                            tool: tool.name,
                            description: tool.description,
                            parameters: tool.parameters,
                        });
                    }
                }
                Err(error) => {
                    tracing::warn!(server = %config.name, %error, "MCP 服务器连接失败");
                }
            }
        }
    }
}

struct ListedTool {
    name: String,
    description: String,
    parameters: Value,
}

async fn connect_server(config: &McpServerConfig) -> Result<(McpSession, Vec<ListedTool>), String> {
    let mut command = Command::new(&config.command);
    command
        .args(&config.args)
        .envs(&config.env)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动 MCP 进程: {error}"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| "MCP 进程缺少 stdin".to_owned())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "MCP 进程缺少 stdout".to_owned())?;
    let pending = Arc::new(Mutex::new(HashMap::new()));
    let reader_pending = pending.clone();
    tokio::spawn(async move {
        read_stdout(stdout, reader_pending).await;
    });
    let mut session = McpSession {
        child,
        stdin,
        pending,
        next_id: 1,
    };
    let init = session
        .rpc(
            "initialize",
            json!({
                "protocolVersion": MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": { "name": "anydatas", "version": "0.1.0" },
            }),
        )
        .await?;
    if init
        .get("protocolVersion")
        .and_then(Value::as_str)
        .is_none()
    {
        return Err("MCP initialize 响应无效".to_owned());
    }
    session
        .notify("notifications/initialized", json!({}))
        .await?;
    let listed = session.rpc("tools/list", json!({})).await?;
    let tools = listed
        .get("tools")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(parse_listed_tool)
        .collect::<Vec<_>>();
    Ok((session, tools))
}

fn parse_listed_tool(value: Value) -> Option<ListedTool> {
    let name = value.get("name")?.as_str()?.trim().to_owned();
    if name.is_empty() {
        return None;
    }
    let description = value
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let parameters = value
        .get("inputSchema")
        .cloned()
        .unwrap_or_else(|| json!({"type": "object"}));
    Some(ListedTool {
        name,
        description,
        parameters,
    })
}

impl McpSession {
    async fn rpc(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let payload = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });
        write_message(&mut self.stdin, &payload).await?;
        match timeout(RPC_TIMEOUT, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err("MCP 会话已关闭".to_owned()),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(format!("MCP 调用超时: {method}"))
            }
        }
    }

    async fn notify(&mut self, method: &str, params: Value) -> Result<(), String> {
        write_message(
            &mut self.stdin,
            &json!({
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }),
        )
        .await
    }
}

async fn write_message(stdin: &mut ChildStdin, value: &Value) -> Result<(), String> {
    let body = serde_json::to_vec(value).map_err(|error| error.to_string())?;
    let header = format!("Content-Length: {}\r\n\r\n", body.len());
    stdin
        .write_all(header.as_bytes())
        .await
        .map_err(|error| error.to_string())?;
    stdin
        .write_all(&body)
        .await
        .map_err(|error| error.to_string())?;
    stdin.flush().await.map_err(|error| error.to_string())
}

async fn read_stdout(stdout: tokio::process::ChildStdout, pending: PendingReplies) {
    let mut reader = BufReader::new(stdout);
    loop {
        match read_message(&mut reader).await {
            Ok(message) => dispatch_message(message, &pending).await,
            Err(_) => {
                let mut pending = pending.lock().await;
                for (_, tx) in pending.drain() {
                    let _ = tx.send(Err("MCP 进程已退出".to_owned()));
                }
                break;
            }
        }
    }
}

async fn dispatch_message(message: Value, pending: &PendingReplies) {
    let Some(id) = message.get("id").and_then(json_id) else {
        return;
    };
    let tx = pending.lock().await.remove(&id);
    let Some(tx) = tx else {
        return;
    };
    if let Some(error) = message.get("error") {
        let text = error
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("MCP 调用失败");
        let _ = tx.send(Err(text.to_owned()));
    } else {
        let _ = tx.send(Ok(message.get("result").cloned().unwrap_or(Value::Null)));
    }
}

fn json_id(value: &Value) -> Option<u64> {
    match value {
        Value::Number(number) => number.as_u64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    }
}

async fn read_message(
    reader: &mut BufReader<tokio::process::ChildStdout>,
) -> Result<Value, String> {
    let mut line = String::new();
    let read = reader
        .read_line(&mut line)
        .await
        .map_err(|error| error.to_string())?;
    if read == 0 {
        return Err("MCP 进程已退出".to_owned());
    }
    if line.trim_start().starts_with('{') {
        return serde_json::from_str(line.trim()).map_err(|error| error.to_string());
    }
    let mut content_length = None;
    loop {
        let lower = line.to_ascii_lowercase();
        if let Some(value) = lower.strip_prefix("content-length:") {
            content_length = Some(
                value
                    .trim()
                    .parse::<usize>()
                    .map_err(|_| "MCP Content-Length 无效".to_owned())?,
            );
        }
        if line.trim().is_empty() {
            break;
        }
        line.clear();
        let read = reader
            .read_line(&mut line)
            .await
            .map_err(|error| error.to_string())?;
        if read == 0 {
            return Err("MCP 进程已退出".to_owned());
        }
    }
    let len = content_length.ok_or_else(|| "MCP 消息缺少 Content-Length".to_owned())?;
    if len > MAX_MESSAGE_BYTES {
        return Err("MCP 消息过大".to_owned());
    }
    let mut buf = vec![0u8; len];
    reader
        .read_exact(&mut buf)
        .await
        .map_err(|error| error.to_string())?;
    serde_json::from_slice(&buf).map_err(|error| error.to_string())
}

fn stringify_mcp_result(value: Value) -> String {
    if let Some(content) = value.get("content").and_then(Value::as_array) {
        let texts = content
            .iter()
            .filter_map(|item| {
                if item.get("type").and_then(Value::as_str) == Some("text") {
                    item.get("text").and_then(Value::as_str)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        if !texts.is_empty() {
            return texts.join("\n");
        }
    }
    value.to_string()
}

pub fn parse_configs(value: &str) -> Result<Vec<McpServerConfig>, String> {
    let parsed: Vec<McpServerConfig> =
        serde_json::from_str(value).map_err(|error| format!("MCP 配置 JSON 无效: {error}"))?;
    validate_configs(&parsed)?;
    Ok(parsed)
}

pub fn validate_configs(configs: &[McpServerConfig]) -> Result<(), String> {
    if configs.len() > MAX_SERVERS {
        return Err(format!("最多配置 {MAX_SERVERS} 个 MCP 服务器"));
    }
    let mut names = HashSet::new();
    for config in configs {
        if !is_server_name(&config.name) {
            return Err(format!("MCP 服务器名称无效: {}", config.name));
        }
        if !names.insert(config.name.clone()) {
            return Err(format!("MCP 服务器名称重复: {}", config.name));
        }
        if config.command.trim().is_empty()
            || config.command.chars().count() > 500
            || config.command.contains('\0')
            || config.command.chars().any(|ch| ch.is_control())
        {
            return Err(format!("MCP 命令无效: {}", config.name));
        }
        if config.args.len() > 16 || config.args.iter().any(|arg| arg.chars().count() > 500) {
            return Err(format!("MCP 参数无效: {}", config.name));
        }
        if config.env.len() > 16 {
            return Err(format!("MCP 环境变量过多: {}", config.name));
        }
        for (key, value) in &config.env {
            if !is_env_key(key) || value.chars().count() > 4_096 {
                return Err(format!("MCP 环境变量无效: {key}"));
            }
        }
    }
    Ok(())
}

pub fn is_server_name(name: &str) -> bool {
    let chars = name.chars().count();
    (1..=32).contains(&chars)
        && name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-')
}

fn is_env_key(key: &str) -> bool {
    let mut chars = key.chars();
    matches!(chars.next(), Some(ch) if ch.is_ascii_alphabetic() || ch == '_')
        && chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

pub fn sanitize_function_name(server: &str, tool: &str) -> String {
    let raw = format!("mcp_{server}_{tool}");
    let mut out = String::new();
    for ch in raw.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
            out.push(ch.to_ascii_lowercase());
        } else {
            out.push('_');
        }
    }
    if out
        .chars()
        .next()
        .is_none_or(|ch| !ch.is_ascii_alphabetic())
    {
        out.insert(0, 'm');
    }
    out.truncate(64);
    while out.ends_with('_') && out.len() > 1 {
        out.pop();
    }
    out
}

fn unique_function_name(server: &str, tool: &str, used: &mut HashSet<String>) -> String {
    let base = sanitize_function_name(server, tool);
    if used.insert(base.clone()) {
        return base;
    }
    for index in 2..100 {
        let candidate = {
            let suffix = format!("_{index}");
            let keep = 64usize.saturating_sub(suffix.len());
            format!("{}{suffix}", &base[..base.len().min(keep)])
        };
        if used.insert(candidate.clone()) {
            return candidate;
        }
    }
    base
}

fn truncate_chars(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        value.to_owned()
    } else {
        value.chars().take(max_chars).collect::<String>() + "…"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizes_mcp_function_names() {
        assert_eq!(
            sanitize_function_name("docs", "search files"),
            "mcp_docs_search_files"
        );
        assert!(sanitize_function_name("s", "t").starts_with("mcp_"));
    }

    #[test]
    fn rejects_duplicate_server_names() {
        let error = validate_configs(&[
            McpServerConfig {
                name: "docs".into(),
                command: "npx".into(),
                args: vec![],
                env: BTreeMap::new(),
                enabled: true,
            },
            McpServerConfig {
                name: "docs".into(),
                command: "npx".into(),
                args: vec![],
                env: BTreeMap::new(),
                enabled: true,
            },
        ])
        .unwrap_err();
        assert!(error.contains("重复"));
    }
}
