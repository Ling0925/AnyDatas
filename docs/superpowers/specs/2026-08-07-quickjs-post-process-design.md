# Quick JS 查询后处理设计

日期: 2026-08-07  
状态: 待实现  
范围: Rust/Vue 活跃实现（`backend/` + `frontend/`）

## 1. 背景与目标

AnyDatas 当前主路径是「逻辑表 → DuckDB 只读 SQL → 结果表/图/任务」。计算字段只是把 DuckDB 表达式写入 SQL，没有通用脚本能力；Python 运行时与插件体系尚未迁到 Rust 版。

本设计增加可选的 **服务端 QuickJS 后处理**：SQL 成功后，用一段用户 JS 对结果集做过滤、派生、整形，并可按部署策略发起 HTTP(S) 请求补充外部数据。

### 目标

- 工作台查询增加可选「后处理 JS」步骤；空脚本时行为与现网完全一致。
- 同步查询、保存查询、后台任务、定时计划共用同一脚本语义与快照规则。
- 进程内嵌入 QuickJS，不引入 Docker socket、不引入插件市场。
- 自托管友好：默认可访问公网 HTTP(S)；管理员可用白名单收紧或点名内网 API。
- 图表、CSV、任务 artifact 一律消费后处理后的最终表。

### 非目标（本轮不做）

- 通用插件加载器、第三方包安装、插件市场。
- 行级流式变换、JS 内再次查询 DuckDB、异步 `process` / 浏览器 `fetch` 语义。
- Python / Wasm 运行时。
- Agent 自动读写 `postJs`。
- 工作区 UI 维护 HTTP 白名单（P1）；密钥引用自动注入 header（P1）。
- 任意 TCP/WebSocket/SMTP；宿主仅提供 HTTP(S) 客户端。

## 2. 产品形态

### 2.1 用户心智

SQL 仍是主路径。需要 SQL 不擅长或需外部 API 的步骤时，展开「后处理 JS」，实现：

```js
function process(rows, meta) {
  // rows: 对象数组
  // meta: { columns, columnTypes?, rowCount }
  return rows
}
```

运行 = 先 SQL，再（若有脚本）JS；保存查询 / 后台任务 / 定时会一并带上脚本。

### 2.2 工作台 UI

- 位置：查询 Tab 中，SQL 编辑器下方、结果区上方，**默认折叠**。
- 有非空脚本时显示「已启用」标记。
- 首次展开且内容为空时插入占位模板（不覆盖已有内容）。
- Monaco `javascript` 与 SQL 编辑体验对齐（若包体策略不允许，实现阶段可降级，但设计偏好 Monaco）。
- 运行按钮携带当前 `postJs`（trim 后为空则不传）。
- 保存 / 载入保存查询：读写 `postJs`。
- 创建后台任务：使用工作台当前脚本快照，对话框不单独再编辑。
- 成功且经过后处理：结果工具条标签「已后处理 · Nms」。
- 失败：banner 区分 SQL 失败与后处理失败（稳定错误码 + 中文说明）。
- 右侧 DuckDB「计算字段」不变；与 JS 后处理并存，文档一句话区分（SQL 内 vs 结果后）。

### 2.3 任务与计划 UI

- 任务详情：SQL 下方只读展示后处理脚本；无则隐藏。
- 步骤日志：`sql` 阶段之后增加 `post_js` 阶段。
- 计划：与 SQL 相同的可编辑性；触发入队时拷贝 `post_js` 到 job 快照。

### 2.4 Agent

MVP 不读写 `postJs`。应用 SQL 提案只改 SQL。

## 3. 架构

```
表绑定 → 只读 SQL (DuckDB) → rows + columns
                ↓ post_js 非空
         PostProcessEngine (QuickJS)
                ↓ 可选 http.request（Rust host）
         最终 columns + rows → 响应 / artifact / 图表 / CSV
```

| 组件 | 职责 |
| --- | --- |
| `PostProcessEngine`（新） | 隔离 runtime、执行 `process`、限额、错误归一化、注入 `http` |
| `query_engine` / `execution` | SQL 成功后按需调用引擎；输出以引擎为准 |
| 共用 `net_guard` | 从 AI URL 校验抽取的地址分类 + JS HTTP 白名单匹配 |
| SQLite | `saved_queries` / `jobs` / `schedules` 可空 `post_js` |
| 前端工作台 | 折叠 JS 编辑区；API 字段往返 |

每次调用新建 QuickJS runtime，不跨请求复用。不把 `AppState`、DB、文件系统路径直接传入 VM。

推荐 crate：`rquickjs`（实现计划锁定版本）。要求：进程内、可中断/超时、无默认危险 host 绑定。

## 4. 脚本契约

### 4.1 入口

