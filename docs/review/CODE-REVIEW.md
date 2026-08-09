# AnyDatas 代码评审报告

- 评审日期: 2026-07-26
- 评审范围: 全仓库（Rust 后端 `backend/`、Vue 前端 `frontend/`、遗留 Python `app/`、运维脚本 `scripts/`、CI/Docker、文档 `docs/`）
- 代码规模: 后端 Rust ≈ 12.6k 行 / 前端 Vue+TS ≈ 8.9k 行 / 遗留 Python ≈ 12.5k 行 / 文档 ≈ 3.2k 行
- 方法: 人工精读关键路径（鉴权、SSRF、密钥、查询引擎、worker、schema）+ 多智能体按 8 个维度并行评审并对每条发现做对抗式验证（61 个 agent，48 条原始发现，41 条 CONFIRMED，5 条被验证驳回）

---

## 一、总体结论

**这是一个工程质量很高、安全意识很强的代码库。** 活跃的 Rust + Vue 重写在多数维度上明显优于常见同类项目：

- **鉴权扎实**：Argon2 加盐哈希、`spawn_blocking` 避免阻塞事件循环、用 dummy hash 做恒定时间比较防用户枚举、会话 token 仅存 SHA-256 摘要、邮箱+IP 双阈值限流、HttpOnly/SameSite=Lax cookie（`backend/src/api/auth.rs`）。
- **SSRF 纵深防御**：DNS 解析后逐个 IP 校验 v4/v6 全部保留网段（含 `169.254.169.254`、CGNAT `100.64/10`、IPv4-mapped）、禁止重定向、公网强制 HTTPS、拒绝 URL 内嵌凭据（`backend/src/services/agent_provider.rs`）。
- **SQL 安全分层**：`enable_external_access=false` + 禁扩展自动加载 + 只读 ATTACH + 标识符/字面量转义 + 服务端强制的内存/线程/临时盘上限（用户无法用 `SET` 覆盖）（`backend/src/services/query_engine.rs`）。
- **持久化与恢复**：一切事实以 SQLite 为准，进程内事件总线只做唤醒；启动时用 `recover_interrupted_jobs`/`recover_interrupted_agent_runs` 把残留 `running` 状态收敛为 `failed`；缓存与结果制品用「临时文件 → CHECKPOINT → 原子重命名」保证崩溃不留半成品。
- **前端安全**：cookie 鉴权（无 localStorage token，杜绝 XSS 窃取）、AI Markdown 经 marked→DOMPurify 双阶段清洗、CSV 导出防公式注入、别名校验与后端一致。强 CSP（`script-src 'self'`、`object-src 'none'`、`frame-ancestors 'none'`）。
- **CI 是真正的发布闸门**：`cargo fmt --check`、`cargo clippy -D warnings`、`cargo test --locked`、前端构建 + bundle 预算检查、compose 配置校验，容器构建 gated 在前三者之后。

因此本报告的绝大多数发现是 **中低severity 的加固项、正确性边界与迁移缺口**，而不是当前可被利用的严重漏洞。前端 XSS 与大结果内存被专门复核，结论为「无漏洞」。

### 发现分布

| 严重度 | 数量 | 性质概览 |
| --- | --- | --- |
| 高 (High) | 6 | 1 个静默数据损坏、1 个可 DoS 的 OOM、1 个 ops 工具默认必失败、3 个 P0 产品能力缺失（文档声称已实现但代码没有） |
| 中 (Medium) | 16 | SSRF DNS-rebinding、CSV 头注入、登录全局 DoS、若干竞态与资源边界、关键路径零测试 |
| 低 (Low) | 18 | 竞态导致丢日志、cookie 默认非 Secure、构建不可复现、大量可测函数无测试 |
| 信息 (Info) | 3 | a11y、CI 覆盖缺口，以及 1 条「已复核确认无漏洞」的负向结论 |

> 说明：多智能体评审对每条发现做了独立的「开文件复核」，并会自我修正过度声称（例如把「Slack/Teams 通知」从 P0 更正为 P1）。下文按主题归并了少量重复项（如 `logs_json` 竞态、磁盘守卫时机）。

