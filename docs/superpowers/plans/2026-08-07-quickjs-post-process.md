# Quick JS 查询后处理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 DuckDB SQL 成功后增加可选的服务端 QuickJS 后处理（`process(rows, meta)`），并支持受部署白名单约束的同步 `http.request`；同步查询、保存查询、后台任务与定时计划共用同一语义。

**Architecture:** 新建 `PostProcessEngine`（rquickjs）与共用 `net_guard`；在 `execution` 层于 SQL 结果返回前调用引擎；`QueryRequest` / 元数据表增加可空 `post_js`；工作台 SQL 下增加可折叠 JS 编辑区。无脚本时路径与现网一致。

**Tech Stack:** Rust (Axum, SQLx, reqwest, rquickjs 0.12.x), DuckDB, Vue 3 + Monaco, SQLite migrations.

**Spec:** [docs/superpowers/specs/2026-08-07-quickjs-post-process-design.md](../specs/2026-08-07-quickjs-post-process-design.md)

## Global Constraints

- 空 / 缺省 `postJs` 时行为与现网完全一致（含 Agent 工具查询构造 `QueryRequest` 时不传字段）。
- 后处理失败则整次查询/任务失败，不返回「仅 SQL」半结果。
- JS 仅同步 `process`；HTTP 仅经 Rust host `http.request`；不跟随重定向。
- 白名单为空：公网可访 + 默认 SSRF 拦受限地址；白名单非空：仅名单目标，名单内私网视为已批准。
- 不强制 HTTPS；scheme 仅 `http`/`https`。
- 错误码使用 spec §5.5 / §6 中的稳定字符串；`AppError` 响应 `code` 字段需能带上这些码（见 Task 1）。
- 迁移文件名：`0009_query_post_js.sql`（当前最新为 `0008_job_result_artifacts.sql`）。
- 中文 UI 文案；配置项写入 `.env.example`。
- Agent 不读写 `postJs`。
- 依赖：`rquickjs = "0.12"`，features 至少包含绑定与完整运行所需项（实现时锁定；优先 `bindgen`/`phf` 默认组合，以 `cargo test` 能编过为准）。
- HTTP 与 post-process 均在 `spawn_blocking` 内同步执行，避免在 async 中阻塞 worker；整体仍受现有 query timeout 包裹。

## File map

| Path | Responsibility |
| --- | --- |
| `backend/src/services/net_guard.rs` | 受限 IP 分类、allowlist 解析/匹配、JS HTTP URL 校验（从 `agent_provider` 抽取复用） |
| `backend/src/services/post_process.rs` | QuickJS 引擎、`process` 契约、限额、`http.request` host、行列转换 |
| `backend/src/services/mod.rs` | 导出新模块 |
| `backend/src/error.rs` | 支持稳定 `code` 的 BadRequest 变体或构造器 |
| `backend/src/config.rs` | JS / HTTP 限额与白名单配置 |
| `backend/src/models.rs` | `JsRuntimeLimits`、`AppState` 字段、`QueryRequest/Response`、SavedQuery/Job/Schedule 模型 |
| `backend/src/main.rs` | 组装 `js_runtime` 进 `AppState` |
| `backend/src/services/execution.rs` | SQL 后调用 post-process；artifact 路径写最终表 |
| `backend/src/services/query_engine.rs` | 可选：从行列重建 artifact 的 helper（若 post-process 后重写 artifact） |
| `backend/src/services/agent_provider.rs` | 改用 `net_guard` 的地址分类 |
| `backend/src/api/{queries,saved_queries,jobs,schedules}.rs` | 读写 `post_js`；`enqueue_job` 签名扩展 |
| `backend/src/workers.rs` | job 请求带 `post_js`；日志阶段 |
| `backend/migrations/0009_query_post_js.sql` | 三列可空 `post_js` |
| `.env.example` | 新环境变量 |
| `frontend/src/types.ts` / `api.ts` / `stores/*` | 字段往返 |
| `frontend/src/monaco.ts` | 加载 JS language contribution |
| `frontend/src/views/WorkbenchView.vue` | 折叠 JS 编辑区、运行/保存/任务带脚本 |
| `frontend/src/views/TasksView.vue` | 任务/计划展示与创建携带 `postJs` |
| `docs/14-rust-vue-rewrite.md` 或 README 一节 | 用户可见说明（Task 文档步） |

---

### Task 1: 错误码与 `net_guard` 抽取