- 必须定义全局函数 `process`。
- 同步调用、同步返回；不支持 `async process`。
- 参数：
  - `rows`: `Array<Record<string, null | boolean | number | string>>`
  - `meta`: `{ columns: string[], columnTypes?: Record<string, string>, rowCount: number }`
- 返回值：对象数组。列名 = 所有行 key 的并集；顺序以第一行 key 为准，后续行新 key 按首次出现追加。
- 缺失 key / `null` → 空单元格。
- `return []` → 空表，`columns = []`。
- 非对象数组、循环引用、不可 JSON 序列化 → `post_js_bad_return` 或等价失败。

### 4.2 进入 VM 前的行格式

DuckDB 结果在 Rust 侧转为对象数组再注入 JS（用户写 `row.amount`）。返回后再转为现有 `columns + rows` 行列格式，消费方（表格/图表/CSV）无需分支。

类型粗推断：全 null → text；首个非空值为 bool/int/float → 对应类型；混合 → text。日期时间保持字符串。

### 4.3 全局能力

- 保留：`JSON`、`Math`、`Date`、`Array`、`Object`、`String`、`Number`、`Boolean`、`RegExp`、`Map`、`Set`、有限的 `console`。
- 注入：`http`（见 §6）。
- 不提供：`setTimeout` / `setInterval`、文件系统、环境变量、工作区密钥自动注入、动态加载外部 JS 模块（宿主包）。
- `console.log/warn/error`：写入本次查询/任务日志，条数与单条长度截断（默认 50 × 500 字）。

## 5. 数据模型与 API

### 5.1 迁移

`backend/migrations/0009_query_post_js.sql`（名称以落地时序号为准）：

- `saved_queries.post_js TEXT NULL`
- `jobs.post_js TEXT NULL`
- `schedules.post_js TEXT NULL`

`NULL` 或仅空白 = 不启用后处理。

### 5.2 请求/响应字段

JSON 使用 camelCase，与现有 API 一致。

| 接口 | 变更 |
| --- | --- |
| `POST /api/query` | body 可选 `postJs?: string`；响应增加 `postProcessed: boolean`、`postProcessMs?: number` |
| `POST/PUT/GET saved-queries` | 读写 `postJs` |
| `POST/GET jobs` | 创建时快照 `postJs`；详情返回；retry 用快照 |
| `POST/PUT/GET schedules` | 读写 `postJs`；触发入队时拷贝到 job |

老客户端不传字段 → 行为不变。

### 5.3 执行流（同步）

1. 规范化绑定与只读 SQL（现有逻辑）。
2. 规范化 `postJs`：trim，空 → `None`。
3. DuckDB 执行；失败则不进入 JS。
4. 若有脚本：检查脚本大小、进入行数、载荷体积 → `PostProcessEngine::run`。
5. 成功：用返回行列构造响应，`postProcessed: true`。
6. 失败：整次查询失败，不返回「仅 SQL」半结果。

后台 job：SQL 日志 → 可选 JS 日志 → 以最终表写 artifact。取消：SQL 阶段沿用现有中断；JS 阶段依赖超时与引擎 interrupt。

### 5.4 权限与审计

- 写/跑带 `postJs` 的接口：与对应 SQL 写/跑权限相同（Analyst+ 写；Viewer 策略与现网只读查询一致）。
- 审计若记录查询/任务：增加 `post_js_hash`（SHA-256 截断）与 `post_processed`；高基数日志不打全文。任务详情 UI 可展示全文。

### 5.5 错误码

| code | 含义 |
| --- | --- |
| `post_js_syntax` | 解析/编译失败 |
| `post_js_no_process` | 未定义 `process` |
| `post_js_throw` | 运行期 throw（message 截断） |
| `post_js_timeout` | 超时 |
| `post_js_limit_script` | 源码过大 |
| `post_js_limit_input_rows` | 进入行数/载荷超限 |
| `post_js_limit_output_rows` | 返回行数超限 |
| `post_js_bad_return` | 返回值不合法 |
| `post_js_http_disabled` | 部署关闭脚本 HTTP |
| `post_js_http_blocked` | URL 未过白名单或命中默认受限地址 |
| `post_js_http_limit` | 次数/体积超限 |
| `post_js_http_error` | 传输层失败（DNS/连接/超时等） |
| `post_js_internal` | 引擎内部错误 |

前端文案：`后处理 JS 失败：{短说明}`。

## 6. HTTP：host 代发与白名单

### 6.1 原则

- 自托管默认允许脚本访问外部 HTTP(S)。
- QuickJS 内 **不**暴露原始 socket；由 Rust 注入同步 `http.request`。
- **不强制 HTTPS**；`http` 与 `https` 均可（宿主只实现这两种 scheme）。
- 管理员通过 **部署级白名单** 收紧范围或批准内网地址。
- 无 cookie 罐；不自动附带用户会话或 AI Key。
- **不跟随重定向**（防跳转到 metadata 或绕过白名单）。

