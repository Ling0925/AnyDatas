---
name: anydatas-review
description: Repo-specific review checklist for AnyDatas changes, distilled from a full audit. Use when reviewing a PR/diff or self-checking before committing backend/frontend/ops changes — it lists the known-fragile spots and the invariants most likely to be broken in this codebase.
---

# AnyDatas 专项评审清单

配合通用 `/code-review` 使用。以下是本仓库**最容易被破坏的不变量**与**已知脆弱点**，按改动区域分组。发现的完整背景见 `docs/review/CODE-REVIEW.md`。

## 后端查询 / 数据引擎（改 `query_engine.rs`/`spreadsheet.rs`/`execution.rs` 时）
- [ ] 用户 SQL 是否仍过 `validate_read_only_sql` + `enable_external_access=false`？新拼接是否用了 `quote_identifier`/`quote_string_literal`（而非裸 `format!`）？
- [ ] 并发许可是否**移动进** `spawn_blocking` 闭包（HTTP 超时不得提前释放槽位）？
- [ ] 缓存键：新增进 `source_cache_key` 的字段是否**域分隔**（长度前缀）？变长字段直接拼接=碰撞（M6）。
- [ ] 数值转换：字符串→整数是否先精确 `parse::<i64>()` 再回退？f64 路径会损坏 >2^53 的值（H1）。
- [ ] 大文件解析是否有**独立于 DuckDB** 的内存/单元格上限？calamine 全量物化会 OOM（H2）。
- [ ] 物化（`CREATE TABLE AS`/建缓存）期间是否有增量磁盘/尺寸守卫？事后检查=可写满盘（M9）。

## 后台任务 / 调度（改 `workers.rs`/`jobs.rs`/`schedules.rs` 时）
- [ ] 终态写入是否带状态守卫（`WHERE ... AND status='running'`）？取消是否带 `status IN ('queued','running')`？（M5）
- [ ] 认领后的每个 `?` 错误路径是否都会落 `failed` 终态并清理制品？否则卡 running（M8）。
- [ ] 定时入队是否靠 `rows_affected()==1` 去重？单条坏 cron 是否会中断整批（应逐条容错，M-low）？
- [ ] `logs_json` 读-改-写是否可能与并发写竞争丢条目？（评审已记，L）

## 安全（改 auth / provider / 导出 / cookie 时）
- [ ] 出站 HTTP 目标是否过 `validate_base_url_network`？是否把连接钉到已校验 IP（防 DNS-rebinding，M1）？
- [ ] CSV/导出：**表头列名**是否也做了公式注入消毒（当前只消毒了数据值，M2）？
- [ ] 限流/身份是否从**受信转发头**取客户端 IP（NAT/反代后用 peer IP 会全局 DoS，M3）？
- [ ] 反代/公网部署是否要求 `ANYDATAS_COOKIE_SECURE=1`？
- [ ] 密钥是否只进内存、不回显、不落日志？备份是否意识到含 `.secret-key`（等同数据卷敏感度）？
- [ ] 任何「DB 值→文件路径」是否用 `Uuid::parse_str`/`is_*_key` 校验（防穿越）？

## 前端（改 `frontend/src` 时）
- [ ] 渲染模型/用户内容是否仍走 DOMPurify（`AiMarkdown.vue`）？没有引入新的 `v-html` 未清洗 sink？
- [ ] Agent 表绑定是否仍与工作台绑定**隔离**（默认空、签名不一致不携带 SQL/样本）？
- [ ] 轮询是否有请求代际失效、错误是否被暴露而非静默吞掉（`TasksView.vue`，L）？
- [ ] 会话/token 仍只经 HttpOnly cookie（不进 localStorage）？

## 迁移 / 文档 / 运维
- [ ] 新增能力是否更新了 README「已迁移/未迁移」清单？`docs/02`/`docs/12` 的 P0 与代码是否一致（勿留「文档声称已实现但代码没有」）？
- [ ] 改了端口/路径是否同步 `scripts/upgrade.py` 的 `--health-url`（当前默认是错的，H6）？注意静态 fallback 会让错路径返回 200 假通过。
- [ ] 遗留 Python（`app/`）改动：确认它是否仍应存在——它不被构建/部署/CI 测试（M11）。
- [ ] 迁移是否加了 partial unique index 编码新不变量？时间戳用 RFC3339 TEXT？

## 通用闸门
- [ ] `cargo fmt --check` / `cargo clippy -D warnings` / `cargo test --locked` 通过？
- [ ] 前端 `pnpm build`（含 vue-tsc + bundle 预算）通过？
- [ ] 新增的守卫函数/状态机是否有直接单元测试？关键路径（workers/execution/集成）是否补了测试？
