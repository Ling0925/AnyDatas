//! QuickJS post-process engine for optional query result transforms.
//!
//! Runs user `process(rows, meta)` after SQL succeeds. Optional sandboxed
//! `http.request` is injected when callers pass a `JsHttpRuntime`.

// Public surface is consumed by execution integration (Task 6).
#![allow(dead_code)]

use std::cell::Cell;
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::net::{IpAddr, ToSocketAddrs};
use std::rc::Rc;
use std::time::{Duration, Instant};

use reqwest::blocking::Client;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue, CONTENT_LENGTH, HOST};
use reqwest::{Method, Url};
use rquickjs::function::Rest;
use rquickjs::{
    CatchResultExt, Context, Ctx, Exception, Function, Object, Runtime, Value as JsValue,
};
use serde_json::{Map, Value};

use crate::error::AppError;
use crate::models::{FieldDefinition, JsRuntimeLimits};
use crate::services::net_guard::{self, AllowlistEntry, NetGuardError};

const CONSOLE_LINE_CHARS: usize = 500;
const ERROR_MESSAGE_CHARS: usize = 500;
const ALLOWED_HTTP_METHODS: &[&str] = &["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"];

/// Per-request HTTP limits sliced from [`JsRuntimeLimits`].
#[derive(Debug, Clone)]
pub struct JsHttpLimits {
    pub max_requests: usize,
    pub timeout_ms: u64,
    pub max_timeout_ms: u64,
    pub max_body_bytes: usize,
    pub max_request_body_bytes: usize,
}

impl JsHttpLimits {
    pub fn from_runtime(limits: &JsRuntimeLimits) -> Self {
        Self {
            max_requests: limits.http_max_requests,
            timeout_ms: limits.http_timeout_ms,
            max_timeout_ms: limits.http_max_timeout_ms,
            max_body_bytes: limits.http_max_body_bytes,
            max_request_body_bytes: limits.http_max_request_body_bytes,
        }
    }
}

/// Sandboxed host for synchronous `http.request` inside post-process JS.
#[derive(Debug)]
pub struct JsHttpRuntime {
    pub client: Client,
    pub limits: JsHttpLimits,
    pub allowlist: Vec<AllowlistEntry>,
    pub enabled: bool,
    pub allow_private_when_empty: bool,
    request_count: Cell<usize>,
}

impl JsHttpRuntime {
    /// Build a blocking client (no redirects) from runtime limits.
    pub fn new(limits: &JsRuntimeLimits) -> Result<Self, PostProcessError> {
        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_millis(limits.http_max_timeout_ms.max(1)))
            .build()
            .map_err(|error| {
                PostProcessError::new(
                    "post_js_internal",
                    format!("创建 HTTP 客户端失败：{error}"),
                )
            })?;
        Ok(Self::from_parts(
            client,
            JsHttpLimits::from_runtime(limits),
            limits.allowlist.clone(),
            limits.enabled_http,
            limits.allow_private_network,
        ))
    }

    pub fn from_parts(
        client: Client,
        limits: JsHttpLimits,
        allowlist: Vec<AllowlistEntry>,
        enabled: bool,
        allow_private_when_empty: bool,
    ) -> Self {
        Self {
            client,
            limits,
            allowlist,
            enabled,
            allow_private_when_empty,
            request_count: Cell::new(0),
        }
    }

    fn perform(&self, request: HttpRequestArgs) -> Result<HttpResponseValue, HttpHostError> {
        if !self.enabled {
            return Err(HttpHostError::disabled("脚本 HTTP 已禁用"));
        }

        let used = self.request_count.get();
        if used >= self.limits.max_requests {
            return Err(HttpHostError::limit(format!(
                "HTTP 请求次数超过限制（{}）",
                self.limits.max_requests
            )));
        }
        self.request_count.set(used + 1);

        let method = parse_http_method(&request.method)?;
        let url = Url::parse(&request.url).map_err(|error| {
            HttpHostError::error(format!("URL 无效：{error}"))
        })?;

        if url.scheme() != "http" && url.scheme() != "https" {
            return Err(HttpHostError::blocked("仅允许 http 或 https 协议"));
        }

        let host = url
            .host_str()
            .ok_or_else(|| HttpHostError::blocked("URL 缺少主机"))?;
        let port = url.port_or_known_default().unwrap_or(80);
        let resolved = resolve_host_ips(host, port).map_err(HttpHostError::error)?;

        match net_guard::url_allowed(
            &url,
            &self.allowlist,
            &resolved,
            self.allow_private_when_empty,
        ) {
            Ok(()) => {}
            Err(NetGuardError::InvalidScheme) => {
                return Err(HttpHostError::blocked("仅允许 http 或 https 协议"));
            }
            Err(NetGuardError::NotAllowlisted) => {
                return Err(HttpHostError::blocked("目标地址不在白名单内"));
            }
            Err(NetGuardError::RestrictedAddress) => {
                return Err(HttpHostError::blocked("目标解析到本机或私有网络"));
            }
        }

        if let Some(body) = request.body.as_ref()
            && body.len() > self.limits.max_request_body_bytes
        {
            return Err(HttpHostError::limit(format!(
                "请求体超过限制（{} > {} 字节）",
                body.len(),
                self.limits.max_request_body_bytes
            )));
        }

        let timeout_ms = request
            .timeout_ms
            .unwrap_or(self.limits.timeout_ms)
            .clamp(1, self.limits.max_timeout_ms.max(1));

        let mut builder = self
            .client
            .request(method, url)
            .timeout(Duration::from_millis(timeout_ms));

        let mut headers = HeaderMap::new();
        for (name, value) in request.headers {
            let lower = name.to_ascii_lowercase();
            if lower == "host" || lower == "content-length" {
                continue;
            }
            let header_name = HeaderName::from_bytes(name.as_bytes()).map_err(|error| {
                HttpHostError::error(format!("非法请求头名 `{name}`：{error}"))
            })?;
            if header_name == HOST || header_name == CONTENT_LENGTH {
                continue;
            }
            let header_value = HeaderValue::from_str(&value).map_err(|error| {
                HttpHostError::error(format!("非法请求头值 `{name}`：{error}"))
            })?;
            headers.append(header_name, header_value);
        }
        builder = builder.headers(headers);

        if let Some(body) = request.body {
            builder = builder.body(body);
        }

        let response = builder.send().map_err(|error| {
            HttpHostError::error(format!("HTTP 请求失败：{error}"))
        })?;

        let status = response.status().as_u16();
        let ok = response.status().is_success();
        let response_headers = flatten_response_headers(response.headers());
        let body_bytes = response.bytes().map_err(|error| {
            HttpHostError::error(format!("读取响应失败：{error}"))
        })?;
        if body_bytes.len() > self.limits.max_body_bytes {
            return Err(HttpHostError::limit(format!(
                "响应体超过限制（{} > {} 字节）",
                body_bytes.len(),
                self.limits.max_body_bytes
            )));
        }
        let body = String::from_utf8_lossy(&body_bytes).into_owned();

        Ok(HttpResponseValue {
            ok,
            status,
            headers: response_headers,
            body,
        })
    }
}