**Files:**
- Create: `backend/src/services/net_guard.rs`
- Modify: `backend/src/services/mod.rs`
- Modify: `backend/src/error.rs`
- Modify: `backend/src/services/agent_provider.rs`（`is_restricted_*` / `validate_base_url_network` 改调 `net_guard`）
- Test: `backend/src/services/net_guard.rs` 内 `#[cfg(test)]`

**Interfaces:**
- Produces:
  - `AppError::coded_bad_request(code: &'static str, message: impl Into<String>)` 或等价，使 HTTP JSON `error.code` 为 `post_js_*` 而非一律 `bad_request`
  - `net_guard::is_restricted_address(IpAddr) -> bool`（行为与现 `agent_provider` 一致）
  - `net_guard::AllowlistEntry` 枚举：`Host(String)` / `HostPort { host: String, port: u16 }` / `UrlPrefix(String)`
  - `net_guard::parse_allowlist(text: &str) -> Result<Vec<AllowlistEntry>, String>`（非法条目 fail-fast）
  - `net_guard::url_allowed(url: &reqwest::Url, allowlist: &[AllowlistEntry], resolved: &[IpAddr], allow_private_when_empty: bool) -> Result<(), NetGuardError>`

- [ ] **Step 1: 扩展 `AppError` 以支持稳定业务 code**

在 `error.rs` 增加例如：

```rust
BadRequestCoded {
    code: &'static str,
    message: String,
},
```

`IntoResponse` 中映射为 `StatusCode::BAD_REQUEST` 且 `ErrorBody.code = code`。  
增加 helper：

```rust
impl AppError {
    pub fn bad_request_code(code: &'static str, message: impl Into<String>) -> Self {
        Self::BadRequestCoded { code, message: message.into() }
    }
}
```

保留原 `BadRequest` 不变。

- [ ] **Step 2: 写 `net_guard` 失败测试骨架与地址分类单测**

把 `agent_provider` 中 `is_restricted_ipv4/ipv6/address` 原样迁到 `net_guard`，单测至少包含现有断言：

```rust
assert!(is_restricted_address("127.0.0.1".parse().unwrap()));
assert!(is_restricted_address("169.254.169.254".parse().unwrap()));
assert!(is_restricted_address("10.0.0.8".parse().unwrap()));
assert!(!is_restricted_address("8.8.8.8".parse().unwrap()));
```

- [ ] **Step 3: 实现 allowlist 解析与匹配**

规则（与 spec 一致）：

- 忽略空行与 `#` 注释；支持逗号或换行分隔（`parse_allowlist` 对整段文本）。
- `host` / `host:port` / 以 `http://` 或 `https://` 开头的 URL 前缀。
- `url_allowed`：
  1. scheme 必须是 http 或 https
  2. allowlist 非空：必须命中一条（host 大小写不敏感；host:port 比端口；UrlPrefix 用规范化 URL 字符串 `starts_with`）
  3. allowlist 空：若任一 resolved IP 受限且 `!allow_private_when_empty` → 拒绝；hostname 为 localhost / `*.localhost` / `*.local` 在空名单时同样拒绝（除非 allow_private）
  4. allowlist 命中 → 信任目标（不因私网再拒）

单测：

- 空名单 + 8.8.8.8 公网 URL 结构允许（测匹配逻辑时可直接喂 resolved）
- 空名单 + 127.0.0.1 拒绝
- 名单含 `localhost:11434` 时允许对应 URL
- 名单含 `https://api.example.com/v1/` 时允许子路径、拒绝其它 host

- [ ] **Step 4: `agent_provider` 改为调用 `net_guard::is_restricted_address`**

删除重复函数；跑现有 agent_provider 测试。

- [ ] **Step 5: 注册模块并编译测试**

```bash
cargo test --manifest-path backend/Cargo.toml --locked net_guard -- --nocapture
cargo test --manifest-path backend/Cargo.toml --locked agent_provider -- --nocapture
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/src/error.rs backend/src/services/net_guard.rs backend/src/services/mod.rs backend/src/services/agent_provider.rs
git commit -m "refactor: extract net_guard and coded bad-request errors"
```

---

### Task 2: 配置、`JsRuntimeLimits` 与 `AppState`

**Files:**
- Modify: `backend/src/config.rs`
- Modify: `backend/src/models.rs`（`JsRuntimeLimits`、`AppState`、可选 `HttpAllowlist` 存 `Vec<AllowlistEntry>`）
- Modify: `backend/src/main.rs`
- Modify: `.env.example`
- Test: config 解析单测可放 `config.rs` 的 `#[cfg(test)]` 或集成在 post_process 测试用 fixture

