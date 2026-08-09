---
name: anydatas-backend-patterns
description: Conventions for safely extending the AnyDatas Rust/Axum backend — adding an API endpoint, a SQLx migration, a query/DuckDB code path, a background job type, or an AI agent tool. Use when writing or reviewing backend/ code so new code matches the codebase's tenant-isolation, error-handling, resource-permit, SQL-safety, and testing conventions.
---

# AnyDatas 后端扩展规范

后端刻意做到「安全边界集中、路径一致」。新增代码必须复用既有原语，否则会绕过隔离/资源/安全控制。以下每条都对应仓库里的既有做法。

## 加一个 API 端点
1. 在对应 `backend/src/api/<feature>.rs` 建 `Router<SharedState>`，在 `api/mod.rs::router` 合并。
2. handler 一律返回 `AppResult<T>`（`Json`/元组）。**绝不**手写 HTTP 状态或把 `sqlx`/`io` 错误直接回给客户端——`AppError::into_response` 会映射为 `{error:{code,message}}` 并对内部错误打日志后脱敏为 500。
3. 需要身份就用 `AuthContext` 提取器（`FromRequestParts`，自动校验会话 cookie）。写操作调 `ctx.require_analyst()`；工作区级基础设施配置调 `ctx.require_admin()`。
4. **租户隔离不可省**：任何按 id 取资源的查询都要带 `workspace_id`（复用 `db.rs` 里 `(? IS NULL OR workspace_id = ?)` 的既有 helper）。跨工作区访问应表现为 404，不是 403。

## 加一个 SQLx 迁移
- 新增 `backend/migrations/000N_<name>.sql`，`PRAGMA foreign_keys = ON;` 开头，启动时 `sqlx::migrate!` 自动执行。
- 时间戳统一存 RFC3339 `TEXT`。
- 用 **partial unique index** 编码不变量（参考 `idx_ai_runs_one_active`「一会话一活跃 Run」、一源一默认表）。
- 更新 `docs/14 §4` 的迁移表——注意目前该表有漂移（把 `0007` 写成 query_governance，实际是 `agent_reasoning_effort`），别照抄。

## 加一条查询 / DuckDB 路径
- **不要**新开裸 DuckDB 连接执行用户 SQL。经 `execution.rs` → `query_engine.rs`：它负责租户校验、`acquire_permit(query_semaphore)`、`spawn_blocking`、超时 + `InterruptHandle` 取消。
- 许可（`OwnedSemaphorePermit`）必须**移动进 blocking 闭包**，让 HTTP 超时不会提前释放并发槽（见 `resource_control.rs`）。
- 用户 SQL 必须过 `validate_read_only_sql`（单条 SELECT/WITH），且连接建好后 `SET enable_external_access=false` + 禁扩展自动加载 + 只读 ATTACH。
- 拼 SQL 标识符/字面量用 `quote_identifier` / `quote_string_literal`，**绝不**用裸 `format!` 拼用户可控的表名/别名/路径。
- **已知坑**：`validate_read_only_sql` 的关键字黑名单会误伤字符串字面量；缓存键哈希无分隔符有碰撞风险；CSV 大整数走 f64 会损坏 >2^53 的值——改这些路径时先读 `docs/review/CODE-REVIEW.md`（H1/M4/M6）。

## 加一种后台任务
- 经 `api::jobs::enqueue_job`（同事务写 `queued` job + 绑定 junction 行）。**不要**直接写 job 表。
- worker 认领用 `UPDATE ... WHERE id=? AND status='queued'` 原子模式并查 `rows_affected`。
- 写终态时**加状态守卫**（`WHERE id=? AND status='running'`）——否则会与取消竞态（见评审 M5/M8）。任何认领后的错误路径都要落 `failed` 终态并清理制品。
- 完整结果写 `job-results/<uuid>.duckdb` 独立制品（分页/流式 CSV），SQLite 只存有界样本 + 元数据。制品 key 必须是 UUID，`job_results::artifact_path` 用 `Uuid::parse_str` 拦路径穿越——新的「DB 值→文件路径」代码都要照此校验。

## 加一个 AI Agent 工具
- 在 `services/agent.rs` 的工具注册表加**显式**条目（参考 `preview_sql`/`inspect_table`），用静态 name/description/JSON Schema。
- 工具必须**只读**且复用查询引擎安全边界；参数在完整拼接后才校验；未知工具/坏参数返回「失败工具观察」让模型自我修正，绝不动态派发。
- 不得把原始文件、整表、服务器路径、DuckDB 缓存键或全部结果塞进上下文；样本要在后端二次裁剪。
- 别名/表只能来自服务端当前绑定（`context_signature`），不能由模型任意指定。

## 调外部 HTTP（模型/provider）
- 复用 `state.http_client`（已 `redirect::Policy::none()`）。
- 目标 URL 必须过 `validate_base_url_network`（DNS 解析后逐 IP 拒私网/保留段、公网强制 HTTPS、拒 URL 内嵌凭据）。
- **修复中的坑**：当前是先校验主机名再由 reqwest 二次解析，有 DNS-rebinding TOCTOU（评审 M1）；新代码应把连接钉到已校验 IP。
- 密钥经 `services::secrets`（AES-256-GCM）解密，只进内存、不回显、不落日志。

## 测试约定
- 用 `#[tokio::test]` + `agent.rs` 里的 `seeded_agent_state`（TempDir + pool）夹具做 hermetic 测试。
- 守卫函数（路径校验、缓存键校验、别名/表数上限、状态机）应有直接单元测试——参考 `maintenance.rs::accepts_only_sha256_cache_keys`。
- 目前 `workers.rs`、`execution.rs`、`schedules::next_run`、`job_results` 无测试，且无 upload→query→job→schedule 集成测试（评审 M13/M14/M15）——新增相关代码时顺手补。
- 提交前跑 `cargo fmt --check`、`cargo clippy -D warnings`、`cargo test --locked`（CI 会 block）。