#[derive(Debug, Clone)]
struct HttpRequestArgs {
    method: String,
    url: String,
    headers: Vec<(String, String)>,
    body: Option<String>,
    timeout_ms: Option<u64>,
}

#[derive(Debug, Clone)]
struct HttpResponseValue {
    ok: bool,
    status: u16,
    headers: BTreeMap<String, String>,
    body: String,
}

#[derive(Debug, Clone)]
struct HttpHostError {
    code: &'static str,
    message: String,
}

impl HttpHostError {
    fn disabled(message: impl Into<String>) -> Self {
        Self {
            code: "post_js_http_disabled",
            message: message.into(),
        }
    }

    fn blocked(message: impl Into<String>) -> Self {
        Self {
            code: "post_js_http_blocked",
            message: message.into(),
        }
    }

    fn limit(message: impl Into<String>) -> Self {
        Self {
            code: "post_js_http_limit",
            message: message.into(),
        }
    }

    fn error(message: impl Into<String>) -> Self {
        Self {
            code: "post_js_http_error",
            message: message.into(),
        }
    }
}

fn parse_http_method(raw: &str) -> Result<Method, HttpHostError> {
    let upper = raw.trim().to_ascii_uppercase();
    if !ALLOWED_HTTP_METHODS.contains(&upper.as_str()) {
        return Err(HttpHostError::error(format!(
            "不支持的 HTTP 方法：{raw}"
        )));
    }
    Method::from_bytes(upper.as_bytes()).map_err(|error| {
        HttpHostError::error(format!("不支持的 HTTP 方法：{error}"))
    })
}

fn resolve_host_ips(host: &str, port: u16) -> Result<Vec<IpAddr>, String> {
    let target = format!("{host}:{port}");
    let addrs = target
        .to_socket_addrs()
        .map_err(|error| format!("DNS 解析失败：{error}"))?;
    let ips: Vec<IpAddr> = addrs.map(|addr| addr.ip()).collect();
    if ips.is_empty() {
        return Err("DNS 解析未返回地址".to_owned());
    }
    Ok(ips)
}

fn flatten_response_headers(headers: &HeaderMap) -> BTreeMap<String, String> {
    let mut map = BTreeMap::<String, String>::new();
    for (name, value) in headers.iter() {
        let key = name.as_str().to_ascii_lowercase();
        let value_text = value.to_str().unwrap_or("").to_owned();
        map.entry(key)
            .and_modify(|existing| {
                if !existing.is_empty() {
                    existing.push(',');
                }
                existing.push_str(&value_text);
            })
            .or_insert(value_text);
    }
    map
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct PostProcessMeta {
    pub columns: Vec<String>,
    pub column_types: Map<String, Value>,
    pub row_count: usize,
}

#[derive(Debug, Clone)]
pub struct PostProcessOutput {
    pub columns: Vec<FieldDefinition>,
    pub rows: Vec<Vec<Value>>,
    pub elapsed: Duration,
    pub console: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct PostProcessError {
    pub code: &'static str,
    pub message: String,
}

impl PostProcessError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn into_app_error(self) -> AppError {
        AppError::bad_request_code(self.code, format!("后处理 JS 失败：{}", self.message))
    }
}

/// Trim and treat blank scripts as disabled.
pub fn normalize_post_js(raw: Option<&str>) -> Option<String> {
    raw.map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

/// Execute user post-process script against SQL result columns/rows.
///
/// When `http` is `Some`, injects a sandboxed `http.request` host. When `None`,
/// no `http` global is available to scripts.
pub fn run_post_process(
    script: &str,
    columns: &[FieldDefinition],
    rows: &[Vec<Value>],
    limits: &JsRuntimeLimits,
    timeout_ms: u64,
    http: Option<&JsHttpRuntime>,
) -> Result<PostProcessOutput, PostProcessError> {
    let started = Instant::now();

    if script.len() > limits.max_script_bytes {
        return Err(PostProcessError::new(
            "post_js_limit_script",
            format!(
                "脚本超过大小限制（{} > {} 字节）",
                script.len(),
                limits.max_script_bytes
            ),
        ));
    }
    if rows.len() > limits.max_input_rows {
        return Err(PostProcessError::new(
            "post_js_limit_input_rows",
            format!(
                "输入行数超过限制（{} > {}）",
                rows.len(),
                limits.max_input_rows
            ),
        ));
    }
    let payload_bytes = estimate_payload_bytes(columns, rows);
    if payload_bytes > limits.max_input_payload_bytes {
        return Err(PostProcessError::new(
            "post_js_limit_input_rows",
            format!(
                "输入载荷超过限制（约 {} > {} 字节）",
                payload_bytes, limits.max_input_payload_bytes
            ),
        ));
    }

    let input_objects = rows_to_objects(columns, rows);
    let meta = PostProcessMeta {
        columns: columns.iter().map(|c| c.name.clone()).collect(),
        column_types: columns
            .iter()
            .map(|c| (c.name.clone(), Value::String(c.data_type.clone())))
            .collect(),
        row_count: rows.len(),
    };
    let meta_json = serde_json::json!({
        "columns": meta.columns,
        "columnTypes": meta.column_types,
        "rowCount": meta.row_count,
    });
    let rows_json = Value::Array(input_objects);
    let rows_json_text = serde_json::to_string(&rows_json).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("序列化输入行失败：{error}"),
        )
    })?;
    let meta_json_text = serde_json::to_string(&meta_json).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("序列化 meta 失败：{error}"),
        )
    })?;

    let timeout = Duration::from_millis(timeout_ms.max(1));
    let deadline = Instant::now() + timeout;
    let console_buf = Rc::new(RefCell::new(Vec::<String>::new()));
    let max_console_lines = limits.max_console_lines;
    let memory_limit = limits.memory_mb.saturating_mul(1024 * 1024);

    let runtime = Runtime::new().map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("创建 JS 运行时失败：{error}"),
        )
    })?;
    if memory_limit > 0 {
        runtime.set_memory_limit(memory_limit);
    }
    runtime.set_interrupt_handler(Some(Box::new(move || Instant::now() >= deadline)));

    let context = Context::full(&runtime).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("创建 JS 上下文失败：{error}"),
        )
    })?;

    let max_output_rows = limits.max_output_rows;
    let eval_result =
        context.with(|ctx| -> Result<Vec<Vec<(String, Value)>>, PostProcessError> {
            install_console(&ctx, Rc::clone(&console_buf), max_console_lines)?;
            if let Some(http_runtime) = http {
                install_http(&ctx, http_runtime)?;
            }

            if let Err(error) = ctx.eval::<(), _>(script).catch(&ctx) {
                return Err(map_js_error(&ctx, error, deadline, "post_js_syntax"));
            }

            let globals = ctx.globals();
            let process_value: JsValue = match globals.get("process") {
                Ok(value) => value,
                Err(_) => {
                    return Err(PostProcessError::new(
                        "post_js_no_process",
                        "未定义 process 函数",
                    ));
                }
            };
            if process_value.is_null()
                || process_value.is_undefined()
                || !process_value.is_function()
            {
                return Err(PostProcessError::new(
                    "post_js_no_process",
                    "未定义 process 函数",
                ));
            }
            let process: Function = process_value.into_function().ok_or_else(|| {
                PostProcessError::new("post_js_no_process", "未定义 process 函数")
            })?;

            let rows_js = ctx
                .json_parse(rows_json_text.as_str())
                .map_err(|error| map_rquickjs_error(&ctx, error, deadline, "post_js_internal"))?;
            let meta_js = ctx
                .json_parse(meta_json_text.as_str())
                .map_err(|error| map_rquickjs_error(&ctx, error, deadline, "post_js_internal"))?;

            let returned: JsValue = match process.call((rows_js, meta_js)).catch(&ctx) {
                Ok(value) => value,
                Err(error) => return Err(map_js_error(&ctx, error, deadline, "post_js_throw")),
            };

            js_return_to_objects(&ctx, returned, max_output_rows, deadline)
        });

    let objects = eval_result?;
    let (out_columns, out_rows) = objects_to_table(&objects);
    let console = console_buf.borrow().clone();

    Ok(PostProcessOutput {
        columns: out_columns,
        rows: out_rows,
        elapsed: started.elapsed(),
        console,
    })
}

