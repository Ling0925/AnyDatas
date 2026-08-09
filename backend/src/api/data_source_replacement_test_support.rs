use std::{path::PathBuf, sync::Arc};

use axum::{
    Router,
    body::Body,
    http::{Request, header},
};
use chrono::{Duration, Utc};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

use crate::{
    db,
    models::{AppState, JsRuntimeLimits, QueryRuntimeLimits, RuntimeMetrics, SharedState},
};

pub(super) const SOURCE_ID: &str = "source-replace";
const WORKSPACE_ID: &str = "workspace-replace";
const USER_ID: &str = "user-replace";
pub(super) const SESSION_TOKEN: &str = "replace-test-session";
pub(super) const OTHER_SESSION_TOKEN: &str = "replace-other-session";

#[derive(Debug, PartialEq, Eq, sqlx::FromRow)]
pub(super) struct TableState {
    pub(super) id: String,
    pub(super) name: String,
    pub(super) schema_json: String,
    pub(super) config_version: i64,
    pub(super) cache_key: Option<String>,
    pub(super) cache_status: String,
    pub(super) cache_error: Option<String>,
    pub(super) is_default: bool,
    pub(super) row_count: i64,
}

#[derive(Debug, PartialEq, Eq, sqlx::FromRow)]
pub(super) struct SourceState {
    pub(super) original_filename: String,
    pub(super) stored_path: String,
    media_type: String,
    file_kind: String,
    size_bytes: i64,
    selected_sheet: String,
    start_cell: String,
    first_row_as_header: bool,
    sheet_names_json: String,
    row_count: i64,
    column_count: i64,
    updated_at: String,
}

pub(super) struct ReplacementFixture {
    _directory: tempfile::TempDir,
    state: SharedState,
}

