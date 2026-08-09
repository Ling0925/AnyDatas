# AnyDatas 评审与设计沉淀

- 生成日期: 2026-07-26
- 范围: 全仓库扫描（Rust 后端 / Vue 前端 / 遗留 Python / 运维 / CI / 文档）

本目录是对 AnyDatas 的一次完整评审沉淀，包含评审意见、提炼的设计文档，以及可复用技能。

## 内容

| 文件 | 是什么 |
| --- | --- |
| [CODE-REVIEW.md](CODE-REVIEW.md) | **评审意见**：总体结论 + 43 条已验证发现（6 高 / 16 中 / 18 低 / 3 信息），含证据、影响链路、修复建议与优先级排序 |
| [DESIGN-OVERVIEW.md](DESIGN-OVERVIEW.md) | **提炼的设计文档**：以实际代码为准的架构心智模型（三层数据模型、查询/缓存、后台队列、Agent 运行时、安全模型、部署、设计取舍） |
| [SKILLS.md](SKILLS.md) | **技能**：为本仓落地的 3 个 Claude Code 技能索引 + 11 条可迁移工程范式 |

配套的 Claude Code 技能（`.claude/skills/`，可 `/<name>` 调用或自动触发）：
`anydatas-dev`（构建/运行/验证）· `anydatas-backend-patterns`（后端扩展规范）· `anydatas-review`（专项评审清单）。

## 一分钟结论

代码质量**很高、安全意识强**：鉴权、SSRF、SQL 安全、持久化恢复、CI 闸门都做得扎实，前端 XSS 与大结果内存经复核无漏洞。发现集中在**中低severity 加固项、正确性边界与迁移缺口**，无当前可被利用的严重漏洞。

最该先处理的四类：
1. **静默数据损坏/可 DoS 资源问题** — CSV 大整数经 f64 损坏(H1)、Excel 全量载入 OOM(H2)、缓存键碰撞(M6)、磁盘守卫事后检查(M9)。
2. **后台状态机与可靠性** — 取消竞态(M5)、卡 running(M8)、单 worker 串行(M7)，并补 workers/集成/守卫测试(M13–M15)。
3. **安全加固** — SSRF DNS-rebinding pin IP(M1)、CSV 表头注入(M2)、NAT 后登录 DoS(M3)、备份密钥处理。
4. **仓库卫生与文档对齐** — 隔离遗留 Python(M11)、审计表(M10)、修 `upgrade.py`(H6)、把 `docs/02/12` 未落地的 P0(H3–H5) 显式降级或排期。

## 方法与可复现性

人工精读关键路径（鉴权/SSRF/密钥/查询引擎/worker/schema/前端）+ 多智能体按 8 维并行评审、每条发现独立开文件对抗验证（61 agent，原始 48 条 → 41 CONFIRMED / 5 驳回）。评审驳回率约 10%，说明多处防护为多层冗余。