fn install_http<'js>(
    ctx: &Ctx<'js>,
    http: &JsHttpRuntime,
) -> Result<(), PostProcessError> {
    // QuickJS callbacks must be 'static. The host pointer is only dereferenced while
    // `run_post_process` still holds `http` and the JS context is active.
    let http_ptr = http as *const JsHttpRuntime as usize;
    let request_fn = Function::new(ctx.clone(), move |ctx, options| {
        // SAFETY: pointer remains valid for the duration of run_post_process / ctx.with.
        let runtime = unsafe { &*(http_ptr as *const JsHttpRuntime) };
        http_request_callback(ctx, options, runtime)
    })
    .map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("注册 http.request 失败：{error}"),
        )
    })?;

    let http_obj = Object::new(ctx.clone()).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("创建 http 对象失败：{error}"),
        )
    })?;
    http_obj.set("request", request_fn).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("设置 http.request 失败：{error}"),
        )
    })?;
    ctx.globals().set("http", http_obj).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("注入 http 失败：{error}"),
        )
    })?;
    Ok(())
}

fn http_request_callback<'js>(
    ctx: Ctx<'js>,
    options: JsValue<'js>,
    runtime: &JsHttpRuntime,
) -> rquickjs::Result<JsValue<'js>> {
    match http_request_js(&ctx, runtime, options) {
        Ok(object) => Ok(object.into_value()),
        Err(error) => Err(throw_http_error(&ctx, error)),
    }
}

fn throw_http_error(ctx: &Ctx<'_>, error: HttpHostError) -> rquickjs::Error {
    match Exception::from_message(ctx.clone(), &error.message) {
        Ok(exception) => {
            let object = exception.as_object();
            let _ = object.set("code", error.code);
            let _ = object.set("name", "Error");
            exception.throw()
        }
        Err(err) => err,
    }
}

fn http_request_js<'js>(
    ctx: &Ctx<'js>,
    runtime: &JsHttpRuntime,
    options: JsValue<'js>,
) -> Result<Object<'js>, HttpHostError> {
    let args = parse_http_request_args(ctx, options)?;
    let response = runtime.perform(args)?;
    response_to_js_object(ctx, response)
}

fn parse_http_request_args<'js>(
    ctx: &Ctx<'js>,
    options: JsValue<'js>,
) -> Result<HttpRequestArgs, HttpHostError> {
    let object = options
        .into_object()
        .ok_or_else(|| HttpHostError::error("http.request 参数必须是对象"))?;

    let method = match object.get::<_, JsValue>("method") {
        Ok(value) if !value.is_undefined() && !value.is_null() => {
            js_value_to_string(ctx, value).map_err(HttpHostError::error)?
        }
        _ => "GET".to_owned(),
    };

    let url = match object.get::<_, JsValue>("url") {
        Ok(value) if !value.is_undefined() && !value.is_null() => {
            js_value_to_string(ctx, value).map_err(HttpHostError::error)?
        }
        _ => {
            return Err(HttpHostError::error("http.request 缺少 url"));
        }
    };

    let mut headers = Vec::new();
    if let Ok(header_value) = object.get::<_, JsValue>("headers")
        && !header_value.is_undefined()
        && !header_value.is_null()
    {
        let header_obj = header_value
            .into_object()
            .ok_or_else(|| HttpHostError::error("headers 必须是对象"))?;
        let keys = header_obj.keys::<String>();
        for key_result in keys {
            let key = key_result.map_err(|error| {
                HttpHostError::error(format!("读取 headers 失败：{error}"))
            })?;
            let value: JsValue = header_obj.get(key.as_str()).map_err(|error| {
                HttpHostError::error(format!("读取 headers 失败：{error}"))
            })?;
            let text = js_value_to_string(ctx, value).map_err(HttpHostError::error)?;
            headers.push((key, text));
        }
    }

    let body = match object.get::<_, JsValue>("body") {
        Ok(value) if value.is_null() || value.is_undefined() => None,
        Ok(value) => Some(js_value_to_string(ctx, value).map_err(HttpHostError::error)?),
        Err(_) => None,
    };

    let timeout_ms = match object.get::<_, JsValue>("timeoutMs") {
        Ok(value) if value.is_undefined() || value.is_null() => None,
        Ok(value) => Some(js_value_to_u64(ctx, value)?),
        Err(_) => None,
    };

    Ok(HttpRequestArgs {
        method,
        url,
        headers,
        body,
        timeout_ms,
    })
}