**Interfaces:**
- Consumes: `net_guard::parse_allowlist`
- Produces:
  ```rust
  pub struct JsRuntimeLimits {
      pub enabled_http: bool,
      pub allow_private_network: bool,
      pub allowlist: Vec<crate::services::net_guard::AllowlistEntry>,
      pub max_script_bytes: usize,
      pub max_input_rows: usize,
      pub max_output_rows: usize,
      pub timeout_ms: u64,
      pub job_timeout_ms: u64,
      pub memory_mb: usize,
      pub max_console_lines: usize,
      pub max_input_payload_bytes: usize,
      pub http_max_requests: usize,
      pub http_timeout_ms: u64,
      pub http_max_timeout_ms: u64,
      pub http_max_body_bytes: usize,
      pub http_max_request_body_bytes: usize,
  }
  ```
  - `AppState.js_runtime: JsRuntimeLimits`
  - `Config` 增加对应字段；`from_env` 读取并校验范围；allowlist 非法 → **启动 fail-fast**

默认值（spec）：

| env | default |
| --- | --- |
| `ANYDATAS_JS_HTTP` | `1` |
| `ANYDATAS_JS_HTTP_ALLOWLIST` | empty |
| `ANYDATAS_JS_HTTP_ALLOWLIST_FILE` | empty |
| `ANYDATAS_JS_ALLOW_PRIVATE_NETWORK` | `0` |
| `ANYDATAS_JS_MAX_SCRIPT_BYTES` | 65536 |
| `ANYDATAS_JS_MAX_INPUT_ROWS` | 20000 |
| `ANYDATAS_JS_MAX_OUTPUT_ROWS` | 20000 |
| `ANYDATAS_JS_TIMEOUT_MS` | 5000 |
| `ANYDATAS_JS_JOB_TIMEOUT_MS` | 30000 |
| `ANYDATAS_JS_MEMORY_MB` | 64 |
| `ANYDATAS_JS_MAX_CONSOLE_LINES` | 50 |
| `ANYDATAS_JS_MAX_INPUT_PAYLOAD_BYTES` | 33554432 |
| `ANYDATAS_JS_HTTP_MAX_REQUESTS` | 8 |
| `ANYDATAS_JS_HTTP_TIMEOUT_MS` | 3000 |
| `ANYDATAS_JS_HTTP_MAX_TIMEOUT_MS` | 10000 |
| `ANYDATAS_JS_HTTP_MAX_BODY_BYTES` | 2097152 |
| `ANYDATAS_JS_HTTP_MAX_REQUEST_BODY_BYTES` | 1048576 |

`ANYDATAS_JS_HTTP` 解析：`0`/`false`/`off` → false，其余非空常见真值 → true（与项目其它 bool 解析风格一致，若无现成 helper 则本地写 `parse_bool`）。

- [ ] **Step 1: 扩展 Config 与校验范围**

合理 clamp 校验示例：timeout 100–120_000 ms；rows 1–1_000_000；script 1_024–1_048_576 等（与 duckdb 配置风格一致，过宽则 bail）。

读取 FILE：若路径设置则 `std::fs::read_to_string`，与 env 文本用换行拼接后再 `parse_allowlist`。

- [ ] **Step 2: AppState + main 注入**

所有构造 `AppState` 的测试 helper（如 `agent.rs` 测试里的 `AppState { ... }`）补上 `js_runtime: JsRuntimeLimits::test_default()`（在 models 或 post_process 提供测试默认）。

- [ ] **Step 3: 更新 `.env.example` 注释块**

中文注释说明白名单与 private 开关语义。

- [ ] **Step 4: 编译**

```bash
cargo test --manifest-path backend/Cargo.toml --locked --lib 2>&1 | tail -40
```

修复所有 `AppState` 缺字段。

- [ ] **Step 5: Commit**

```bash
git add backend/src/config.rs backend/src/models.rs backend/src/main.rs .env.example
git commit -m "feat: add JS runtime limits and allowlist config"
```

---

### Task 3: `PostProcessEngine` 核心（无 HTTP）

**Files:**
- Create: `backend/src/services/post_process.rs`
- Modify: `backend/src/services/mod.rs`
- Modify: `backend/Cargo.toml`（加 `rquickjs`）