---

## 二、高severity 发现（建议优先处理）

### H1. CSV 大整数经 f64 静默损坏（>2^53）— 正确性 / 数据完整性
`backend/src/services/query_engine.rs:809`（`integer_value`）

- **问题**：CSV 单元格全部以 `Value::String` 进入（`spreadsheet.rs:619`）。类型推断 `infer_fields` 用**精确的** `parse::<i64>()` 把 17–19 位数字列判定为「整数」（`spreadsheet.rs:549`），但缓存导入的转换却先 `parse::<f64>()` 再 `as i64`。f64 尾数只有 53 位，`2^53`（16 位数）以上的整数会被静默舍入。推断与转换对同一列做出了不一致的判断。
- **影响**：本产品目标数据里极常见的 18 位身份证号、16–19 位银行卡号 / 订单号 / 雪花 ID 会被自动判为整数，然后每个 >2^53 的值在建缓存时被悄悄改写。例如 `110101199003074258` → `110101199003074256`。所有基于该键列的聚合、`GROUP BY`、`JOIN`、CSV 导出都会出错（不同 ID 舍入到同值造成错误 join，或舍离造成漏 join），**全程无任何报错**。
- **附带**：边界判断 `number <= i64::MAX as f64` 也是错的——`i64::MAX as f64 == 2^63`，落在 `(i64::MAX, 2^63)` 的值会被接受并饱和成 `i64::MAX`。
- **修复**：字符串分支先尝试精确 `parse::<i64>()`（与推断逻辑对齐），仅在确需时回退 f64；超出 f64 精确范围的整数应保留为文本或拒绝，而非静默 `as i64`。边界改用严格 `< 2^63` 比较。

### H2. Excel 全量载入内存，无视 `memory_limit_mb` → 可 DoS 的 OOM — 数据引擎 / 资源耗尽
`backend/src/services/spreadsheet.rs:466`（`stream_excel_rows` / `read_excel`）

- **问题**：代码宣称「固定内存流式导入」（`spreadsheet.rs:118`、`query_engine.rs:459`），但 Excel 路径调用 calamine 的 `worksheet_range(sheet)`，会在读取首行前把**整张工作表解压并物化**到 Rust 堆上。`duckdb_memory_limit_mb` 只约束 DuckDB 自身算子，管不到 calamine；`max_upload_bytes` 只限压缩包体积，而 xlsx/xlsb 是 zip，可 100:1+ 膨胀。`build_source_cache` 每次建缓存甚至物化两次。
- **影响**：一次工作区分析员上传的中等体积 `.xlsx`（大量单元格或类 zip-bomb 的稀疏表）在预览/导入/首查触发建缓存时即可把共享进程 OOM，**拖垮同实例所有租户**。`file_parse_timeout` 无法中止 calamine（`resource_control.rs:29-30` 已注明），超时只是返回客户端而线程继续分配。
- **修复**：给表格解析设独立于 DuckDB 的内存/单元格数/估算解压尺寸上限，在物化前拒绝超限；或在 inspect 阶段限制表维度；并订正「固定内存」的注释。理想方案是为建缓存路径引入真正的流式 xlsx 读取。

### H3. 无法多人协作：无成员管理，RBAC 角色实际不可达 — 迁移 / 产品能力
`backend/src/api/auth.rs:41`

- **问题**：`docs/02` 把「组织/工作区/成员/角色」列为 P0，定义五种角色。但后端只有 `/auth/{status,setup,login,logout,me}` 五个路由；`setup()` 在已有用户时 `Conflict` 拒绝再次运行，且硬编码首个成员角色为 `owner`。全仓无 invite / member / 角色分配 / 工作区切换端点。migration 0002 的 `CHECK` 允许 owner/admin/analyst/viewer，`require_admin()`/`require_analyst()` 也接入了大量 handler，但**没有任何代码路径会写入非 owner 成员**，这些分支形同死脚手架。
- **影响**：部署出来的实例永远是「单人单工作区」。文档主打的「面向团队的数据分析平台」及旅程 2、3 无法演示；RBAC 脚手架暗示了产品并不具备的能力。
- **修复**：要么补齐邀请 + 角色分配 + 成员/工作区切换以恢复 P0，要么明确将产品定位为「单操作者」并移除不可达的角色脚手架，避免误导。

