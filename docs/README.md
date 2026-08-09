# AnyDatas 在线数据分析平台文档包

更新日期: 2026-08-09

本目录是一套用于启动 AnyDatas 通用在线数据分析平台的基础文档。目标用户是需要上传或连接数据源、编写 SQL/Python 脚本、定时运行计算任务、沉淀报表和分享结果的数据分析师、数据科学家、运营分析、财务分析和工程数据团队。

## 阅读顺序

1. [00 项目总览](00-project-overview.md): 产品定位、核心判断、MVP 边界。
2. [01 市场调研](01-market-research.md): 市场空间、竞品分层、机会点和风险。
3. [02 产品需求文档](02-product-requirements.md): 用户、功能、非功能需求和成功指标。
4. [03 技术选型](03-technical-selection.md): 推荐技术栈和取舍依据。
5. [04 架构设计](04-architecture-design.md): 系统组件、数据流、部署拓扑和核心实体。
6. [05 数据接入与执行设计](05-data-ingestion-and-execution.md): 上传、连接器、脚本运行、调度、沙箱和资源控制。
7. [06 报表功能设计](06-reporting-design.md): Dashboard、Report、订阅、导出和权限。
8. [07 安全治理](07-security-governance.md): 多租户、权限、密钥、审计、代码执行风险和合规路线。
9. [08 开发规划](08-development-roadmap.md): 阶段、里程碑、团队配置和风险。
10. [09 MVP Backlog](09-mvp-backlog.md): 可拆解到研发排期的 Epic、用户故事和验收条件。
11. [10 运维与成本](10-operations-and-cost.md): 环境、SLO、监控、容量、成本模型和故障预案。
12. [11 单机部署方案](11-single-server-deployment.md): 单台服务器部署拓扑、服务清单、资源建议和演进路径。
13. [12 完整开发计划](12-complete-development-plan.md): 基于当前 MVP 的完整路线图、模块计划、里程碑、测试、运维和风险。
14. [13 实现验收清单](13-implementation-acceptance.md): 单机 MVP 的交付范围、模块边界、质量门禁和明确延期项。
15. [14 Rust + Vue 重构设计与实施状态](14-rust-vue-rewrite.md): 当前有效实现、模块边界、API 和验证状态。
16. [15 跨文件与跨 Sheet 分析实现](15-cross-file-sheet-analysis.md): 逻辑表、多表绑定、DuckDB 缓存、兼容迁移和验收证据。
17. [16 导入、图表与 AI SQL 实现](16-import-charts-ai.md): 导入预检、字段类型、多指标图表、AI 上下文、安全和运维。
18. [17 AI Agent Runtime](17-ai-agent-runtime.md): 持久会话、原生工具、流式状态与上下文预算。
19. [18 Electron 本地采集 Agent](18-electron-local-collector.md): 桌面文件源、覆盖更新、下游调度、隔离验收与运行约束。
20. [19 桌面双模式与服务端运行时](19-desktop-runtime-modes.md): 单机下载、远端连接、版本协议、GitHub Release 与进程生命周期。
21. [sources](sources.md): 调研和技术引用来源。

`00` 到 `10`、`12` 保留产品和平台化演进规划；`11` 已更新为当前单机运维方案。当前可运行代码及完成状态以 `13` 到 `19` 和根目录 `README.md` 为准。

## 核心结论

AnyDatas 当前优先把“本地 Excel 难以承载的数据分析”做成短闭环:

- 数据接入: 上传 Excel/CSV，在确认导入前选择 Sheet、检查样本并调整字段类型。
- 在线分析: 跨文件/跨 Sheet DuckDB SQL、AI SQL、计算字段、单表缓存和多指标结果图表。
- 后台执行: 复杂查询的持久队列、取消、重试、历史和计划运行。
- 后续交付: 在分析主路径稳定后增加持久报表、导出和分享。

当前推荐并已实施的技术路线是 Rust + Axum + SQLite + DuckDB + Vue 3，单容器、单持久卷部署。MVP 不引入 Kubernetes、Redis、Temporal 或用户代码容器；只有规模、安全隔离或多节点需求出现后才评估这些组件。