**Interfaces:**
- Consumes: `JsRuntimeLimits`, `FieldDefinition`, `QueryResponse` 的行列形状
- Produces:
  ```rust
  pub struct PostProcessMeta {
      pub columns: Vec<String>,
      pub column_types: serde_json::Map<String, serde_json::Value>,
      pub row_count: usize,
  }

  pub struct PostProcessOutput {
      pub columns: Vec<FieldDefinition>,
      pub rows: Vec<Vec<serde_json::Value>>,
      pub elapsed: std::time::Duration,
      pub console: Vec<String>,
  }

  #[derive(Debug)]
  pub struct PostProcessError {
      pub code: &'static str,
      pub message: String,
  }

  impl PostProcessError {
      pub fn into_app_error(self) -> AppError {
          AppError::bad_request_code(self.code, format!("后处理 JS 失败：{}", self.message))
      }
  }

  /// timeout_ms: 调用方传入同步或任务超时
  pub fn run_post_process(
      script: &str,
      columns: &[FieldDefinition],
      rows: &[Vec<serde_json::Value>],
      limits: &JsRuntimeLimits,
      timeout_ms: u64,
      http: Option<&JsHttpRuntime>, // Task 4 再实现；本任务可传 None 且不注入 http
  ) -> Result<PostProcessOutput, PostProcessError>;

  pub fn normalize_post_js(raw: Option<&str>) -> Option<String> {
      raw.map(str::trim).filter(|s| !s.is_empty()).map(str::to_owned)
  }
  ```

- [ ] **Step 1: 添加依赖并确认编译**

`Cargo.toml`:

```toml
rquickjs = { version = "0.12", features = ["bindgen", "classes", "properties"] }
```

若 bindgen 在环境失败，改用 crate 文档推荐的 prebuilt feature 组合，以 CI 可构建为准。

```bash
cargo check --manifest-path backend/Cargo.toml --locked
```

- [ ] **Step 2: 写失败单测（引擎未实现前可先写测试模块）**

在 `post_process.rs`：

```rust
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
    let err = run_post_process("function process(r){return r}", &[], &[], &limits, 1000, None).unwrap_err();
    assert_eq!(err.code, "post_js_limit_script");
}
```

`field`/`test_limits`/`json` 为测试 helper。

- [ ] **Step 3: 实现引擎**

要点：

1. 校验 `script.len() <= max_script_bytes`、`rows.len() <= max_input_rows`、估算 payload（列名+值 JSON 近似）`<= max_input_payload_bytes`。
2. `rquickjs::Runtime` + `Context`；配置 interrupt / memory 若 API 支持（`runtime.set_memory_limit`、`runtime.set_interrupt_handler` 基于 `Instant` deadline = now + timeout_ms）。
3. 行列 → 对象数组：`columns[i].name` → key。
4. `eval` 用户脚本；`globals.get("process")` 为 function，否则 `post_js_no_process`。
5. 调用 `process(rows, meta)`；收集返回。
6. 校验返回为数组；元素为 object；行数 `<= max_output_rows`。
7. 列并集顺序 + 类型粗推断（bool / 整数 / 小数 / 文本；全 null → 文本）。
8. `console.log`：注入简单 console 对象，push 到 `Vec` 截断。
9. 本任务 **不**注入 `http`（或注入后调用即 `post_js_http_disabled` 占位，Task 4 替换）。

语法错误 → `post_js_syntax`；中断 → `post_js_timeout`。

- [ ] **Step 4: 跑测**

```bash
cargo test --manifest-path backend/Cargo.toml --locked post_process -- --nocapture
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/Cargo.toml backend/Cargo.lock backend/src/services/post_process.rs backend/src/services/mod.rs
git commit -m "feat: add QuickJS post-process engine without HTTP"
```

---

### Task 4: `http.request` host 与白名单集成

**Files:**
- Modify: `backend/src/services/post_process.rs`
- Test: 同文件；使用 `tokio` 或 `std::net::TcpListener` + 线程 mock HTTP（参考 `agent_provider` 测试里本地 listener 写法）

**Interfaces:**
- Produces:
  ```rust
  pub struct JsHttpRuntime {
      pub client: reqwest::blocking::Client, // 或在 blocking 上下文用 runtime.block_on(async client)
      pub limits: JsHttpLimits, // 从 JsRuntimeLimits 切片
      pub allowlist: Vec<AllowlistEntry>,
      pub enabled: bool,
      pub allow_private_when_empty: bool,
  }
  ```
  用户 API 与 spec §6.2 一致。

