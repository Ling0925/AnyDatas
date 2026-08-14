# 13 Rust/Vue 单机 MVP 实现验收清单

更新日期: 2026-07-18

## 1. 验收结论

AnyDatas 当前可验收范围是“桌面优先、单机部署的 Excel/CSV 在线分析工作台”。产品主路径已经收敛为:

1. 上传 Excel 或 CSV。
2. 按文件、Sheet 和单元格范围组织逻辑表。
3. 预览字段与数据。
4. 使用 DuckDB SQL 跨文件、跨 Sheet 查询。
5. 保存查询、查看表格/图表或导出 CSV。
6. 将耗时查询放入后台，并可配置计划运行。

当前活跃实现位于 `backend/` 与 `frontend/`。历史 Python 代码只作为迁移参考，不计入本轮已交付能力。

## 2. 已交付能力

| 范围 | 已交付 |
| --- | --- |
| 身份 | 首次 Owner 初始化、密码登录、退出、HttpOnly 会话、失败登录限流 |
| 工作区权限 | Owner/Admin/Analyst/Viewer，资源按工作区隔离，写操作要求 Analyst 及以上 |
| 文件 | `.xlsx`、`.xls`、`.xlsb`、`.ods`、`.csv` 上传、列表和删除 |
| 逻辑表 | 每个 Sheet 自动建表，同 Sheet 额外范围，起止单元格、表头和字段推断 |
| 交互查询 | 最多 16 张逻辑表、可编辑别名、自连接、DuckDB `SELECT`/`WITH` |
| 性能 | 无源行数硬上限、CSV 流式导入、不可变单表 DuckDB 缓存和重复查询复用 |
| 查询资产 | 保存查询增删改查，SQL 与有序表绑定同时持久化 |
| 结果 | 表格、柱状图、折线图、饼图、公式安全 CSV 导出，前端最多返回 5000 行 |
| 后台任务 | SQLite 持久队列、状态、进度、日志、结果、取消、重试和删除 |
| 计划任务 | Cron、时区、启停、编辑、立即运行和删除，多表绑定随计划保存 |
| 部署 | 单 Rust 进程、单容器、单持久卷、健康检查和 SPA history fallback |
| Web 体验 | 中文桌面三栏工作台、文件-Sheet 树、绑定栏、Monaco 补全和任务管理页 |

## 3. 模块边界

| 模块 | 职责 |
| --- | --- |
| `backend/src/api/auth.rs` | 身份、会话、登录限流和工作区上下文 |
| `backend/src/api/data_sources.rs` | 物理文件上传、列表、删除和旧接口兼容 |
| `backend/src/api/source_tables.rs` | 逻辑表配置、范围预览、创建和删除 |
| `backend/src/services/spreadsheet.rs` | Excel/CSV 范围读取、表头去重和类型推断 |
| `backend/src/services/query_bindings.rs` | 多表绑定校验与关系表事务写入 |
| `backend/src/services/query_engine.rs` | DuckDB 缓存、只读挂载、查询、中断和结果转换 |
| `backend/src/workers.rs` | 后台队列消费和计划到期入队 |
| `frontend/src/stores/workspace.ts` | 文件浏览、逻辑表预览、查询上下文和保存查询状态 |
| `frontend/src/views/WorkbenchView.vue` | SQL/预览双 Tab、绑定栏和结果区 |
| `frontend/src/views/TasksView.vue` | 任务记录、详情、计划和多表任务表单 |

## 4. 自动质量门禁

```bash
cargo fmt --manifest-path backend/Cargo.toml --all -- --check
python3 scripts/with-duckdb-prebuilt.py -- cargo test --manifest-path backend/Cargo.toml --locked
python3 scripts/with-duckdb-prebuilt.py -- cargo clippy --manifest-path backend/Cargo.toml --locked --all-targets -- -D warnings
pnpm --dir frontend run build
docker compose config --quiet
```

当前 Rust 单元测试共 12 个，覆盖:

- 身份字段、密码散列与验证。
- 单元格引用、起止范围、重复表头和 CSV 流式读取。
- 只读 SQL 拦截与具体 DuckDB 编译错误。
- 基础聚合、跨两个文件 JOIN、同表双别名自连接。
- 超过历史 20 万行限制的数据查询。

Vue 已通过 `vue-tsc` 和 Vite 生产构建。大体积 Monaco、Element Plus 和 ECharts chunk 仍有构建警告，但不影响当前功能验收；代码拆分属于后续性能项。

## 5. 真实验收记录

隔离实例使用两份各 500000 行、6 字段的合成信用卡审批 CSV，已验证:

- 上传和逻辑表创建成功，共处理 1000000 行源数据。
- 跨文件聚合 JOIN 返回正确分组结果。
- 缓存命中后的服务端查询耗时 27–28 ms。
- 浏览器结果显示两个文件行数均为 500000。
- 保存查询回读完整 `tables` 数组和别名。
- 多表后台任务成功返回 2 行，任务详情显示两张表和运行日志。
- 高基数 JOIN 可以从前端/HTTP 真正取消，不只是修改状态。
- 禁用计划成功保存两张表绑定。

Chromium 以 1440x900 桌面视口检查:

- 文件-Sheet 树、查询绑定栏、Monaco、结果表和字段检查器无重叠。
- 双表绑定和别名修改后可从 UI 运行查询。
- 任务列表、任务详情和多表任务对话框无文本溢出。
- 浏览器控制台 0 错误、0 警告。

移动端不属于本轮验收范围。

## 6. 单机发布门禁

发布前先对活动 SQLite 数据库做在线一致性备份，再执行:

```bash
docker compose config --quiet
docker compose up --build -d
curl --fail http://127.0.0.1:28080/api/health
docker compose ps
```

发布后至少检查:

1. 原账号仍可登录并可退出。
2. 原文件、保存查询、任务和计划仍存在。
3. 每个历史文件已回填默认逻辑表。
4. 旧 `FROM data` 查询仍可运行。
5. 新上传 Excel 的全部 Sheet 都显示在左侧文件树。
6. 两张逻辑表可以完成一次 JOIN。
7. 后台任务可成功运行并可取消。

回滚时应恢复升级前镜像和 SQLite 在线备份；上传目录与原始文件不得删除。

## 7. 明确延期项

以下能力不计入当前完成范围:

- 成员管理页面、多工作区切换、邀请、SSO、MFA 和 SCIM。
- 持久 Dashboard/Report、XLSX/PDF 导出、分享和订阅。
- PostgreSQL/MySQL/ClickHouse、S3/MinIO 和外部连接器。
- Python 运行时、任意依赖环境和用户代码容器隔离。
- 可视化 JOIN 画布、Notebook、实时协作和 Git 同步。
- 缓存 GC、缓存构建进度、超大 Excel 异步 Parquet 转换。
- Redis、多 Worker、Kubernetes、Temporal、多节点高可用和灾备。

这些能力需要独立需求和安全设计，不应重新挤占当前“上传表格并完成分析”的主路径。