### H4. 无持久化报表/看板、无订阅、无任何通知投递 — 迁移 / 产品能力
`backend/migrations/`（缺表）

- **问题**：`docs/02` 把「报表 / 报表权限 / 站内通知（运行失败 + 订阅刷新）」列为 P0，遗留 Python 已实现（`reports`、`report_snapshots`、`report_subscriptions`、`notification_deliveries` 等表 + `report_subscriptions.py`、`notification_delivery.py`）。Rust 侧 0001–0008 迁移无任何对应表，无对应路由；图表只存在于前端「即席、不落库」的查询可视化。
- **影响**：旅程 1 第 8 步（自动运行→失败通知→更新报表快照）与旅程 2（订阅周报）无法完成。产品能从实时查询渲染图表，但无法「持久化 / 分享 / 授权 / 快照 / 订阅」报表——即 `docs/12` 所称的核心差异点缺失。
- **范围订正**：多渠道投递（邮件/Webhook/Slack/Teams）在 `docs/02` 中实为 P1；但 **P0 核心（持久化+可授权报表 + 站内失败/订阅通知）确实完全缺失**。
- **修复**：将「持久化报表 + 订阅通知」列为多用户之后的最高优先迁移项；在此之前保持 README「尚未迁移」提示醒目，避免试点误以为报表已具备。

### H5. 未迁移外部数据库 / 对象存储数据源（PG/MySQL/ClickHouse/S3-MinIO）— 迁移 / 产品能力
`backend/src/api/data_sources.rs:971`（`file_metadata` 仅白名单 csv/xlsx/xls/xlsb/ods）

- **问题**：`docs/02` P0 要求「数据库连接：PostgreSQL、MySQL 优先，ClickHouse 可选」，`docs/12` 记为遗留已实现（`postgres_tools.py`/`mysql_tools.py`/`clickhouse_tools.py`/`s3_tools.py`）。Rust 侧数据源层只接受文件上传，全仓无 `postgres|mysql|clickhouse|minio` 命中（唯一的数据库是应用自身元数据用的 SQLite）。
- **影响**：旅程 2（从数据库到团队报表）无法实现——想查 PG/ClickHouse 仓库的分析师必须手动导出再上传。一项 P0 数据源能力回退为「仅文件」。
- **修复**：若外部数据源确不在 MVP 范围，请把 `docs/02`/`docs/12` 从「P0 已实现」下调为「延期」，让计划与代码一致；否则排期连接器移植。注意别让 `Dockerfile.runtime` 里残留的 DB 驱动暗示该能力存在（见 L 类发现）。

### H6. `upgrade.py` 就绪探测指向错误端口与路径，默认必然报「升级失败」— 运维 / 正确性
`scripts/upgrade.py:63` 与 `:118`

- **问题**：`run_upgrade()` 与 CLI 默认 `health_url = "http://127.0.0.1:8000/readyz"`。而服务发布在 `127.0.0.1:28080`（`docker-compose.yml:33`），就绪路由在 `/api` 之下即 `/api/readyz`（`api/mod.rs:42,63`）。全仓无 8000 端口。
- **影响**：操作者跑默认 `python scripts/upgrade.py`：备份、`build`、`up -d` 全部成功，随后 `wait_for_health()` 轮询一个不存在的端点，直到 120s 超时抛 `UpgradeError` 退出码 1「Upgrade failed」——**新容器其实是健康的**，工具却每次都报失败，诱发对正常部署的无谓回滚。
- **修复**：默认改为 `http://127.0.0.1:28080/api/readyz`（或由 `ANYDATAS_PORT` 推导端口 + `/api/readyz` 路径）。**注意**：因存在静态文件 fallback（`api/mod.rs:64`），只改端口不改路径会让 `/readyz` 返回 `index.html`+200，形成假通过。为 `run_upgrade` 加桩测试并把 `upgrade.py` 纳入 CI 的 `py_compile`。