fn js_value_to_string<'js>(ctx: &Ctx<'js>, value: JsValue<'js>) -> Result<String, String> {
    if let Some(s) = value.as_string() {
        return s
            .to_string()
            .map_err(|error| format!("读取字符串失败：{error}"));
    }
    if let Some(v) = value.as_bool() {
        return Ok(v.to_string());
    }
    if let Some(v) = value.as_int() {
        return Ok(v.to_string());
    }
    if let Some(v) = value.as_float() {
        return Ok(v.to_string());
    }
    if value.is_null() {
        return Ok("null".to_owned());
    }
    if value.is_undefined() {
        return Ok("undefined".to_owned());
    }
    match ctx.json_stringify(value) {
        Ok(Some(text)) => text
            .to_string()
            .map_err(|error| format!("序列化失败：{error}")),
        Ok(None) => Ok(String::new()),
        Err(error) => Err(format!("序列化失败：{error}")),
    }
}

fn js_value_to_u64<'js>(_ctx: &Ctx<'js>, value: JsValue<'js>) -> Result<u64, HttpHostError> {
    if let Some(v) = value.as_int() {
        if v < 0 {
            return Err(HttpHostError::error("timeoutMs 必须为非负整数"));
        }
        return Ok(v as u64);
    }
    if let Some(v) = value.as_float() {
        if !v.is_finite() || v < 0.0 {
            return Err(HttpHostError::error("timeoutMs 必须为非负整数"));
        }
        return Ok(v as u64);
    }
    if let Some(s) = value.as_string()
        && let Ok(text) = s.to_string()
        && let Ok(parsed) = text.parse::<u64>()
    {
        return Ok(parsed);
    }
    Err(HttpHostError::error("timeoutMs 必须为数字"))
}

fn response_to_js_object<'js>(
    ctx: &Ctx<'js>,
    response: HttpResponseValue,
) -> Result<Object<'js>, HttpHostError> {
    let object = Object::new(ctx.clone()).map_err(|error| {
        HttpHostError::error(format!("创建响应对象失败：{error}"))
    })?;
    object
        .set("ok", response.ok)
        .map_err(|error| HttpHostError::error(format!("设置 ok 失败：{error}")))?;
    object
        .set("status", response.status as i32)
        .map_err(|error| HttpHostError::error(format!("设置 status 失败：{error}")))?;
    object
        .set("body", response.body)
        .map_err(|error| HttpHostError::error(format!("设置 body 失败：{error}")))?;

    let headers = Object::new(ctx.clone()).map_err(|error| {
        HttpHostError::error(format!("创建 headers 对象失败：{error}"))
    })?;
    for (name, value) in response.headers {
        headers.set(name.as_str(), value).map_err(|error| {
            HttpHostError::error(format!("设置响应头失败：{error}"))
        })?;
    }
    object
        .set("headers", headers)
        .map_err(|error| HttpHostError::error(format!("设置 headers 失败：{error}")))?;
    Ok(object)
}

fn install_console<'js>(
    ctx: &Ctx<'js>,
    console_buf: Rc<RefCell<Vec<String>>>,
    max_console_lines: usize,
) -> Result<(), PostProcessError> {
    let console = Object::new(ctx.clone()).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("创建 console 对象失败：{error}"),
        )
    })?;

    for level in ["log", "warn", "error", "info", "debug"] {
        let buf = Rc::clone(&console_buf);
        let level_owned = level.to_owned();
        let func = Function::new(
            ctx.clone(),
            move |args: Rest<JsValue<'_>>| -> rquickjs::Result<()> {
                if max_console_lines == 0 {
                    return Ok(());
                }
                let mut parts = Vec::with_capacity(args.0.len());
                for arg in args.0 {
                    parts.push(js_value_preview(&arg));
                }
                let mut line = format!("[{level_owned}] {}", parts.join(" "));
                truncate_in_place(&mut line, CONSOLE_LINE_CHARS);
                let mut guard = buf.borrow_mut();
                if guard.len() < max_console_lines {
                    guard.push(line);
                }
                Ok(())
            },
        )
        .map_err(|error| {
            PostProcessError::new(
                "post_js_internal",
                format!("注册 console.{level} 失败：{error}"),
            )
        })?;
        console.set(level, func).map_err(|error| {
            PostProcessError::new(
                "post_js_internal",
                format!("设置 console.{level} 失败：{error}"),
            )
        })?;
    }

    ctx.globals().set("console", console).map_err(|error| {
        PostProcessError::new(
            "post_js_internal",
            format!("注入 console 失败：{error}"),
        )
    })?;
    Ok(())
}

fn js_value_preview(value: &JsValue<'_>) -> String {
    if value.is_undefined() {
        return "undefined".to_owned();
    }
    if value.is_null() {
        return "null".to_owned();
    }
    if let Some(v) = value.as_bool() {
        return v.to_string();
    }
    if let Some(v) = value.as_int() {
        return v.to_string();
    }
    if let Some(v) = value.as_float() {
        return v.to_string();
    }
    if let Some(v) = value.as_string()
        && let Ok(s) = v.to_string()
    {
        return s;
    }
    // Best-effort: type name for objects/functions/etc.
    format!("[{}]", value.type_of())
}

fn map_js_error<'js>(
    ctx: &Ctx<'js>,
    error: rquickjs::CaughtError<'js>,
    deadline: Instant,
    default_code: &'static str,
) -> PostProcessError {
    if Instant::now() >= deadline {
        return PostProcessError::new("post_js_timeout", "脚本执行超时");
    }
    match error {
        rquickjs::CaughtError::Error(inner) => {
            map_rquickjs_error(ctx, inner, deadline, default_code)
        }
        rquickjs::CaughtError::Exception(exception) => {
            exception_to_error(&exception, default_code)
        }
        rquickjs::CaughtError::Value(value) => {
            if value.is_uncatchable_error() || Instant::now() >= deadline {
                return PostProcessError::new("post_js_timeout", "脚本执行超时");
            }
            let mut message = js_value_preview(&value);
            if message.is_empty() {
                message = "脚本抛出了非 Error 值".to_owned();
            }
            truncate_in_place(&mut message, ERROR_MESSAGE_CHARS);
            PostProcessError::new(classify_js_code(default_code, "", &message), message)
        }
    }
}