**实现注意：** 整个 `run_post_process` 已在 `spawn_blocking` 中调用时，优先 `reqwest::blocking::Client`（`redirect::Policy::none()`、timeout 每请求设置）。若坚持用 async client，则在 host 回调里 `Handle::current().block_on` **仅当**外层是 async 运行时线程——`spawn_blocking` 线程没有 handle 时会 panic，故 **必须用 blocking client 或自建 short runtime**。计划规定：**blocking Client**。

- [ ] **Step 1: 写 HTTP 单测（mock server）**

用例：

1. 启用 HTTP + 空名单 + mock 绑在 `127.0.0.1` → 默认 **blocked**（`post_js_http_blocked`）。
2. allowlist `127.0.0.1:{port}` 或 `localhost:{port}` → 成功，`process` 把 body 拼进行。
3. `enabled: false` → `post_js_http_disabled`。
4. 超过 `http_max_requests` → `post_js_http_limit`。
5. mock 302 到其它地址 → client 不跟随；脚本看到 302，`ok: false`（不自动 throw）。

- [ ] **Step 2: 实现 host 函数**

- 解析 JS object 参数：method/url/headers/body/timeoutMs。
- 校验 method 白名单；body 字节；timeout clamp。
- `Url::parse` → DNS `std::net::ToSocketAddrs` 或 `dns_lookup`；用 `net_guard::url_allowed`。
- `client.request(...).headers(...).body(...).send()`；读 body 限 `http_max_body_bytes`。
- 返回 plain object：`{ ok, status, headers, body }`（headers 扁平 string map，同名多值用逗号拼接）。
- 剥离脚本设置的 `Host`/`Content-Length`（reqwest 自管）。

请求计数：`Cell<usize>` 或 `RefCell` 放在闭包捕获里。

- [ ] **Step 3: 跑测**

```bash
cargo test --manifest-path backend/Cargo.toml --locked post_process -- --nocapture
```

- [ ] **Step 4: Commit**

```bash
git add backend/src/services/post_process.rs
git commit -m "feat: add sandboxed http.request for post-process JS"
```

---

### Task 5: Migration 与模型/API 字段

**Files:**
- Create: `backend/migrations/0009_query_post_js.sql`
- Modify: `backend/src/models.rs`（QueryRequest/Response, SavedQuery*, Job*, Schedule*, CreateJobRequest, UpsertScheduleRequest）
- Modify: `backend/src/api/saved_queries.rs`
- Modify: `backend/src/api/jobs.rs`（含 `enqueue_job`）
- Modify: `backend/src/api/schedules.rs`
- Modify: `backend/src/api/queries.rs`（若需）
- Modify: 所有 `QueryRequest { ... }` 构造点：`workers.rs`、`agent.rs`、测试

**Migration SQL:**

```sql
ALTER TABLE saved_queries ADD COLUMN post_js TEXT;
ALTER TABLE jobs ADD COLUMN post_js TEXT;
ALTER TABLE schedules ADD COLUMN post_js TEXT;
```

**模型字段：**

- `QueryRequest.post_js: Option<String>`（`#[serde(default)]`）
- `QueryResponse.post_processed: bool`（`#[serde(default)]`）、`post_process_ms: Option<u128>`
- SavedQuery / payload / row：`post_js`；JSON `postJs`
- Job / JobRow / CreateJobRequest：同上
- ScheduleItem / ScheduleRow / UpsertScheduleRequest：同上

`From<Row>` 映射补字段。SELECT 列表全部加上 `post_js`。

`enqueue_job(..., post_js: Option<&str>)`：INSERT 列包含 `post_js`。  
调用方：

- `jobs::create` / `retry`：从 request 或旧 job 行
- `schedules::run_now` / workers schedule enqueue：从 schedule 行
- retry **必须**复制旧 job 的 `post_js`，不读 saved query

- [ ] **Step 1: 写 migration 并 `cargo sqlx` / 启动迁移路径验证**

项目用 `sqlx::migrate!`：加文件后现有 `db::connect` 会自动跑。

- [ ] **Step 2: 改模型与全部 SQL 字符串**

编译驱动：`cargo check` 修到过。

- [ ] **Step 3: 最小 API 往返测试（可选 sqlx 内存库）**

若已有 saved_query 测试模式则扩展；否则在后续 Task 6 用引擎+execution 测。至少保证 `SavedQueryPayload` serde 圆trip：

```rust
#[test]
fn saved_query_payload_accepts_post_js() {
    let v: SavedQueryPayload = serde_json::from_str(
        r#"{"sourceId":"s","tables":[],"name":"n","sql":"select 1","postJs":"function process(r){return r}"}"#,
    ).unwrap();
    assert!(v.post_js.unwrap().contains("process"));
}
```