---

## 三、中severity 发现（分组）

### 安全加固

- **M1（已处置）：AI DNS 解析与连接目标不一致**。旧实现校验后由 reqwest 二次解析主机名，可能连接到不同地址。当前实现把请求固定到本次 DNS 解析结果并禁止重定向；产品策略允许工作区管理员配置本机和局域网模型，因此不再设置 AI 专属私网开关。QuickJS `http.request` 继续由独立白名单与私网策略约束。
- **M2. CSV 公式注入：结果列名未转义**（`query_engine.rs:297`）。`write_artifact_csv` 只对数据单元格值做了防注入前缀（`duck_value_to_csv`），但表头 `writer.write_record(&names)` 原样写出；而列名来自上传文件表头，`build_column_names` 不做公式字符消毒。攻击者上传表头为 `=HYPERLINK(...)` / `=cmd|'/C calc'!A1` 的文件，同工作区他人 `SELECT *` 导出 CSV 后在 Excel 打开即触发。**修复**：对表头套用同样的前缀消毒；并把触发集扩展到前导 `TAB(0x09)`、`CR(0x0D)`。
- **M3. 登录锁定基于连接对端 IP，在 NAT/Docker 桥后造成全局登录 DoS**（`auth.rs:251`）。限流用 `ConnectInfo<SocketAddr>` 的 IP，不看转发头。compose 部署下容器经 Docker 桥 NAT，所有外部客户端呈现同一网关源 IP；攻击者用任意邮箱失败 25 次即可触发 `ip:` 桶锁定，随后 15 分钟内**同网关所有用户都无法登录**；且该拓扑下 per-IP 限流对真实客户端形同虚设。（per-email 暴力仍受 email+ip 键约束，故主要是可用性问题。）**修复**：在代理/NAT 后从**受信配置的转发头**取客户端 IP（且只信任已知代理源）；否则收窄全局锁的作用域，用 email 维度做锁定、IP 维度只做软延迟。
- **M-低边界：会话 cookie `Secure` 默认关**（`config.rs:121`，见 L 类）：栈内无 TLS 时默认 `ANYDATAS_COOKIE_SECURE=false`，README 已提示 HTTPS 部署要开启，属可接受的默认但需在反代/公网场景强制开启。

### 正确性与竞态

- **M4. 只读 SQL 黑名单误伤字符串字面量**（`query_engine.rs:650`）。`\b(attach|copy|...|set|create|update|delete|...)\b` 会匹配 SQL 中**任意位置**的关键字，包括引号内的数据值。`SELECT ... WHERE status IN ('create','update','delete')`、`WHERE action='load'`、`FROM calls WHERE type='call'` 等合法查询被拒；Agent 生成的 SQL 也走同一校验，导致「AI 产出正确 SQL 却被系统拒绝」。而实际隔离已由 `enable_external_access=false` 保证，故这些字面量匹配无安全收益、只破坏真实查询。**修复**：先剥离字符串字面量与注释再套黑名单，或改用「语句类型判定 + 引擎级只读」而非子串匹配。
- **M5. Job 取消与 worker 终态写入缺少状态守卫**（`jobs.rs:203`、`workers.rs:158/196`）。取消 `UPDATE status='canceled' WHERE id=?` 无状态守卫；worker 的成功/失败写入也无守卫，仅在写入前做了一次**非原子**的 `SELECT status`。竞态结果：(1) worker 完成后读到 running、用户在此后取消、worker 又覆盖为 succeeded → 取消被静默丢弃；(2) worker 写 succeeded（含 artifact key）后取消 UPDATE 覆盖为 canceled 却不清结果字段 → 下载接口仍对「canceled」任务提供制品。**修复**：取消加 `WHERE id=? AND status IN ('queued','running')`；worker 终态加 `WHERE id=? AND status='running'`。
- **M6. 缓存键无分隔符导致碰撞，遗留查询路径返回错数据**（`query_engine.rs:403`）。`source_cache_key` 把 sheet / start_cell / end_cell 直接拼接进 SHA-256，无长度前缀/分隔符。遗留路径（`request.tables` 为空）下这些字段来自用户覆盖值，变长字段直接拼接可产生同哈希。例：sheet=`"AB"`,start=`"C1"` 与 sheet=`"A"`,start=`"BC1"` 得到同键，第二次查询命中第一次的缓存文件并**静默返回错误区间的数据**。**修复**：对哈希做域分隔（每字段前置定长长度或用结构化编码），并加边界碰撞测试。