fn map_rquickjs_error<'js>(
    ctx: &Ctx<'js>,
    error: rquickjs::Error,
    deadline: Instant,
    default_code: &'static str,
) -> PostProcessError {
    if Instant::now() >= deadline {
        return PostProcessError::new("post_js_timeout", "脚本执行超时");
    }
    if error.is_exception() {
        let caught = ctx.catch();
        if caught.is_uncatchable_error() {
            return PostProcessError::new("post_js_timeout", "脚本执行超时");
        }
        if let Some(obj) = caught.as_object()
            && let Some(exception) = rquickjs::Exception::from_object(obj.clone())
        {
            return exception_to_error(&exception, default_code);
        }
        let mut message = js_value_preview(&caught);
        truncate_in_place(&mut message, ERROR_MESSAGE_CHARS);
        return PostProcessError::new(default_code, message);
    }
    match error {
        rquickjs::Error::Allocation => {
            PostProcessError::new("post_js_internal", "JS 运行时内存不足")
        }
        other => {
            let mut message = other.to_string();
            truncate_in_place(&mut message, ERROR_MESSAGE_CHARS);
            PostProcessError::new(default_code, message)
        }
    }
}

fn exception_to_error(exception: &rquickjs::Exception<'_>, default_code: &'static str) -> PostProcessError {
    let name = exception
        .get::<_, Option<String>>("name")
        .ok()
        .flatten()
        .unwrap_or_default();
    let code_prop = exception
        .get::<_, Option<String>>("code")
        .ok()
        .flatten()
        .unwrap_or_default();
    let mut message = exception
        .message()
        .filter(|m| !m.is_empty())
        .unwrap_or_else(|| exception.to_string());
    truncate_in_place(&mut message, ERROR_MESSAGE_CHARS);
    let code = if is_known_post_js_code(&code_prop) {
        leak_code_static(&code_prop)
    } else {
        classify_js_code(default_code, &name, &message)
    };
    PostProcessError::new(code, message)
}

fn is_known_post_js_code(code: &str) -> bool {
    matches!(
        code,
        "post_js_no_process"
            | "post_js_syntax"
            | "post_js_throw"
            | "post_js_timeout"
            | "post_js_limit_script"
            | "post_js_limit_input_rows"
            | "post_js_limit_output_rows"
            | "post_js_bad_return"
            | "post_js_http_disabled"
            | "post_js_http_blocked"
            | "post_js_http_limit"
            | "post_js_http_error"
            | "post_js_internal"
    )
}

fn leak_code_static(code: &str) -> &'static str {
    match code {
        "post_js_no_process" => "post_js_no_process",
        "post_js_syntax" => "post_js_syntax",
        "post_js_throw" => "post_js_throw",
        "post_js_timeout" => "post_js_timeout",
        "post_js_limit_script" => "post_js_limit_script",
        "post_js_limit_input_rows" => "post_js_limit_input_rows",
        "post_js_limit_output_rows" => "post_js_limit_output_rows",
        "post_js_bad_return" => "post_js_bad_return",
        "post_js_http_disabled" => "post_js_http_disabled",
        "post_js_http_blocked" => "post_js_http_blocked",
        "post_js_http_limit" => "post_js_http_limit",
        "post_js_http_error" => "post_js_http_error",
        "post_js_internal" => "post_js_internal",
        _ => "post_js_throw",
    }
}

fn classify_js_code(default_code: &'static str, name: &str, message: &str) -> &'static str {
    if name.contains("Syntax") || message.contains("SyntaxError") {
        "post_js_syntax"
    } else if default_code == "post_js_syntax" {
        // eval-stage failures without a clear SyntaxError name still map to syntax
        "post_js_syntax"
    } else {
        default_code
    }
}

fn rows_to_objects(columns: &[FieldDefinition], rows: &[Vec<Value>]) -> Vec<Value> {
    rows.iter()
        .map(|row| {
            let mut map = Map::new();
            for (index, column) in columns.iter().enumerate() {
                let value = row.get(index).cloned().unwrap_or(Value::Null);
                map.insert(column.name.clone(), sanitize_input_value(value));
            }
            Value::Object(map)
        })
        .collect()
}

/// Keep JSON-friendly scalars; stringify exotic values so JS always sees plain data.
fn sanitize_input_value(value: Value) -> Value {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => value,
        other => Value::String(other.to_string()),
    }
}

/// Convert `process` return value into ordered key/value rows, preserving JS key insertion order.
fn js_return_to_objects<'js>(
    ctx: &Ctx<'js>,
    returned: JsValue<'js>,
    max_output_rows: usize,
    deadline: Instant,
) -> Result<Vec<Vec<(String, Value)>>, PostProcessError> {
    let array = returned.into_array().ok_or_else(|| {
        PostProcessError::new("post_js_bad_return", "process 必须返回对象数组")
    })?;
    let len = array.len();
    if len > max_output_rows {
        return Err(PostProcessError::new(
            "post_js_limit_output_rows",
            format!("输出行数超过限制（{len} > {max_output_rows}）"),
        ));
    }

    let mut objects = Vec::with_capacity(len);
    for index in 0..len {
        let item: JsValue = array.get(index).map_err(|error| {
            map_rquickjs_error(ctx, error, deadline, "post_js_bad_return")
        })?;
        let object = item.into_object().ok_or_else(|| {
            PostProcessError::new(
                "post_js_bad_return",
                format!("process 返回的第 {} 行不是对象", index + 1),
            )
        })?;
        objects.push(js_object_to_ordered_pairs(ctx, &object, deadline)?);
    }
    Ok(objects)
}

fn js_object_to_ordered_pairs<'js>(
    ctx: &Ctx<'js>,
    object: &Object<'js>,
    deadline: Instant,
) -> Result<Vec<(String, Value)>, PostProcessError> {
    let mut pairs = Vec::new();
    let keys = object.keys::<String>();
    for key_result in keys {
        let key = key_result.map_err(|error| {
            map_rquickjs_error(ctx, error, deadline, "post_js_bad_return")
        })?;
        let js_value: JsValue = object.get(key.as_str()).map_err(|error| {
            map_rquickjs_error(ctx, error, deadline, "post_js_bad_return")
        })?;
        let value = js_value_to_json(ctx, js_value, deadline)?;
        pairs.push((key, value));
    }
    Ok(pairs)
}