- [ ] **Step 4: Commit**

```bash
git add backend/migrations/0009_query_post_js.sql backend/src/models.rs backend/src/api/*.rs backend/src/workers.rs backend/src/services/agent.rs
git commit -m "feat: persist optional post_js on queries jobs and schedules"
```

---

### Task 6: `execution` 集成（同步查询 + 后台 artifact）

**Files:**
- Modify: `backend/src/services/execution.rs`
- Modify: `backend/src/services/query_engine.rs`（增加「从最终行列写 artifact」或 post 后重建）
- Modify: `backend/src/workers.rs`（日志文案与 request.post_js）

**同步路径（`execute_request_inner`）伪代码：**

```rust
let mut response = execution.response;
if let Some(script) = post_process::normalize_post_js(request.post_js.as_deref()) {
    let limits = state.js_runtime.clone();
    let timeout_ms = if job_id.is_some() { limits.job_timeout_ms } else { limits.timeout_ms };
    let http = JsHttpRuntime::from_state(&state); // blocking client 可每次新建或 lazy
    let columns = response.columns.clone();
    let rows = response.rows.clone();
    let started_sql_ms = response.elapsed_ms;
    // 已在 spawn_blocking 外的 async 上下文：再 spawn_blocking 跑 JS，或把 JS 并入同一个 blocking 闭包。
    let out = tokio::task::spawn_blocking(move || {
        post_process::run_post_process(&script, &columns, &rows, &limits, timeout_ms, Some(&http))
    })
    .await
    .map_err(|e| AppError::Internal(e.to_string()))?
    .map_err(|e| e.into_app_error())?;
    response.columns = out.columns;
    response.rows = out.rows;
    response.row_count = response.rows.len();
    response.post_processed = true;
    response.post_process_ms = Some(out.elapsed.as_millis());
    response.elapsed_ms = started_sql_ms + out.elapsed.as_millis();
    // truncated: 后处理后按新行数；若曾截断 SQL 前端 limit，保持 truncated 语义为「SQL 阶段是否截断」或统一 false——规定：post 后 truncated = false 除非输出仍被额外截断（不截断）。
    response.truncated = false;
}
Ok(response)
```

**更优结构（推荐实现）：** 把 post-process 放进 **同一个** `spawn_blocking`，在 `query_engine::execute_query` 返回后立刻跑 JS，避免二次线程切换，且 HTTP blocking 合法。即扩展 `execute_request_inner` 的 closure：

```rust
let post_js = post_process::normalize_post_js(request.post_js.as_deref());
let js_limits = state.js_runtime.clone();
let js_timeout = ...;
let http_parts = state.js_http_parts(); // allowlist clone + flags；client 在 closure 内 build
let handle = tokio::task::spawn_blocking(move || {
    let mut execution = query_engine::execute_query(...)?;
    if let Some(script) = post_js {
        let http = JsHttpRuntime::new(&js_limits)?;
        let out = post_process::run_post_process(
            &script,
            &execution.response.columns,
            &execution.response.rows,
            &js_limits,
            js_timeout,
            Some(&http),
        ).map_err(|e| anyhow::anyhow!("{}: {}", e.code, e.message))?;
        // merge into execution.response
    }
    Ok(execution)
});
```

将 `PostProcessError` 映射为 `AppError::bad_request_code` 时，在 async 边界用 code 字符串传出（`anyhow` 会丢 code）——**不要**只用 anyhow 字符串。改为：

```rust
enum BlockingQueryError {
    Engine(anyhow::Error),
    Post(PostProcessError),
}
```

或让 `run_post_process` 错误在 blocking 外匹配。

**Artifact 路径（关键）：** 当前 `execute_query_to_artifact` 用 SQL `CREATE TABLE result AS SELECT ...`。后处理必须作用于**完整结果**，不是 sample：

1. SQL 写入临时 artifact `result` 表（现有）。
2. 若无 post_js：现逻辑。
3. 若有 post_js：
   - 从 artifact 读出全部行（受 `max_input_rows` 限制；超过 fail `post_js_limit_input_rows`）——可用 DuckDB `SELECT * FROM result`。
   - 跑 `run_post_process`。
   - **重写** artifact：删除旧 `result`，按输出 columns 建表并插入所有输出行（类型用简单 DuckDB 类型映射：TEXT/BIGINT/DOUBLE/BOOLEAN），再 CHECKPOINT。
   - sample 从新表 LIMIT 200 构建；`total_rows = output len`；`post_processed` 写入 sample 响应字段。