### 资源与吞吐

- **M7. 后台 job worker 单例串行执行**（`workers.rs:30`）。`spawn_job_worker` 只起一个任务，循环内 `await` 完整执行后才下一 tick；`MissedTickBehavior::Skip` 使运行期间的 tick 全部丢弃 → job 严格一次一个。一条昂贵的后台/定时查询可占用唯一 worker 长达 `background_query_timeout_seconds`（默认 3600s），期间**整个批处理子系统队头阻塞**。**修复**：认领与执行解耦——认领后 `tokio::spawn` 执行（仍受 `query_semaphore` 限并发），或用小型固定 worker 池。
- **M8. 认领后出错的 job 卡在 `running` 直到重启**（`workers.rs:104`）。原子认领把 job 置 `running` 后，`append_log`/`required_job`/进度 UPDATE/`load_bindings` 等一串 `?` 若任一 Err 会**跳出而不写终态**；job 不再是 `queued` 故不会被再认领，只能等下次 `recover_interrupted_jobs`。SQLite 竞争（busy_timeout 仅 5s、池 8 连接）即可触发。**修复**：把认领后主体包进内层 async，任何 Err 都标记 `failed`（复用现有 Err 分支），并确保各非成功路径都清理制品。
- **M9. 磁盘守卫是事后检查，物化期间不设防**（`query_engine.rs:213`）。后台制品用 `CREATE TABLE result AS SELECT ...` 全量物化，`max_artifact_bytes` 只在写完 CHECKPOINT **之后**校验；`ensure_free_space` 只在前后各一次；建缓存逐行 append 期间也不再查剩余空间。结果表/缓存表写在主 DB 文件，`max_temp_directory_size` 管不到。一条 `SELECT * FROM a CROSS JOIN b` 可在守卫触发前把磁盘写满，令 SQLite 写入/上传/他人查询 ENOSPC。**修复**：物化期间设尺寸上限（按 `max_artifact_bytes` 推导 `LIMIT`，或在 append 循环里周期性查剩余空间并中止）。

### 迁移与仓库卫生

- **M10. 无审计历史**（缺表）。`docs/02` P0 要求「登录、数据源创建、运行、报表发布、权限变更」审计；遗留有 `audit_events` + `record_audit()`。Rust 侧无审计表、无审计写入，仅有低基数 Prometheus 计数。Owner/Admin 无法回答「谁访问了哪个数据源 / 改了什么设置」。**修复**：加 append-only `audit_events` 表并记录关键变更。
- **M11. 遗留 Python 应用仍以「主项目」姿态盘踞仓库根，却从不构建/部署/被 CI 测试**（`requirements.txt:1`）。根目录 `requirements.txt`、`pytest.ini(testpaths=tests)` 都指向遗留 FastAPI（`app/main.py` 6097 行 / 84 路由 / 28 模块，含自成一套的 auth、密钥、外部 DB 代码）。生产 Dockerfile 只构建 Rust+Vue，CI 从不跑 `pytest`。开发者若按常规 Python 信号（`pip install -r requirements.txt`、`pytest`、`uvicorn app.main:app`）操作，会**运行旧产品**并误以为是当前版本；23 个遗留测试给出虚假覆盖信心。**修复**：把 `app/`、`templates/`、`static/`、`tests/`、`pytest.ini`、`requirements.txt` 迁入清晰标注的 `legacy/` 目录（或在不再需要参考时删除），根目录只保留仍被 CI 接入的 `scripts/`、`ops_tests/`、`monitoring/`。
- **M12. 新旧 SQLite schema 基本不相交且无迁移路径**（`app/db.py:1`）。遗留 32 表 vs Rust 19 表，语义不同（legacy `runs`→`jobs`，schedule 绑定对象不同）。若有试点跑过旧版，其元数据（projects/runs/reports/secrets/members/audit）无法被 Rust 载入。共用仓库与产品名暗示了并不存在的数据连续性。**修复**：在 README/升级文档明确「Rust 重写从空库开始、无就地迁移」；若存在真实试点数据，再决定是否值得写一次性导出器。