### 6.2 用户 API

```js
const res = http.request({
  method: 'GET', // GET|POST|PUT|PATCH|DELETE|HEAD
  url: 'https://api.example.com/v1/rates',
  headers: { Accept: 'application/json' },
  body: null,        // string | null
  timeoutMs: 3000    // 可选，受全局上限夹紧
})
// res: { ok, status, headers: Record<string, string>, body: string }
// ok === status 在 200–299；非 2xx 不自动 throw
```

### 6.3 白名单策略（R1）

| 状态 | 行为 |
| --- | --- |
| `ANYDATAS_JS_HTTP=0` | 禁用所有 `http.request` |
| HTTP 开启且白名单 **为空** | 允许公网；默认拒绝回环、链路本地、云 metadata、RFC1918 等受限地址（与 AI SSRF 分类对齐，见 `is_restricted_address`） |
| HTTP 开启且白名单 **非空** | **仅**允许命中白名单的目标；命中条目中的私网/localhost **视为管理员已批准**，不再要求单独的 private 开关 |
| 未命中 | `post_js_http_blocked` |

空白名单保持开箱可调公网；要锁死出站时配置白名单即可。

### 6.4 白名单条目格式

来源（MVP，部署级）：

- `ANYDATAS_JS_HTTP_ALLOWLIST`：逗号或换行分隔
- `ANYDATAS_JS_HTTP_ALLOWLIST_FILE`：可选文件路径（与 env 合并去重）

条目类型：

```text
# 主机（任意端口与路径）
api.example.com

# 主机:端口
localhost:11434
192.168.1.10:8080

# URL 前缀（含 scheme）
https://api.example.com/v1/
http://192.168.1.10:8080/hooks/
```

匹配：

1. scheme ∈ `{http, https}`，否则拒绝。
2. 白名单非空时，命中任一即过：
   - host：hostname 等值（大小写不敏感）
   - host:port：hostname + 端口
   - URL 前缀：规范化后请求 URL 以该前缀开头
3. 白名单命中后信任管理员目标 IP；仍不跟随重定向。
4. 白名单为空时：DNS 后每个 IP 做受限地址检查；存在受限且未放行则失败。
5. 可选兼容项 `ANYDATAS_JS_ALLOW_PRIVATE_NETWORK=1`：在白名单为空时放宽私网（与 AI 的 private 开关同角色）。白名单非空时不必依赖该开关。

P1：工作区 Owner/Admin 设置页维护名单；与部署名单同时存在时取 **交集**（工作区不能放大机器权限）。

### 6.5 请求级限额

| 项 | 默认 |
| --- | --- |
| 单次 `process` 内 `http.request` 次数 | 8 |
| 单次请求默认超时 | 3000 ms |
| 脚本声明超时上限 | 10000 ms |
| 响应体 | 2 MiB 文本 |
| 请求体 | 1 MiB |
| Header | 允许自定义；`Host` / `Content-Length` 等由客户端规范，禁止脚本破坏性覆盖 |

HTTP 耗时计入整体 JS 超时预算。

完整 HTTP 相关环境变量：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `ANYDATAS_JS_HTTP` | `1` | `0` 禁用 `http.request` |
| `ANYDATAS_JS_HTTP_ALLOWLIST` | 空 | 逗号/换行分隔白名单 |
| `ANYDATAS_JS_HTTP_ALLOWLIST_FILE` | 空 | 从文件追加白名单 |
| `ANYDATAS_JS_ALLOW_PRIVATE_NETWORK` | `0` | 白名单为空时是否放行私网 |
| `ANYDATAS_JS_HTTP_MAX_REQUESTS` | `8` | 单次 process 内最多请求 |
| `ANYDATAS_JS_HTTP_TIMEOUT_MS` | `3000` | 单次默认超时 |
| `ANYDATAS_JS_HTTP_MAX_TIMEOUT_MS` | `10000` | 脚本声明超时上限 |
| `ANYDATAS_JS_HTTP_MAX_BODY_BYTES` | `2097152` | 响应体上限 |
| `ANYDATAS_JS_HTTP_MAX_REQUEST_BODY_BYTES` | `1048576` | 请求体上限 |

### 6.6 实现要点

- 从 `agent_provider` 抽取 `is_restricted_address` 等至共用 `net_guard`（或等价模块），AI 与 JS 共用。
- `http.request` 在 host 函数中调用 reqwest（禁止重定向）；同步桥接回 QuickJS。
- 启动或首次使用时解析 allowlist；非法条目记日志并忽略或使配置校验失败（实现选严格模式并在计划中写明）。

## 7. 资源限额（JS 本体）