在 `query_engine` 增加：

```rust
pub fn replace_artifact_with_rows(
    artifact_path: &Path,
    columns: &[FieldDefinition],
    rows: &[Vec<Value>],
    runtime: &QueryRuntimeLimits,
) -> Result<()>
```

或在 `execute_query_to_artifact` 增加可选 `post_js` 参数与 `JsRuntimeLimits`。

**Workers：**

```rust
let request = QueryRequest {
    ...
    post_js: job.post_js.clone(), // JobRow 新字段
};
// 日志：
append_log(..., "正在执行 DuckDB 查询").await?;
// 成功后若 post：在 execution 内无法直接 append_log；可在 workers 根据 response.post_processed 再写一条
// 「后处理 JS 完成，N ms」或失败错误信息已在 error_message
```

在 `claim_and_run_job` match Ok 时：

```rust
if result.sample.post_processed {
    append_log(&state, &id, "info", &format!(
        "后处理 JS 完成，{} ms",
        result.sample.post_process_ms.unwrap_or(0)
    )).await?;
}
```

`QueryArtifactExecution.sample` 类型是 `QueryResponse`，已含新字段。

- [ ] **Step 1: 实现同步路径集成 + 单测**

单测可用 tempfile + 最小 CSV 源（复制 `query_engine` 测试模式）或纯 mock：对 `run_post_process` 已测的前提下，execution 集成测至少一条「带 post_js 的 QueryRequest 改列」。

若全链路重，最少：

```rust
#[test]
fn normalize_post_js_trims() {
    assert!(normalize_post_js(Some("  ")).is_none());
    assert_eq!(normalize_post_js(Some(" function process(r){return r} ")).unwrap().starts_with("function"), true);
}
```

并手工 `cargo test` query_engine 回归。

- [ ] **Step 2: 实现 artifact 重写路径**

- [ ] **Step 3: workers / schedule enqueue 传 post_js**

检查 `workers.rs` schedule 分支 `enqueue_job` 参数。

- [ ] **Step 4: 全量后端测试**

```bash
cargo test --manifest-path backend/Cargo.toml --locked
cargo clippy --manifest-path backend/Cargo.toml --locked --all-targets -- -D warnings
```

- [ ] **Step 5: Commit**

```bash
git add backend/src/services/execution.rs backend/src/services/query_engine.rs backend/src/workers.rs
git commit -m "feat: run post-process JS after SQL in query and job paths"
```

---

### Task 7: 前端类型、API 与 store

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/stores/workspace.ts`
- Modify: `frontend/src/stores/tasks.ts`
- Modify: `frontend/src/monaco.ts`（加载 javascript contribution）

**类型增量：**

```ts
// QueryResponse
postProcessed?: boolean
postProcessMs?: number | null