### 测试缺口（关键路径）

- **M13. `workers.rs` 零测试**（`workers.rs:83`）。后台运行时核心（认领状态机、取消判定、日志追加、失败制品清理、定时去重）无任何 `#[test]`。两处微妙守卫完全未验证：取消判定 `if current_status=="canceled"`（决定刚生成的制品是否丢弃）与定时的 `rows_affected()==1`（防止 10s tick 重复入队）。**修复**：用 `agent.rs` 已有的 `seeded_agent_state` 夹具写 `#[tokio::test]`：插入 queued job → `claim_and_run_job` → 断言转 succeeded 且 artifact key 非空；插入两条到期 schedule → `enqueue_due_schedules` → 断言每条恰一个 job 且 `next_run_at` 前进。
- **M14. 无 upload→query→job→schedule 集成测试**（`data_sources.rs:991`）。产品关键链路跨越导入→逻辑表→交互查询→入队→worker→结果分页/CSV→定时，无端到端测试；最接近的 `agent.rs` 用例全用原始 SQL 播种、绕过上传 API 且不碰 worker/scheduler。任一接缝契约破坏都会绿灯通过 CI。**修复**：加一个 `#[tokio::test]` 走真实上传+导入 handler → `execute_request` 断言行数 → `enqueue_job`+`claim_and_run_job` 断言 succeeded+可读结果 → 建到期 schedule 断言 `enqueue_due_schedules` 产出 job。
- **M15. `job_results::artifact_path` 路径穿越守卫（UUID 解析）无测试**（`job_results.rs:19`）。这是把 DB 中 `result_artifact_key` 变为文件路径的唯一守卫，专门 `Uuid::parse_str` 拦截恶意键，却零测试。**修复**：加 `#[test]` 断言合法 UUID → 正确路径，`../evil`/裸文件名/空串 → `Err`（参照 `maintenance.rs` 已有的 `accepts_only_sha256_cache_keys`）。

---

## 四、低severity 与信息级（择要）