| 项 | 环境变量 | 默认 |
| --- | --- | --- |
| 脚本最大字节 | `ANYDATAS_JS_MAX_SCRIPT_BYTES` | 65536 |
| 进入 JS 最大行数 | `ANYDATAS_JS_MAX_INPUT_ROWS` | 20000 |
| JS 输出最大行数 | `ANYDATAS_JS_MAX_OUTPUT_ROWS` | 20000 |
| 同步执行超时 | `ANYDATAS_JS_TIMEOUT_MS` | 5000 |
| 任务执行超时 | `ANYDATAS_JS_JOB_TIMEOUT_MS` | 30000 |
| 堆内存软上限 | `ANYDATAS_JS_MEMORY_MB` | 64 |
| console 条数 | `ANYDATAS_JS_MAX_CONSOLE_LINES` | 50 |
| 进入载荷体积（序列化前估算） | （可并入 input 限制） | 约 32 MiB 量级 |

超限 **失败**，不静默截断。

HTTP 相关环境变量见 §6。

## 8. 模块草图

```rust
// backend/src/services/post_process.rs
pub struct JsLimits { /* script/rows/timeout/memory/http... */ }

pub struct PostProcessInput<'a> {
    pub columns: &'a [String],
    pub rows: Vec<serde_json::Map<String, serde_json::Value>>,
    pub meta: PostProcessMeta,
}

pub struct PostProcessOutput {
    pub columns: Vec<FieldDefinition>,
    pub rows: Vec<Vec<serde_json::Value>>,
    pub elapsed: std::time::Duration,
    pub console: Vec<String>,
}

pub fn run_post_process(
    script: &str,
    input: PostProcessInput<'_>,
    limits: &JsLimits,
    http: &JsHttpContext, // allowlist + client + flags
) -> Result<PostProcessOutput, PostProcessError>;
```

窄接口便于日后增加第二种变换实现，但 MVP 不暴露插件注册表。

## 9. 测试计划

### 引擎

- 过滤、派生列、空数组、列并集顺序。
- 无 `process`、语法错误、throw、坏返回值。
- 超时、输入/输出行数、脚本大小。
- console 截断。

### HTTP

- mock 公网形态 URL 成功。
- 默认拒绝 `127.0.0.1`、`169.254.169.254`。
- 白名单放行 `localhost:port` 后 mock 成功。
- 白名单非空时未列主机失败。
- `ANYDATAS_JS_HTTP=0` 调用失败。
- 超过最大请求次数失败。
- 302 不跟随（不得打到受限地址）。

### API / Worker

- 不传 `postJs` 黄金路径不变。
- 同步查询列被 JS 改写。
- saved_query 往返。
- job 快照与 retry 不读新稿。
- schedule 入队携带脚本。
- artifact/CSV 为最终表。

### 前端

- 折叠/启用标记、占位模板、载入恢复。
- 错误 banner 与「已后处理」标签。

### 权限

- 与现网 SQL 写/跑一致（含 Viewer 边界）。

## 10. 里程碑

1. `PostProcessEngine` + 单测（无 HTTP）。
2. `net_guard` 抽取 + `http.request` + 白名单 + mock 测试。
3. Migration 与 query/saved_queries/jobs/schedules API。
4. Worker 日志阶段与最终 artifact。
5. 前端折叠编辑区与往返。
6. 文档（README / rewrite 文档一节、`.env.example` 配置项）与验收。

## 11. 成功标准

- 不传 `postJs` 时现有查询/任务/计划行为与测试不受影响。
- 用户可在工作台用 JS 过滤/派生，并在白名单策略下用 `http.request` 拼外部字段；保存与定时复用同一脚本。
- 默认无法用脚本打到链路本地 metadata；管理员可用白名单显式批准内网 API。
- 失败具备稳定错误码与可读中文提示。

## 12. 已决问题

| 问题 | 决定 |
| --- | --- |
| 用途 | SQL 结果后处理（非整表 ETL 平台） |
| 执行位置 | 服务端 QuickJS |
| MVP 形态 | 查询可选后处理步骤 |
| 脚本 API | 整表 `process(rows, meta)` |
| 架构方案 | 流水线内嵌 + 窄引擎接口，不做插件市场 |
| 网络 | 默认可 HTTP(S)；同步 host `http.request`；部署级白名单；空名单公网+SSRF 底线；不强制 HTTPS；不跟随重定向 |
| Agent | 不接入 |
| 工作区白名单 UI | P1 |

## 13. 开放实现细节（计划阶段锁定即可）

- `rquickjs` 具体版本与 feature 集、timeout interrupt 写法。
- 同步 query 路径上 blocking HTTP 与 async Axum 的桥接方式（`spawn_blocking` 等）。
- Monaco JS 语言是否与 SQL 同 bundle 或懒加载。
- 迁移文件精确序号（以仓库当时最新 migration 为准）。
- allowlist 非法条目是启动失败还是跳过并告警（偏好：启动时校验并 fail-fast 更安全）。
