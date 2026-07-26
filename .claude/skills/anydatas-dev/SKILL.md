---
name: anydatas-dev
description: Build, run, and verify the AnyDatas app (Rust/Axum backend + Vue 3 frontend, single-process + SQLite + in-process DuckDB). Use when asked to run/start/build the app, reproduce a bug in the real app, run the test/lint gates, or serve the production bundle locally or via Docker.
---

# AnyDatas — 构建 / 运行 / 验证

单进程 Rust 后端同时提供 API、静态前端、后台 worker 和调度；前端是独立的 Vue 3 SPA。DuckDB 是 `bundled` 源码编译，**首次 release 构建很慢**（ARM64 Docker 约 20–30 分钟）。

## 本地开发（两个终端）

后端（默认 `127.0.0.1:8080`，数据目录 `var-rust/`）：
```bash
cargo run --manifest-path backend/Cargo.toml
```

前端（默认 `127.0.0.1:5173`，dev 代理 `/api` → `:8080`）：
```bash
cd frontend && pnpm install && pnpm dev
```
首次打开会走「首次初始化」创建 owner 账号与默认工作区；密码 **≥ 12 字符**。

## 用 Rust 进程验证生产包
```bash
cd frontend && pnpm build && cd ..
ANYDATAS_WEB_DIR=frontend/dist cargo run --manifest-path backend/Cargo.toml
# 打开 http://127.0.0.1:8080
```

## Docker（单容器 + 单卷，默认绑 127.0.0.1:28080）
```bash
cp .env.example .env
docker compose up --build -d
curl --fail http://127.0.0.1:28080/api/readyz
```
远程访问时在 `.env` 设 `ANYDATAS_HOST_BIND` 为 LAN/Tailscale IP。数据都在 `anydatas-data` 卷（含 `.secret-key`——恢复缺它则已存 AI 凭据不可解）。

## 验证闸门（提交前跑这些 = CI 会跑的）
```bash
cargo fmt --all --check --manifest-path backend/Cargo.toml
cargo clippy --all-targets --locked --manifest-path backend/Cargo.toml -- -D warnings
cargo test --locked --manifest-path backend/Cargo.toml
pnpm --dir frontend build     # 含 vue-tsc 类型检查 + check-bundle.mjs 预算
python3 -m unittest discover -s ops_tests -v   # 备份/恢复
docker compose config --quiet
```

## 常用运行时开关（`backend/src/config.rs` 有区间校验，越界即拒启动）
- `ANYDATAS_BIND`（默认 `127.0.0.1:8080`）、`ANYDATAS_DATA_DIR`、`ANYDATAS_WEB_DIR`
- `ANYDATAS_COOKIE_SECURE`（HTTPS/反代部署应设 `1`）
- `ANYDATAS_QUERY_MAX_CONCURRENCY`(默认2) / `ANYDATAS_FILE_PARSE_MAX_CONCURRENCY`(默认1)
- `ANYDATAS_DUCKDB_MEMORY_LIMIT_MB`(1024) / `_THREADS` / `_TEMP_LIMIT_MB`
- `ANYDATAS_QUERY_TIMEOUT_SECONDS`(120) / `ANYDATAS_BACKGROUND_QUERY_TIMEOUT_SECONDS`(3600)
- `ANYDATAS_AGENT_MAX_STEPS`(6) / `_TIMEOUT_SECONDS`(300) / `_CONTEXT_CHARS`(80000)
- `ANYDATAS_AI_ALLOW_PRIVATE_NETWORK`（默认 false；仅部署者可放开内网模型端点）

## 健康探针
- `GET /api/livez` 只证明事件循环活着
- `GET /api/readyz` 校验 SQLite 可读 + 数据卷可读写 + 剩余空间
- `GET /api/metrics` 需 Bearer（`ANYDATAS_METRICS_TOKEN[_FILE]`）

## 注意
- ⚠️ **不要**用 `pip install -r requirements.txt` / `pytest` / `uvicorn app.main:app`——那是仓库根的**遗留 Python 应用**，已不是当前产品（生产只构建 Rust+Vue）。当前测试是 `cargo test` + `ops_tests/`。
- `scripts/upgrade.py` 默认 `--health-url` 指向错误端口（`:8000/readyz`），会误报升级失败；正确值是 `http://127.0.0.1:28080/api/readyz`（见 `docs/review/CODE-REVIEW.md` H6）。
- 首次 Docker 构建慢是正常的（DuckDB 源码编译 + `CARGO_BUILD_JOBS=1`）；BuildKit 缓存后业务代码重建约 22s。