- **竞态丢日志**（`workers.rs:215/219`，correctness+rust-quality 两条合并）：`jobs.logs_json` 读-改-写非原子，取消 handler 与 worker `append_log` 并发会丢条目。改为 SQLite JSON 追加或加行锁/独立 `job_logs` 表。
- **定时批处理队头阻塞**（`workers.rs:255`）：`enqueue_due_schedules` 单条 cron/时区不可解析即 `?` 中断整批。应逐条容错、跳过坏项。
- **制品资源边界/泄漏**（`query_engine.rs:187`、`workers.rs:138`）：物化期间无增量尺寸校验；完成竞态窗口内被取消时成功制品在磁盘上成孤儿。加 `CREATE TABLE` 期间守卫 + 各路径统一清理。
- **cookie Secure 默认关**（`config.rs:121`）：反代/公网部署应强制 `ANYDATAS_COOKIE_SECURE=1`，建议在无 TLS 且 `bind` 非 loopback 时启动告警。
- **前端**：`PixelOcean.vue:197` 登录页每帧对每格做三角函数全量重绘（改为预计算/降采样/`prefers-reduced-motion` 降级）；`TasksView.vue:141` 轮询与手动刷新竞争且吞掉全部错误（加请求代际失效 + 错误暴露）。`DataGrid.vue:99` 表头缺 `scope`（a11y，info）。
- **运维**：`scripts/backup.py:407` 恢复不强制服务已停止（`--force` 仅口头确认）；`backup.py:165` `verify_checksum` 在缺 `.sha256` 时静默通过；`ops_tests` 从不在 WAL 模式下测试（生产实际 WAL）；`Dockerfile:29` 基础镜像按 tag、apt 包未固定 → 构建不可复现。
- **备份即高敏**（`backup.py:137`，归为中/低边界）：归档把 `/data/.secret-key` 与含密文的 `anydatas.db` 打进同一未加密 tar.gz，任何人拿到一个归档即可解出全部 AI provider API Key。至少 `chmod 600`，并在文档标注归档等同数据卷敏感度；可选信封加密或恢复时带外提供密钥。
- **大量可测纯函数无测试**：`schedules::next_run`（cron+时区）、`resource_control` 许可超时、`execution`/`query_bindings`（16 表上限、重复别名、超时/取消）、API handler 状态机与工作区隔离——均只能经 HTTP 触达。遗留 `tests/`（239 用例）针对已死的 `app/`，`pytest.ini` 仍指向它却不在任何 CI 闸门中。
- **信息级正向结论**：AI Markdown（`AiMarkdown.vue:33`）XSS 与大结果内存被专门复核，**未发现漏洞**（marked→DOMPurify 分离 + cookie 鉴权无 token 可窃）。CI 闸门被确认为真正 blocking，但 `upgrade.py` 被排除在所有 CI 检查外（与 H6 相关）。

---

## 五、被验证驳回的发现

对抗验证阶段驳回了 5 条初始发现（占原始 48 条约 10%），例如把「Slack/Teams 通知缺失」由 P0 更正为 P1、以及若干「已被别处守卫覆盖」的误报。这说明代码的多处防护是**多层冗余**的，单点担忧往往已被上游拦截。

---

## 六、建议的处理顺序

1. **先堵静默数据损坏与可 DoS 的资源问题**（对用户信任影响最大、且不涉及产品范围决策）：H1（f64 大整数）、H2（Excel OOM）、M6（缓存键碰撞）、M9（磁盘守卫）、M4（SQL 黑名单误伤）。
2. **再补状态机守卫与后台可靠性**：M5（取消竞态）、M8（卡 running）、M7（单 worker 串行），并配 M13/M14/M15 测试锁住回归。
3. **安全加固**：M1（SSRF pin IP）、M2（CSV 头注入）、M3（登录 DoS 取转发头）、备份密钥处理、反代场景强制 Secure cookie。
4. **仓库卫生 + 文档对齐**：M11（隔离遗留 Python）、M10（审计表）、H6（修 `upgrade.py`）、以及把 `docs/02`/`docs/12` 中未落地的 P0（H3/H4/H5）显式降级或排期，让「计划」与「代码」一致。
5. **产品能力路线**：多用户/成员管理 → 持久化报表+订阅 → 外部数据源连接器，按 `docs/14 §8` 的既有顺序推进。

---

## 附：评审方法与可复现性

- 人工精读文件：`main.rs`、`config.rs`、`api/mod.rs`、`api/auth.rs`、`services/secrets.rs`、`services/agent_provider.rs`、`services/query_engine.rs`（含 `validate_read_only_sql`/`quote_identifier`）、`workers.rs`、`migrations/0001|0002|0006`、`frontend/src/api.ts`、`stores/workspace.ts`、`components/AiMarkdown.vue`、`Dockerfile`、`docs/04|14|17`。
- 自动评审维度：correctness / security / rust-quality / frontend / data-engine / migration / ops / tests，每维一个查找 agent + 每条发现一个「开文件复核」验证 agent。
- 完整发现明细（含每条证据、影响链路、验证推理）见工作流原始输出；本报告为归并、去重、排序后的可执行版本。