fn js_value_to_json<'js>(
    ctx: &Ctx<'js>,
    value: JsValue<'js>,
    deadline: Instant,
) -> Result<Value, PostProcessError> {
    if value.is_undefined() || value.is_null() {
        return Ok(Value::Null);
    }
    if let Some(v) = value.as_bool() {
        return Ok(Value::Bool(v));
    }
    if let Some(v) = value.as_int() {
        return Ok(Value::Number(v.into()));
    }
    if let Some(v) = value.as_float() {
        // QuickJS often stores whole numbers as floats after arithmetic.
        if v.is_finite()
            && v.fract() == 0.0
            && v >= i64::MIN as f64
            && v <= i64::MAX as f64
        {
            return Ok(Value::Number((v as i64).into()));
        }
        if let Some(n) = serde_json::Number::from_f64(v) {
            return Ok(Value::Number(n));
        }
        return Ok(Value::Null);
    }
    if let Some(s) = value.as_string() {
        let text = s
            .to_string()
            .map_err(|error| map_rquickjs_error(ctx, error, deadline, "post_js_bad_return"))?;
        return Ok(Value::String(text));
    }

    // Nested objects/arrays/functions: JSON-stringify then parse to detect cycles / non-JSON.
    match ctx.json_stringify(value) {
        Ok(Some(text)) => {
            let text = text
                .to_string()
                .map_err(|error| map_rquickjs_error(ctx, error, deadline, "post_js_bad_return"))?;
            serde_json::from_str(&text).map_err(|_| {
                PostProcessError::new(
                    "post_js_bad_return",
                    "process 返回值包含无法序列化的内容",
                )
            })
        }
        Ok(None) => Ok(Value::Null),
        Err(_) => Err(PostProcessError::new(
            "post_js_bad_return",
            "process 返回值包含无法序列化的内容",
        )),
    }
}

fn objects_to_table(
    objects: &[Vec<(String, Value)>],
) -> (Vec<FieldDefinition>, Vec<Vec<Value>>) {
    if objects.is_empty() {
        return (Vec::new(), Vec::new());
    }

    // First-row key order, then append later keys on first appearance.
    let mut ordered: Vec<String> = Vec::new();
    let mut ordered_seen = std::collections::HashSet::new();
    for object in objects {
        for (key, _) in object {
            if ordered_seen.insert(key.clone()) {
                ordered.push(key.clone());
            }
        }
    }

    let mut type_state: BTreeMap<String, TypeState> = ordered
        .iter()
        .map(|name| (name.clone(), TypeState::AllNull))
        .collect();

    let mut out_rows = Vec::with_capacity(objects.len());
    for object in objects {
        let lookup: std::collections::HashMap<&str, &Value> = object
            .iter()
            .map(|(k, v)| (k.as_str(), v))
            .collect();
        let mut row = Vec::with_capacity(ordered.len());
        for name in &ordered {
            let raw = lookup.get(name.as_str()).copied().cloned().unwrap_or(Value::Null);
            let cell = normalize_cell(raw);
            if let Some(state) = type_state.get_mut(name) {
                state.observe(&cell);
            }
            row.push(cell);
        }
        out_rows.push(row);
    }

    let columns = ordered
        .into_iter()
        .map(|name| {
            let data_type = type_state
                .get(&name)
                .map(|state| state.label())
                .unwrap_or("文本")
                .to_owned();
            FieldDefinition {
                name,
                data_type,
                nullable: true,
            }
        })
        .collect();

    (columns, out_rows)
}

#[derive(Debug, Clone, Copy)]
enum TypeState {
    AllNull,
    Bool,
    Int,
    Float,
    Text,
}

impl TypeState {
    fn observe(&mut self, value: &Value) {
        if matches!(self, Self::Text) || value.is_null() {
            return;
        }
        let next = match value {
            Value::Bool(_) => Self::Bool,
            Value::Number(n) => {
                if n.is_i64() || n.as_u64().is_some() {
                    Self::Int
                } else if n.as_f64().is_some() {
                    // Distinguish whole floats as int when finite and fract==0
                    if let Some(f) = n.as_f64() {
                        if f.is_finite() && f.fract() == 0.0 && f >= i64::MIN as f64 && f <= i64::MAX as f64
                        {
                            Self::Int
                        } else {
                            Self::Float
                        }
                    } else {
                        Self::Float
                    }
                } else {
                    Self::Text
                }
            }
            Value::String(_) => Self::Text,
            _ => Self::Text,
        };
        *self = match (*self, next) {
            (Self::AllNull, x) => x,
            (Self::Bool, Self::Bool) => Self::Bool,
            (Self::Int, Self::Int) => Self::Int,
            (Self::Int, Self::Float) | (Self::Float, Self::Int) | (Self::Float, Self::Float) => {
                Self::Float
            }
            (Self::Bool, _) | (_, Self::Bool) => Self::Text,
            (Self::Text, _) | (_, Self::Text) => Self::Text,
            (a, b) if matches!((a, b), (Self::Int, Self::Int)) => Self::Int,
            _ => Self::Text,
        };
    }

    fn label(&self) -> &'static str {
        match self {
            Self::AllNull | Self::Text => "文本",
            Self::Bool => "布尔",
            Self::Int => "整数",
            Self::Float => "小数",
        }
    }
}

fn normalize_cell(value: Value) -> Value {
    match value {
        Value::Null => Value::Null,
        Value::Bool(v) => Value::Bool(v),
        Value::Number(n) => Value::Number(n),
        Value::String(s) => Value::String(s),
        // Arrays/objects are not valid cell scalars for the table model.
        other => Value::String(other.to_string()),
    }
}

fn estimate_payload_bytes(columns: &[FieldDefinition], rows: &[Vec<Value>]) -> usize {
    let mut total = 0usize;
    for column in columns {
        total = total.saturating_add(column.name.len());
        total = total.saturating_add(8);
    }
    for row in rows {
        for value in row {
            total = total.saturating_add(approx_value_bytes(value));
        }
    }
    total
}

fn approx_value_bytes(value: &Value) -> usize {
    match value {
        Value::Null => 4,
        Value::Bool(_) => 5,
        Value::Number(n) => n.to_string().len(),
        Value::String(s) => s.len().saturating_add(2),
        Value::Array(items) => items
            .iter()
            .fold(2usize, |acc, item| acc.saturating_add(approx_value_bytes(item))),
        Value::Object(map) => map.iter().fold(2usize, |acc, (k, v)| {
            acc.saturating_add(k.len())
                .saturating_add(approx_value_bytes(v))
                .saturating_add(4)
        }),
    }
}