// SavedQuery, SavedQueryPayload, Job, ScheduleItem, SchedulePayload
postJs?: string | null
```

`api.runQuery` / `createJob` / saved query / schedule payload 增加可选 `postJs`。

**workspace store：**

```ts
const currentPostJs = ref('')
// runQuery:
queryResult.value = await api.runQuery({
  sourceId: ...,
  tables: ...,
  sql: currentSql.value,
  postJs: currentPostJs.value.trim() || undefined,
})
// load saved:
currentPostJs.value = query.postJs ?? ''
// save payload include postJs: currentPostJs.value
// insertFormula 不碰 postJs
```

导出 `currentPostJs`。

**monaco.ts：** 在 Promise.all 中增加：

```ts
import('monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js'),
```

SqlEditor 已支持 `language` prop，工作台传 `language="javascript"`。

- [ ] **Step 1: 改 types + api + stores + monaco**

- [ ] **Step 2: `pnpm --dir frontend exec vue-tsc --noEmit`（或项目既有脚本）**

```bash
cd frontend && pnpm exec vue-tsc -b --pretty false
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types.ts frontend/src/api.ts frontend/src/stores/workspace.ts frontend/src/stores/tasks.ts frontend/src/monaco.ts
git commit -m "feat: thread postJs through frontend API and workspace state"
```

---

### Task 8: 工作台与任务 UI

**Files:**
- Modify: `frontend/src/views/WorkbenchView.vue`
- Modify: `frontend/src/views/TasksView.vue`
- 可选 Create: `frontend/src/components/PostJsPanel.vue`（若 Workbench 过大则拆）

**Workbench：**

1. SQL `SqlEditor` 下增加折叠面板：
   - 标题：`后处理 JS（可选）`
   - 非空 `currentPostJs` 时圆点「已启用」
   - `v-show` / 展开状态 `postJsOpen`
   - 首次展开且 trim 为空时写入模板：
     ```js
     function process(rows, meta) {
       // rows: 对象数组；返回对象数组
       // 可用 http.request({ method, url, headers, body, timeoutMs })
       return rows
     }
     ```
   - `SqlEditor` `language="javascript"`，`v-model="store.currentPostJs"`，高度约 160px
   - 说明小字：spec §6 UI 文案
2. 创建任务 dialog：`api.createJob({ ..., postJs: store.currentPostJs.trim() || undefined })`
3. 结果工具条：`queryResult.postProcessed` 时显示 `已后处理 · ${postProcessMs}ms`
4. 错误：`errorMessage` 已含后端中文；若 `error.code` 以 `post_js_` 开头可前缀强调（若前端 api 客户端解析 `error.code`）

检查 `frontend/src/api.ts` 错误解析是否暴露 `code`；若只有 message，则依赖后端 message 即可。

**TasksView：**

- 任务详情：SQL 只读块下，若 `job.postJs` 显示 `<pre>` 脚本
- 创建任务表单：增加可选 JS 文本域（或从当前无工作台上下文则可选空）；`createJob` 传 `postJs`
- 计划创建/编辑：payload 增加 `postJs`；编辑回填

- [ ] **Step 1: 实现 Workbench UI**

- [ ] **Step 2: 实现 Tasks UI**

- [ ] **Step 3: 类型检查与 build**

```bash
cd frontend && pnpm exec vue-tsc -b --pretty false && pnpm build
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/WorkbenchView.vue frontend/src/views/TasksView.vue frontend/src/components/PostJsPanel.vue
git commit -m "feat: add post-process JS editor and task display"
```

---

### Task 9: 文档与验收

**Files:**
- Modify: `docs/14-rust-vue-rewrite.md`（§3 工作台增加后处理 JS 一条；§4 模块表加 `post_process`/`net_guard`；§5 API 字段说明）
- Modify: `README.md` 若有功能列表则补一句
- Modify: `.env.example`（若 Task 2 已写则复核）

**验收清单（手工或脚本）：**

1. 不传 postJs：旧查询/保存/任务仍成功。
2. 工作台脚本 `return rows.filter(...)`：结果行变化，标签「已后处理」。
3. 保存查询再载入：脚本恢复。
4. 后台任务带脚本：日志有后处理完成；结果页为最终表。
5. 脚本 `throw`：失败信息含「后处理 JS」。
6. `http.request` 打 `http://127.0.0.1:9`：默认失败 blocked。
7. 配置 allowlist 后本地 mock 成功（开发机）。

- [ ] **Step 1: 更新文档**

- [ ] **Step 2: 最终测试门禁**

```bash
cargo fmt --manifest-path backend/Cargo.toml --all -- --check
cargo test --manifest-path backend/Cargo.toml --locked
cargo clippy --manifest-path backend/Cargo.toml --locked --all-targets -- -D warnings
cd frontend && pnpm exec vue-tsc -b --pretty false && pnpm build
```

- [ ] **Step 3: Commit**

```bash
git add docs/14-rust-vue-rewrite.md README.md .env.example
git commit -m "docs: document Quick JS query post-processing"
```

---

## Spec coverage checklist

| Spec 项 | Task |
| --- | --- |
| process(rows,meta) 契约 / 列并集 / 类型推断 | 3 |
| 限额与失败不截断 | 2, 3 |
| http.request + 白名单 R1 + 不强制 HTTPS + 无重定向 | 1, 4 |
| net_guard 与 AI 共用 | 1 |
| Query/saved/jobs/schedules 字段与快照 | 5 |
| 同步 + job artifact 最终表 | 6 |
| 错误码 | 1, 3, 4 |
| 工作台 UI / 任务展示 | 7, 8 |
| Agent 不接入 | 5（构造点不传）、全局约束 |
| 配置与 .env.example | 2, 9 |
| 测试 | 各 Task |

## 实现时注意

- **Artifact 必须后处理全量行**，不要只处理 sample 200 行。
- **`QueryResponse` 新增字段**要 `serde(default)`，避免旧 `result_json` 反序列化失败。
- 所有 `AppState { ... }` 测试夹具同步新字段。
- `enqueue_job` 签名变更编译器会列出全部调用点。
- rquickjs 与 `edition = "2024"` 若冲突，在 Task 3 调整 feature/版本并记入 commit message。