impl ReplacementFixture {
    pub(super) async fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let data_dir = directory.path().to_path_buf();
        for child in ["uploads", "staging", "table-cache"] {
            std::fs::create_dir(data_dir.join(child)).unwrap();
        }
        let pool = db::connect(&format!("sqlite://{}", data_dir.join("test.db").display()))
            .await
            .unwrap();
        let state = Arc::new(AppState {
            pool,
            data_dir,
            max_upload_bytes: 1_048_576,
            session_ttl_days: 7,
            cookie_secure: false,
            metrics_token: None,
            secret_key: [0; 32],
            query_control: Default::default(),
            cache_build_locks: Default::default(),
            query_semaphore: Arc::new(tokio::sync::Semaphore::new(1)),
            file_parse_semaphore: Arc::new(tokio::sync::Semaphore::new(1)),
            query_max_concurrency: 1,
            file_parse_max_concurrency: 1,
            resource_queue_timeout_seconds: 5,
            query_timeout_seconds: 30,
            background_query_timeout_seconds: 60,
            file_parse_timeout_seconds: 60,
            query_runtime: QueryRuntimeLimits {
                memory_limit_mb: 256,
                threads: 1,
                temp_limit_mb: 256,
                min_free_space_bytes: 0,
                max_artifact_bytes: 1_048_576,
            },
            js_runtime: JsRuntimeLimits::test_default(),
            job_result_retention_days: 30,
            metrics: RuntimeMetrics::new(),
            agent_control: Default::default(),
            agent_events: Default::default(),
            agent_max_steps: 4,
            agent_timeout_seconds: 30,
            agent_context_chars: 80_000,
        });
        Self {
            _directory: directory,
            state,
        }
    }

    pub(super) async fn seed_source(&self) {
        let now = Utc::now().to_rfc3339();
        sqlx::query("INSERT INTO users (id, email, name, password_hash, created_at, updated_at) VALUES (?, 'replace@example.com', '替换测试', 'hash', ?, ?)")
            .bind(USER_ID).bind(&now).bind(&now).execute(&self.state.pool).await.unwrap();
        sqlx::query("INSERT INTO workspaces (id, name, created_at, updated_at) VALUES (?, '替换工作区', ?, ?)")
            .bind(WORKSPACE_ID).bind(&now).bind(&now).execute(&self.state.pool).await.unwrap();
        sqlx::query("INSERT INTO workspace_memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'analyst', ?)")
            .bind(USER_ID).bind(WORKSPACE_ID).bind(&now).execute(&self.state.pool).await.unwrap();
        self.insert_session(SESSION_TOKEN, USER_ID, WORKSPACE_ID, &now)
            .await;

        let old_path = self
            .state
            .data_dir
            .join("uploads")
            .join(format!("{SOURCE_ID}.csv"));
        std::fs::write(&old_path, "id,amount\n1,10\n").unwrap();
        sqlx::query("INSERT INTO data_sources (id, name, original_filename, stored_path, media_type, file_kind, size_bytes, selected_sheet, start_cell, first_row_as_header, sheet_names_json, row_count, column_count, workspace_id, created_by_user_id, created_at, updated_at) VALUES (?, '订单', 'old.csv', ?, 'text/csv', 'csv', 18, '数据', 'A1', 1, '[\"数据\"]', 1, 2, ?, ?, ?, ?)")
            .bind(SOURCE_ID).bind(old_path.to_string_lossy().to_string()).bind(WORKSPACE_ID)
            .bind(USER_ID).bind(&now).bind(&now).execute(&self.state.pool).await.unwrap();
        let schema = r#"[{"name":"id","dataType":"文本","nullable":false},{"name":"amount","dataType":"小数","nullable":false}]"#;
        for (id, name, is_default, cache) in [
            ("table-default", "主表", true, "a".repeat(64)),
            ("table-secondary", "辅助表", false, "b".repeat(64)),
        ] {
            sqlx::query("INSERT INTO source_tables (id, source_id, name, sheet_name, start_cell, first_row_as_header, row_count, column_count, schema_json, config_version, cache_key, cache_status, cache_error, is_default, created_at, updated_at) VALUES (?, ?, ?, '数据', 'A1', 1, 1, 2, ?, 7, ?, 'ready', '旧错误', ?, ?, ?)")
                .bind(id).bind(SOURCE_ID).bind(name).bind(schema).bind(&cache).bind(is_default)
                .bind(&now).bind(&now).execute(&self.state.pool).await.unwrap();
            std::fs::write(
                self.state
                    .data_dir
                    .join("table-cache")
                    .join(format!("{cache}.duckdb")),
                b"cache",
            )
            .unwrap();
        }
    }

    pub(super) async fn seed_other_workspace_session(&self) {
        let now = Utc::now().to_rfc3339();
        sqlx::query("INSERT INTO users (id, email, name, password_hash, created_at, updated_at) VALUES ('other-user', 'other@example.com', '其他用户', 'hash', ?, ?)")
            .bind(&now).bind(&now).execute(&self.state.pool).await.unwrap();
        sqlx::query("INSERT INTO workspaces (id, name, created_at, updated_at) VALUES ('other-workspace', '其他工作区', ?, ?)")
            .bind(&now).bind(&now).execute(&self.state.pool).await.unwrap();
        sqlx::query("INSERT INTO workspace_memberships (user_id, workspace_id, role, created_at) VALUES ('other-user', 'other-workspace', 'analyst', ?)")
            .bind(&now).execute(&self.state.pool).await.unwrap();
        self.insert_session(OTHER_SESSION_TOKEN, "other-user", "other-workspace", &now)
            .await;
    }

    pub(super) async fn force_data_source_update_failure(&self) {
        sqlx::query(
            "CREATE TRIGGER fail_source_replace BEFORE UPDATE ON data_sources BEGIN SELECT RAISE(FAIL, 'forced replacement failure'); END",
        )
        .execute(&self.state.pool)
        .await
        .unwrap();
    }

    async fn insert_session(&self, token: &str, user_id: &str, workspace_id: &str, now: &str) {
        let token_hash = hex::encode(Sha256::digest(token.as_bytes()));
        sqlx::query("INSERT INTO auth_sessions (token_hash, user_id, workspace_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)")
            .bind(token_hash).bind(user_id).bind(workspace_id).bind(now)
            .bind((Utc::now() + Duration::hours(1)).to_rfc3339()).execute(&self.state.pool).await.unwrap();
    }

    pub(super) async fn request(
        &self,
        source_id: &str,
        session_token: &str,
        body: Vec<u8>,
    ) -> axum::response::Response {
        let app = Router::new()
            .nest("/api", super::router(self.state.max_upload_bytes))
            .with_state(self.state.clone());
        let request = Request::builder()
            .method("POST")
            .uri(format!("/api/data-sources/{source_id}/replace"))
            .header(
                header::CONTENT_TYPE,
                "multipart/form-data; boundary=replace-boundary",
            )
            .header(header::COOKIE, format!("anydatas_session={session_token}"))
            .body(Body::from(body))
            .unwrap();
        app.oneshot(request).await.unwrap()
    }

    pub(super) async fn table_states(&self) -> Vec<TableState> {
        sqlx::query_as("SELECT id, name, schema_json, config_version, cache_key, cache_status, cache_error, is_default, row_count FROM source_tables WHERE source_id = ? ORDER BY id")
            .bind(SOURCE_ID).fetch_all(&self.state.pool).await.unwrap()
    }

    pub(super) async fn source_state(&self) -> SourceState {
        sqlx::query_as("SELECT original_filename, stored_path, media_type, file_kind, size_bytes, selected_sheet, start_cell, first_row_as_header, sheet_names_json, row_count, column_count, updated_at FROM data_sources WHERE id = ?")
            .bind(SOURCE_ID).fetch_one(&self.state.pool).await.unwrap()
    }

    pub(super) fn cache_path(&self, fill: char) -> PathBuf {
        self.state
            .data_dir
            .join("table-cache")
            .join(format!("{}.duckdb", fill.to_string().repeat(64)))
    }

    pub(super) fn force_cache_cleanup_failure(&self, fill: char) {
        let path = self.cache_path(fill);
        std::fs::remove_file(&path).unwrap();
        std::fs::create_dir(&path).unwrap();
    }

    pub(super) fn temporary_artifacts(&self) -> Vec<PathBuf> {
        let uploads = std::fs::read_dir(self.state.data_dir.join("uploads"))
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.contains(".backup-"))
            });
        let staging = std::fs::read_dir(self.state.data_dir.join("staging"))
            .unwrap()
            .filter_map(Result::ok)
            .map(|entry| entry.path());
        uploads.chain(staging).collect()
    }
}

pub(super) fn multipart_file(filename: &str, content: &[u8]) -> Vec<u8> {
    let mut body = format!("--replace-boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").into_bytes();
    body.extend_from_slice(content);
    body.extend_from_slice(b"\r\n--replace-boundary--\r\n");
    body
}

pub(super) fn multipart_file_with_tables(filename: &str, content: &[u8]) -> Vec<u8> {
    let mut body = format!("--replace-boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").into_bytes();
    body.extend_from_slice(content);
    body.extend_from_slice(b"\r\n--replace-boundary\r\nContent-Disposition: form-data; name=\"tables\"\r\n\r\n[]\r\n--replace-boundary--\r\n");
    body
}