fn truncate_in_place(text: &mut String, max_chars: usize) {
    if max_chars == 0 {
        text.clear();
        return;
    }
    let mut chars = text.chars();
    let preview: String = chars.by_ref().take(max_chars).collect();
    if chars.next().is_some() {
        *text = format!("{preview}...");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::blocking::Client;
    use serde_json::json;

    fn field(name: &str, data_type: &str) -> FieldDefinition {
        FieldDefinition {
            name: name.to_owned(),
            data_type: data_type.to_owned(),
            nullable: true,
        }
    }

    fn test_limits() -> JsRuntimeLimits {
        JsRuntimeLimits::test_default()
    }

    #[test]
    fn normalize_post_js_trims() {
        assert!(normalize_post_js(None).is_none());
        assert!(normalize_post_js(Some("  ")).is_none());
        assert!(normalize_post_js(Some("\n\t")).is_none());
        let script = normalize_post_js(Some(" function process(r){return r} ")).unwrap();
        assert!(script.starts_with("function"));
    }

    #[test]
    fn filters_and_derives_columns() {
        let columns = vec![field("amount", "小数")];
        let rows = vec![vec![json!(1)], vec![json!(2)]];
        let script = r#"
          function process(rows, meta) {
            return rows.filter(r => r.amount > 1).map(r => ({ amount: r.amount, doubled: r.amount * 2 }));
          }
        "#;
        let out = run_post_process(script, &columns, &rows, &test_limits(), 5000, None).unwrap();
        assert_eq!(out.rows.len(), 1);
        assert!(out.columns.iter().any(|c| c.name == "doubled"));
        let amount_idx = out
            .columns
            .iter()
            .position(|c| c.name == "amount")
            .unwrap();
        let doubled_idx = out
            .columns
            .iter()
            .position(|c| c.name == "doubled")
            .unwrap();
        assert_eq!(out.rows[0][amount_idx], json!(2));
        assert_eq!(out.rows[0][doubled_idx], json!(4));
    }

    #[test]
    fn rejects_missing_process() {
        let err = run_post_process("const x = 1", &[], &[], &test_limits(), 1000, None).unwrap_err();
        assert_eq!(err.code, "post_js_no_process");
    }

    #[test]
    fn rejects_throw() {
        let script = "function process(){ throw new Error('boom') }";
        let err = run_post_process(script, &[], &[], &test_limits(), 1000, None).unwrap_err();
        assert_eq!(err.code, "post_js_throw");
        assert!(err.message.contains("boom"));
    }

    #[test]
    fn rejects_bad_return() {
        let script = "function process(){ return 42 }";
        let err = run_post_process(script, &[], &[], &test_limits(), 1000, None).unwrap_err();
        assert_eq!(err.code, "post_js_bad_return");
    }

    #[test]
    fn rejects_oversized_script() {
        let mut limits = test_limits();
        limits.max_script_bytes = 8;
        let err = run_post_process(
            "function process(r){return r}",
            &[],
            &[],
            &limits,
            1000,
            None,
        )
        .unwrap_err();
        assert_eq!(err.code, "post_js_limit_script");
    }

    #[test]
    fn empty_return_yields_empty_table() {
        let columns = vec![field("a", "文本")];
        let rows = vec![vec![json!("x")]];
        let script = "function process(){ return [] }";
        let out = run_post_process(script, &columns, &rows, &test_limits(), 1000, None).unwrap();
        assert!(out.columns.is_empty());
        assert!(out.rows.is_empty());
    }

    #[test]
    fn rejects_syntax_error() {
        let err = run_post_process(
            "function process(rows) { return rows;",
            &[],
            &[],
            &test_limits(),
            1000,
            None,
        )
        .unwrap_err();
        assert_eq!(err.code, "post_js_syntax");
    }

    #[test]
    fn rejects_output_row_limit() {
        let mut limits = test_limits();
        limits.max_output_rows = 1;
        let script = "function process(){ return [{a:1},{a:2}] }";
        let err = run_post_process(script, &[], &[], &limits, 1000, None).unwrap_err();
        assert_eq!(err.code, "post_js_limit_output_rows");
    }

    #[test]
    fn rejects_input_row_limit() {
        let mut limits = test_limits();
        limits.max_input_rows = 1;
        let columns = vec![field("a", "整数")];
        let rows = vec![vec![json!(1)], vec![json!(2)]];
        let script = "function process(rows){ return rows }";
        let err = run_post_process(script, &columns, &rows, &limits, 1000, None).unwrap_err();
        assert_eq!(err.code, "post_js_limit_input_rows");
    }

    #[test]
    fn captures_console_lines() {
        let script = r#"
          function process(rows) {
            console.log('hello', 1);
            console.warn('careful');
            return rows;
          }
        "#;
        let out = run_post_process(script, &[], &[], &test_limits(), 1000, None).unwrap();
        assert!(out.console.iter().any(|line| line.contains("hello")));
        assert!(out.console.iter().any(|line| line.contains("careful")));
    }

    #[test]
    fn column_union_order_follows_first_row() {
        let script = r#"
          function process() {
            return [
              { b: 1, a: 2 },
              { a: 3, c: 4, b: 5 }
            ];
          }
        "#;
        let out = run_post_process(script, &[], &[], &test_limits(), 1000, None).unwrap();
        let names: Vec<_> = out.columns.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, vec!["b", "a", "c"]);
    }

    #[test]
    fn infers_types_from_values() {
        let script = r#"
          function process() {
            return [
              { flag: true, n: 1, f: 1.5, t: "x", z: null },
              { flag: false, n: 2, f: 2.5, t: "y", z: null }
            ];
          }
        "#;
        let out = run_post_process(script, &[], &[], &test_limits(), 1000, None).unwrap();
        let by_name: std::collections::HashMap<_, _> = out
            .columns
            .iter()
            .map(|c| (c.name.as_str(), c.data_type.as_str()))
            .collect();
        assert_eq!(by_name["flag"], "布尔");
        assert_eq!(by_name["n"], "整数");
        assert_eq!(by_name["f"], "小数");
        assert_eq!(by_name["t"], "文本");
        assert_eq!(by_name["z"], "文本");
    }

    #[test]
    fn into_app_error_prefixes_message() {
        let err = PostProcessError::new("post_js_throw", "boom");
        let app = err.into_app_error();
        match app {
            AppError::BadRequestCoded { code, message } => {
                assert_eq!(code, "post_js_throw");
                assert!(message.starts_with("后处理 JS 失败："));
                assert!(message.contains("boom"));
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn times_out_infinite_loop() {
        let script = "function process(){ for(;;){} }";
        let err = run_post_process(script, &[], &[], &test_limits(), 50, None).unwrap_err();
        assert_eq!(err.code, "post_js_timeout");
    }

    fn spawn_mock_http_server(
        status_line: &str,
        headers: &str,
        body: &str,
    ) -> (String, u16, std::thread::JoinHandle<()>) {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let port = addr.port();
        let status_line = status_line.to_owned();
        let headers = headers.to_owned();
        let body = body.to_owned();
        let handle = std::thread::spawn(move || {
            // Serve a few connections so multi-request tests can reuse the same mock.
            for _ in 0..16 {
                listener.set_nonblocking(false).ok();
                let Ok((mut socket, _)) = listener.accept() else {
                    break;
                };
                let mut buf = [0u8; 4096];
                let _ = socket.read(&mut buf);
                let response = format!(
                    "{status_line}\r\n{headers}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = socket.write_all(response.as_bytes());
            }
        });
        (format!("http://127.0.0.1:{port}"), port, handle)
    }

    fn http_runtime_for(
        limits: &JsRuntimeLimits,
        enabled: bool,
        allowlist: Vec<crate::services::net_guard::AllowlistEntry>,
    ) -> JsHttpRuntime {
        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap();
        JsHttpRuntime::from_parts(
            client,
            JsHttpLimits::from_runtime(limits),
            allowlist,
            enabled,
            limits.allow_private_network,
        )
    }

    #[test]
    fn http_request_blocked_on_loopback_without_allowlist() {
        let (base, _port, _handle) =
            spawn_mock_http_server("HTTP/1.1 200 OK", "Content-Type: text/plain\r\n", "ok");
        let limits = test_limits();
        let http = http_runtime_for(&limits, true, Vec::new());
        let script = format!(
            r#"
              function process(rows) {{
                const res = http.request({{ method: 'GET', url: '{base}/data' }});
                return [{{ body: res.body }}];
              }}
            "#
        );
        let err = run_post_process(&script, &[], &[], &limits, 5000, Some(&http)).unwrap_err();
        assert_eq!(err.code, "post_js_http_blocked");
    }

    #[test]
    fn http_request_succeeds_when_allowlisted() {
        let (base, port, _handle) =
            spawn_mock_http_server("HTTP/1.1 200 OK", "Content-Type: text/plain\r\n", "hello-body");
        let limits = test_limits();
        let allowlist = crate::services::net_guard::parse_allowlist(&format!("127.0.0.1:{port}"))
            .unwrap();
        let http = http_runtime_for(&limits, true, allowlist);
        let script = format!(
            r#"
              function process(rows) {{
                const res = http.request({{ method: 'GET', url: '{base}/data' }});
                return [{{ ok: res.ok, status: res.status, body: res.body }}];
              }}
            "#
        );
        let out = run_post_process(&script, &[], &[], &limits, 5000, Some(&http)).unwrap();
        assert_eq!(out.rows.len(), 1);
        let by_name: std::collections::HashMap<_, _> = out
            .columns
            .iter()
            .enumerate()
            .map(|(i, c)| (c.name.as_str(), &out.rows[0][i]))
            .collect();
        assert_eq!(by_name["ok"], &json!(true));
        assert_eq!(by_name["status"], &json!(200));
        assert_eq!(by_name["body"], &json!("hello-body"));
    }

    #[test]
    fn http_request_disabled() {
        let (base, port, _handle) =
            spawn_mock_http_server("HTTP/1.1 200 OK", "Content-Type: text/plain\r\n", "x");
        let limits = test_limits();
        let allowlist = crate::services::net_guard::parse_allowlist(&format!("127.0.0.1:{port}"))
            .unwrap();
        let http = http_runtime_for(&limits, false, allowlist);
        let script = format!(
            r#"
              function process() {{
                http.request({{ url: '{base}/' }});
                return [];
              }}
            "#
        );
        let err = run_post_process(&script, &[], &[], &limits, 5000, Some(&http)).unwrap_err();
        assert_eq!(err.code, "post_js_http_disabled");
    }

    #[test]
    fn http_request_enforces_max_requests() {
        let (base, port, _handle) =
            spawn_mock_http_server("HTTP/1.1 200 OK", "Content-Type: text/plain\r\n", "x");
        let mut limits = test_limits();
        limits.http_max_requests = 1;
        let allowlist = crate::services::net_guard::parse_allowlist(&format!("127.0.0.1:{port}"))
            .unwrap();
        let http = http_runtime_for(&limits, true, allowlist);
        let script = format!(
            r#"
              function process() {{
                http.request({{ url: '{base}/a' }});
                http.request({{ url: '{base}/b' }});
                return [];
              }}
            "#
        );
        let err = run_post_process(&script, &[], &[], &limits, 5000, Some(&http)).unwrap_err();
        assert_eq!(err.code, "post_js_http_limit");
    }

    #[test]
    fn http_request_does_not_follow_redirects() {
        let (base, port, _handle) = spawn_mock_http_server(
            "HTTP/1.1 302 Found",
            "Location: http://169.254.169.254/\r\nContent-Type: text/plain\r\n",
            "redirect-body",
        );
        let limits = test_limits();
        let allowlist = crate::services::net_guard::parse_allowlist(&format!("127.0.0.1:{port}"))
            .unwrap();
        let http = http_runtime_for(&limits, true, allowlist);
        let script = format!(
            r#"
              function process() {{
                const res = http.request({{ url: '{base}/redirect' }});
                return [{{ ok: res.ok, status: res.status, body: res.body }}];
              }}
            "#
        );
        let out = run_post_process(&script, &[], &[], &limits, 5000, Some(&http)).unwrap();
        let by_name: std::collections::HashMap<_, _> = out
            .columns
            .iter()
            .enumerate()
            .map(|(i, c)| (c.name.as_str(), &out.rows[0][i]))
            .collect();
        assert_eq!(by_name["ok"], &json!(false));
        assert_eq!(by_name["status"], &json!(302));
        assert_eq!(by_name["body"], &json!("redirect-body"));
    }
}
