# AnyDatas

AnyDatas is being rebuilt as a desktop-first, single-server data analysis workbench. The primary workflow is now:

1. Upload an Excel or CSV file, inspect its worksheets, and confirm column types before import.
2. Select one or more worksheets/ranges across uploaded files.
3. Preview the parsed fields and rows.
4. Query the data with DuckDB SQL or add a calculated field expression.
5. Save reusable SQL, build multi-series charts, or export results as CSV.
6. Move expensive queries to the background and optionally schedule them.

The previous Python/FastAPI implementation remains in `app/`, `templates/`, and `static/` as migration reference. The active rewrite lives in `backend/` and `frontend/`.

## Architecture

- Backend: Rust, Axum, SQLx, SQLite, Calamine, DuckDB
- Frontend: Vue 3, TypeScript, Vite, Pinia, Element Plus, Monaco Editor, ECharts
- Deployment: one container and one persistent volume
- Background execution: an in-process worker backed by a durable SQLite queue
- Scheduling: cron expressions evaluated by the Rust service

No Redis, Kubernetes, external worker service, or Docker socket is required for this MVP.

## Current Rewrite Scope

Implemented:

- Excel (`.xlsx`, `.xls`, `.xlsb`, `.ods`) and CSV upload
- Staged import inspection with Sheet selection, source-row samples, and editable text/integer/decimal/boolean/date/datetime types
- Multiple worksheet discovery and one logical table per Sheet
- Independent start/end-cell and first-row-as-header settings per logical table
- Field deduplication and type inference
- No source-row hard cap; CSV rows stream into immutable per-table DuckDB caches
- Cross-file, cross-Sheet, and self-join DuckDB SQL queries with editable aliases
- Cached table reuse so repeated queries do not reparse Excel/CSV
- Independent logical-table preview and field inspection
- SQL-based calculated fields
- Reusable saved queries that persist SQL and ordered multi-table bindings
- Result tables, grouped/stacked bar, line, area, pie, scatter and radar charts with up to four measures, and formula-safe CSV export
- Workspace OpenAI Chat Completions-compatible settings and a server-persisted Agent Runtime with native tools, resumable conversations, run steps, cancel, retry, and rolling summaries
- Background jobs with multi-table snapshots, progress, logs, result preview, stop, retry, and deletion
- Multi-table cron schedules with timezone, enable/disable, edit, run-now, and deletion
- Password setup/login, HttpOnly sessions, logout, login throttling, and workspace RBAC enforcement
- Vue history routing served by the Rust process
- Chinese desktop interface

Not yet migrated from the legacy implementation:

- Member administration and workspace switching
- Persisted dashboards/reports, XLSX/PDF export, and report sharing
- External databases, S3/MinIO, Python runtimes, notifications, and audit history
- Legacy SQLite data migration

The service defaults to `127.0.0.1`. For HTTPS deployments, set `ANYDATAS_COOKIE_SECURE=1`; use a trusted LAN or private Tailscale bind until a reverse proxy and TLS are configured.

## Local Development

Backend:

```bash
cargo run --manifest-path backend/Cargo.toml
```

Frontend in a second terminal:

```bash
cd frontend
pnpm install
pnpm dev
```

Open `http://127.0.0.1:5173`.

To test the production bundle through Rust:

```bash
cd frontend && pnpm build
cd ..
ANYDATAS_WEB_DIR=frontend/dist cargo run --manifest-path backend/Cargo.toml
```

Open `http://127.0.0.1:8080`.

On a fresh database, the first page creates the owner account and default workspace. Passwords must contain at least 12 characters.

## Docker Compose

```bash
cp .env.example .env
docker compose up --build -d
curl --fail http://127.0.0.1:28080/api/health
```

The default bind is `127.0.0.1:28080`. Set `ANYDATAS_HOST_BIND` in `.env` to the server's LAN or Tailscale IP when remote access is required.

Uploaded and staged files, per-table DuckDB caches, SQLite metadata, users, sessions, saved queries, task results, schedules, and the local AI encryption key are stored in the `anydatas-data` volume. Back up the complete volume; restoring the database without `/data/.secret-key` makes saved AI credentials unreadable.

The bundled DuckDB engine is compiled from source. On ARM64 Docker Desktop, the first release build can take roughly 20-30 minutes. The Dockerfile limits Cargo to one build job to keep peak memory predictable, and BuildKit caches the compiled dependency layers for later rebuilds.

## Verification

```bash
cargo test --manifest-path backend/Cargo.toml
pnpm --dir frontend build
docker compose config --quiet
curl --fail http://127.0.0.1:28080/api/health
```

See `docs/14-rust-vue-rewrite.md` for the active implementation status, `docs/15-cross-file-sheet-analysis.md` for the multi-table model, `docs/16-import-charts-ai.md` for staged imports and charts, and `docs/17-ai-agent-runtime.md` for the persistent Agent architecture.
