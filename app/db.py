from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .schema_tools import build_column_metadata


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("ANYDATAS_DATA_DIR", str(ROOT / "var"))).expanduser().resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
RUN_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "anydatas.sqlite3"
DEFAULT_USER_ID = "demo-user"
DEFAULT_WORKSPACE_ID = "demo-workspace"
DEFAULT_REPORT_WIDGETS = (
    ("metric", "Rows", {"aggregate": "row_count", "width": "quarter"}),
    ("metric", "Columns", {"aggregate": "column_count", "width": "quarter"}),
    ("bar", "Comparison", {"width": "half"}),
    ("table", "Result Table", {"limit": 100, "width": "full"}),
)


def ensure_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate_schedule_concurrency_policy() -> None:
    """Extend legacy schedule constraints without breaking runs.schedule_id references."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        schedule_definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'schedules'"
        ).fetchone()
        definition = (schedule_definition[0] or "").lower() if schedule_definition is not None else ""
        if schedule_definition is None or ("cancel_previous" in definition and "queue_all" in definition):
            return

        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
        values = {
            "id": "id",
            "project_id": "project_id",
            "name": "name",
            "schedule_type": "'interval'",
            "interval_minutes": "60",
            "cron_expression": "''",
            "timezone": "'UTC'",
            "max_retries": "0",
            "retry_delay_minutes": "5",
            "concurrency_policy": "'skip'",
            "is_active": "1",
            "last_run_at": "NULL",
            "next_run_at": "CURRENT_TIMESTAMP",
            "created_at": "CURRENT_TIMESTAMP",
        }
        source_values = [column if column in legacy_columns else fallback for column, fallback in values.items()]
        legacy_table = f"_schedules_pre_queue_all_{uuid.uuid4().hex}"
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("BEGIN")
        try:
            conn.execute(f'ALTER TABLE schedules RENAME TO "{legacy_table}"')
            conn.execute(
                """
                CREATE TABLE schedules (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    schedule_type TEXT NOT NULL DEFAULT 'interval',
                    interval_minutes INTEGER NOT NULL,
                    cron_expression TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
                    concurrency_policy TEXT NOT NULL DEFAULT 'skip' CHECK(concurrency_policy IN ('skip', 'queue_one', 'queue_all', 'cancel_previous')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    next_run_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = ", ".join(values)
            conn.execute(
                f"INSERT INTO schedules ({columns}) SELECT {', '.join(source_values)} FROM \"{legacy_table}\""
            )
            conn.execute(f'DROP TABLE "{legacy_table}"')
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def migrate_report_widget_kinds() -> None:
    """Extend legacy report widget constraints before newer component kinds are inserted."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        widget_definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'report_widgets'"
        ).fetchone()
        if widget_definition is None or "'scatter'" in (widget_definition[0] or "").lower():
            return

        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(report_widgets)").fetchall()}
        values = {
            "id": "id",
            "report_id": "report_id",
            "workspace_id": f"'{DEFAULT_WORKSPACE_ID}'",
            "created_by_user_id": "NULL",
            "kind": "'table'",
            "title": "''",
            "config_json": "'{}'",
            "position": "0",
            "created_at": "CURRENT_TIMESTAMP",
        }
        source_values = [column if column in legacy_columns else fallback for column, fallback in values.items()]
        legacy_table = f"_report_widgets_pre_scatter_{uuid.uuid4().hex}"
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("BEGIN")
        try:
            conn.execute(f'ALTER TABLE report_widgets RENAME TO "{legacy_table}"')
            conn.execute(
                """
                CREATE TABLE report_widgets (
                    id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                    created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('metric', 'table', 'bar', 'line', 'scatter', 'pie', 'markdown')),
                    title TEXT NOT NULL DEFAULT '',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            columns = ", ".join(values)
            conn.execute(
                f"INSERT INTO report_widgets ({columns}) SELECT {', '.join(source_values)} FROM \"{legacy_table}\""
            )
            conn.execute(f'DROP TABLE "{legacy_table}"')
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def migrate_notification_channel_types() -> None:
    """Extend persisted channel types while preserving delivery foreign keys."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        table_definition = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notification_channels'"
        ).fetchone()
        definition = (table_definition[0] or "").lower() if table_definition is not None else ""
        if table_definition is None or ("'slack'" in definition and "'teams'" in definition):
            return
        legacy_table = f"_notification_channels_pre_chat_{uuid.uuid4().hex}"
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("BEGIN")
        try:
            conn.execute(f'ALTER TABLE notification_channels RENAME TO "{legacy_table}"')
            conn.execute(
                """
                CREATE TABLE notification_channels (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    channel_type TEXT NOT NULL CHECK(channel_type IN ('email', 'webhook', 'slack', 'teams')),
                    destination TEXT NOT NULL DEFAULT '',
                    secret_id TEXT REFERENCES secret_references(id) ON DELETE RESTRICT,
                    event_types_json TEXT NOT NULL DEFAULT '[]',
                    max_retries INTEGER NOT NULL DEFAULT 3 CHECK(max_retries BETWEEN 0 AND 10),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, name)
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO notification_channels (
                    id, workspace_id, name, channel_type, destination, secret_id,
                    event_types_json, max_retries, is_active, created_by_user_id,
                    created_at, updated_at
                )
                SELECT
                    id, workspace_id, name, channel_type, destination, secret_id,
                    event_types_json, max_retries, is_active, created_by_user_id,
                    created_at, updated_at
                FROM "{legacy_table}"
                """
            )
            conn.execute(f'DROP TABLE "{legacy_table}"')
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notification_channels_workspace ON notification_channels (workspace_id, is_active, created_at)"
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def init_db() -> None:
    ensure_dirs()
    migrate_schedule_concurrency_policy()
    migrate_report_widget_kinds()
    migrate_notification_channel_types()
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                password_hash TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry
                ON auth_sessions (expires_at);

            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                key_hash TEXT PRIMARY KEY,
                failed_count INTEGER NOT NULL,
                first_failed_at TEXT NOT NULL,
                locked_until TEXT
            );

            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                revoked_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user
                ON password_reset_tokens (user_id, workspace_id, used_at, revoked_at, expires_at);

            CREATE TABLE IF NOT EXISTS workspace_invitations (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'analyst', 'viewer')),
                invited_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                revoked_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_workspace_invitations_pending
                ON workspace_invitations (workspace_id, accepted_at, revoked_at, expires_at);

            CREATE TABLE IF NOT EXISTS api_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'read' CHECK(scope IN ('read', 'full')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_api_tokens_user_workspace
                ON api_tokens (user_id, workspace_id, revoked_at, expires_at);

            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'analyst', 'viewer')),
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, workspace_id)
            );

            CREATE TABLE IF NOT EXISTS service_accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('analyst', 'viewer')),
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_service_accounts_workspace
                ON service_accounts (workspace_id, revoked_at, created_at);

            CREATE TABLE IF NOT EXISTS workspace_quotas (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                max_data_sources INTEGER NOT NULL DEFAULT 100 CHECK(max_data_sources >= 0),
                max_projects INTEGER NOT NULL DEFAULT 100 CHECK(max_projects >= 0),
                max_schedules INTEGER NOT NULL DEFAULT 100 CHECK(max_schedules >= 0),
                max_reports INTEGER NOT NULL DEFAULT 100 CHECK(max_reports >= 0),
                max_concurrent_runs INTEGER NOT NULL DEFAULT 2 CHECK(max_concurrent_runs >= 0),
                max_storage_bytes INTEGER NOT NULL DEFAULT 10737418240 CHECK(max_storage_bytes >= 0),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS secret_references (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                environment_variable TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, name),
                UNIQUE(workspace_id, environment_variable)
            );

            CREATE INDEX IF NOT EXISTS idx_secret_references_workspace
                ON secret_references (workspace_id, name);

            CREATE TABLE IF NOT EXISTS data_sources (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                visibility TEXT NOT NULL DEFAULT 'workspace' CHECK(visibility IN ('workspace', 'private')),
                classification TEXT NOT NULL DEFAULT 'internal' CHECK(classification IN ('public', 'internal', 'confidential', 'restricted')),
                source_type TEXT NOT NULL DEFAULT 'file',
                name TEXT NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                column_metadata_json TEXT NOT NULL DEFAULT '{}',
                preview_json TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                quality_json TEXT NOT NULL DEFAULT '{}',
                connection_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS data_source_access_grants (
                data_source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                permission TEXT NOT NULL CHECK(permission IN ('view', 'query', 'manage')),
                granted_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (data_source_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_data_source_access_grants_member
                ON data_source_access_grants (workspace_id, user_id, data_source_id);

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                name TEXT NOT NULL,
                language TEXT NOT NULL CHECK(language IN ('sql', 'python')),
                script TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                runtime_profile TEXT NOT NULL DEFAULT 'standard',
                data_source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
                published_version_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_versions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                language TEXT NOT NULL CHECK(language IN ('sql', 'python')),
                script TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                runtime_profile TEXT NOT NULL DEFAULT 'standard',
                secret_bindings_json TEXT NOT NULL DEFAULT '[]',
                data_source_id TEXT NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS project_secret_bindings (
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                secret_id TEXT NOT NULL REFERENCES secret_references(id) ON DELETE RESTRICT,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                environment_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, secret_id),
                UNIQUE(project_id, environment_name)
            );

            CREATE INDEX IF NOT EXISTS idx_project_secret_bindings_workspace
                ON project_secret_bindings (workspace_id, project_id);

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                project_version_id TEXT REFERENCES project_versions(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL,
                scheduled_for_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                retry_of_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
                next_attempt_at TEXT,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                secret_bindings_json TEXT NOT NULL DEFAULT '[]',
                logs TEXT NOT NULL DEFAULT '',
                result_json TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'interval',
                interval_minutes INTEGER NOT NULL,
                cron_expression TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'UTC',
                max_retries INTEGER NOT NULL DEFAULT 0,
                retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
                concurrency_policy TEXT NOT NULL DEFAULT 'skip' CHECK(concurrency_policy IN ('skip', 'queue_one', 'queue_all', 'cancel_previous')),
                is_active INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                next_run_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'workspace' CHECK(visibility IN ('workspace', 'private')),
                widgets_initialized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS report_widgets (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                kind TEXT NOT NULL CHECK(kind IN ('metric', 'table', 'bar', 'line', 'scatter', 'pie', 'markdown')),
                title TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_report_widgets_report_position
                ON report_widgets (report_id, position, created_at);

            CREATE TABLE IF NOT EXISTS report_filters (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                filter_type TEXT NOT NULL CHECK(filter_type IN ('select', 'contains', 'range')),
                default_value TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_report_filters_report_position
                ON report_filters (report_id, position, created_at);

            CREATE TABLE IF NOT EXISTS report_access_grants (
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                granted_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (report_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_report_access_grants_member
                ON report_access_grants (workspace_id, user_id, report_id);

            CREATE TABLE IF NOT EXISTS report_subscriptions (
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (report_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_report_subscriptions_member
                ON report_subscriptions (workspace_id, user_id, report_id);

            CREATE TABLE IF NOT EXISTS report_subscription_channels (
                report_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                channel_id TEXT NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (report_id, user_id, channel_id),
                FOREIGN KEY (report_id, user_id)
                    REFERENCES report_subscriptions(report_id, user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_report_subscription_channels_member
                ON report_subscription_channels (workspace_id, user_id, report_id);

            CREATE TABLE IF NOT EXISTS report_snapshots (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                recipient_user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL DEFAULT 'general',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                resource_type TEXT NOT NULL DEFAULT '',
                resource_id TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_channels (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL CHECK(channel_type IN ('email', 'webhook', 'slack', 'teams')),
                destination TEXT NOT NULL DEFAULT '',
                secret_id TEXT REFERENCES secret_references(id) ON DELETE RESTRICT,
                event_types_json TEXT NOT NULL DEFAULT '[]',
                max_retries INTEGER NOT NULL DEFAULT 3 CHECK(max_retries BETWEEN 0 AND 10),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, name)
            );

            CREATE INDEX IF NOT EXISTS idx_notification_channels_workspace
                ON notification_channels (workspace_id, is_active, created_at);

            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                channel_id TEXT REFERENCES notification_channels(id) ON DELETE SET NULL,
                notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
                channel_name TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                destination TEXT NOT NULL DEFAULT '',
                secret_id TEXT,
                dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'sending', 'sent', 'failed', 'canceled')),
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                next_attempt_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                finished_at TEXT,
                UNIQUE(channel_id, dedupe_key)
            );

            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_due
                ON notification_deliveries (status, next_attempt_at, created_at);

            CREATE INDEX IF NOT EXISTS idx_notification_deliveries_workspace
                ON notification_deliveries (workspace_id, created_at);
            """
        )
        seed_default_identity(conn)
        ensure_column(conn, "data_sources", "workspace_id", "TEXT NOT NULL DEFAULT 'demo-workspace'")
        ensure_column(conn, "data_sources", "created_by_user_id", "TEXT REFERENCES users(id) ON DELETE SET NULL")
        ensure_column(conn, "data_sources", "visibility", "TEXT NOT NULL DEFAULT 'workspace'")
        ensure_column(conn, "data_sources", "classification", "TEXT NOT NULL DEFAULT 'internal'")
        ensure_column(conn, "data_sources", "source_type", "TEXT NOT NULL DEFAULT 'file'")
        ensure_column(conn, "data_sources", "quality_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "data_sources", "connection_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "data_sources", "column_metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "projects", "workspace_id", "TEXT NOT NULL DEFAULT 'demo-workspace'")
        ensure_column(conn, "projects", "published_version_id", "TEXT")
        ensure_column(conn, "projects", "parameters_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "projects", "runtime_profile", "TEXT NOT NULL DEFAULT 'standard'")
        ensure_column(conn, "project_versions", "parameters_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "project_versions", "runtime_profile", "TEXT NOT NULL DEFAULT 'standard'")
        ensure_column(conn, "project_versions", "secret_bindings_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(conn, "runs", "project_version_id", "TEXT REFERENCES project_versions(id) ON DELETE SET NULL")
        ensure_column(conn, "runs", "schedule_id", "TEXT REFERENCES schedules(id) ON DELETE SET NULL")
        ensure_column(conn, "runs", "scheduled_for_at", "TEXT")
        ensure_column(conn, "users", "password_hash", "TEXT")
        ensure_column(conn, "runs", "attempt", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "runs", "retry_of_run_id", "TEXT REFERENCES runs(id) ON DELETE SET NULL")
        ensure_column(conn, "runs", "next_attempt_at", "TEXT")
        ensure_column(conn, "runs", "parameters_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "runs", "secret_bindings_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(conn, "workspace_quotas", "max_concurrent_runs", "INTEGER NOT NULL DEFAULT 2")
        ensure_column(conn, "workspace_quotas", "max_storage_bytes", "INTEGER NOT NULL DEFAULT 10737418240")
        ensure_column(conn, "schedules", "schedule_type", "TEXT NOT NULL DEFAULT 'interval'")
        ensure_column(conn, "schedules", "cron_expression", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "schedules", "timezone", "TEXT NOT NULL DEFAULT 'UTC'")
        ensure_column(conn, "schedules", "max_retries", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "schedules", "retry_delay_minutes", "INTEGER NOT NULL DEFAULT 5")
        ensure_column(conn, "schedules", "concurrency_policy", "TEXT NOT NULL DEFAULT 'skip'")
        ensure_column(conn, "reports", "workspace_id", "TEXT NOT NULL DEFAULT 'demo-workspace'")
        ensure_column(conn, "reports", "created_by_user_id", "TEXT REFERENCES users(id) ON DELETE SET NULL")
        ensure_column(conn, "reports", "visibility", "TEXT NOT NULL DEFAULT 'workspace'")
        ensure_column(conn, "reports", "widgets_initialized", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "report_snapshots", "workspace_id", "TEXT NOT NULL DEFAULT 'demo-workspace'")
        ensure_column(conn, "audit_events", "workspace_id", "TEXT NOT NULL DEFAULT 'demo-workspace'")
        ensure_column(conn, "notifications", "workspace_id", "TEXT NOT NULL DEFAULT 'demo-workspace'")
        ensure_column(conn, "notifications", "event_type", "TEXT NOT NULL DEFAULT 'general'")
        ensure_column(conn, "notifications", "recipient_user_id", "TEXT")
        ensure_column(conn, "api_tokens", "scope", "TEXT NOT NULL DEFAULT 'full' CHECK(scope IN ('read', 'full'))")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_workspace_recipient ON notifications (workspace_id, recipient_user_id, is_read, created_at)"
        )
        backfill_default_workspace(conn)
        backfill_data_source_access_metadata(conn)
        backfill_data_source_column_metadata(conn)
        backfill_report_creator_subscriptions(conn)
        backfill_published_versions(conn)
        backfill_default_report_widgets(conn)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def seed_default_identity(conn: sqlite3.Connection) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, name, created_at)
        VALUES (?, 'demo@anydatas.local', 'Demo Analyst', ?)
        """,
        (DEFAULT_USER_ID, timestamp),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO workspaces (id, name, created_at)
        VALUES (?, 'Demo Workspace', ?)
        """,
        (DEFAULT_WORKSPACE_ID, timestamp),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO memberships (user_id, workspace_id, role, created_at)
        VALUES (?, ?, 'owner', ?)
        """,
        (DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID, timestamp),
    )


def backfill_default_workspace(conn: sqlite3.Connection) -> None:
    for table in ("data_sources", "projects", "reports", "report_snapshots", "audit_events"):
        conn.execute(f"UPDATE {table} SET workspace_id = ? WHERE workspace_id IS NULL OR workspace_id = ''", (DEFAULT_WORKSPACE_ID,))


def backfill_data_source_access_metadata(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE data_sources
        SET visibility = 'workspace'
        WHERE visibility IS NULL OR visibility = '' OR visibility NOT IN ('workspace', 'private')
        """
    )
    conn.execute(
        """
        UPDATE data_sources
        SET classification = 'internal'
        WHERE classification IS NULL
           OR classification = ''
           OR classification NOT IN ('public', 'internal', 'confidential', 'restricted')
        """
    )


def backfill_published_versions(conn: sqlite3.Connection) -> None:
    projects = conn.execute("SELECT id FROM projects WHERE published_version_id IS NULL OR published_version_id = ''").fetchall()
    for project in projects:
        version = conn.execute(
            """
            SELECT id
            FROM project_versions
            WHERE project_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (project["id"],),
        ).fetchone()
        if version is not None:
            conn.execute("UPDATE projects SET published_version_id = ? WHERE id = ?", (version["id"], project["id"]))


def backfill_data_source_column_metadata(conn: sqlite3.Connection) -> None:
    sources = conn.execute(
        """
        SELECT id, columns_json, preview_json, column_metadata_json
        FROM data_sources
        WHERE column_metadata_json IS NULL OR column_metadata_json = '' OR column_metadata_json = '{}'
        """
    ).fetchall()
    for source in sources:
        columns = decode_json(source["columns_json"], [])
        preview = decode_json(source["preview_json"], [])
        if not isinstance(columns, list):
            columns = []
        if not isinstance(preview, list):
            preview = []
        metadata = build_column_metadata([str(column) for column in columns], preview)
        conn.execute(
            "UPDATE data_sources SET column_metadata_json = ? WHERE id = ?",
            (encode_json(metadata), source["id"]),
        )


def backfill_report_creator_subscriptions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO report_subscriptions (report_id, user_id, workspace_id, created_at)
        SELECT r.id, r.created_by_user_id, r.workspace_id, r.created_at
        FROM reports r
        JOIN memberships m
          ON m.user_id = r.created_by_user_id
          AND m.workspace_id = r.workspace_id
        WHERE r.created_by_user_id IS NOT NULL
        """
    )


def create_default_report_widgets(
    conn: sqlite3.Connection,
    report_id: str,
    workspace_id: str,
    created_by_user_id: Optional[str],
    created_at: Optional[str] = None,
) -> None:
    existing = conn.execute("SELECT 1 FROM report_widgets WHERE report_id = ? LIMIT 1", (report_id,)).fetchone()
    if existing is not None:
        conn.execute("UPDATE reports SET widgets_initialized = 1 WHERE id = ?", (report_id,))
        return
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    for position, (kind, title, config) in enumerate(DEFAULT_REPORT_WIDGETS):
        conn.execute(
            """
            INSERT INTO report_widgets (id, report_id, workspace_id, created_by_user_id, kind, title, config_json, position, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                report_id,
                workspace_id,
                created_by_user_id,
                kind,
                title,
                encode_json(config),
                position,
                timestamp,
            ),
        )
    conn.execute("UPDATE reports SET widgets_initialized = 1 WHERE id = ?", (report_id,))


def backfill_default_report_widgets(conn: sqlite3.Connection) -> None:
    reports = conn.execute(
        "SELECT id, workspace_id, created_by_user_id, created_at FROM reports WHERE widgets_initialized = 0"
    ).fetchall()
    for report in reports:
        create_default_report_widgets(
            conn,
            report["id"],
            report["workspace_id"],
            report["created_by_user_id"],
            report["created_at"],
        )


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def decode_json(value: Optional[str], fallback: Any = None) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def record_audit(
    conn: sqlite3.Connection,
    action: str,
    resource_type: str,
    resource_id: str,
    detail: Optional[dict[str, Any]] = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (id, workspace_id, action, resource_type, resource_id, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            workspace_id,
            action,
            resource_type,
            resource_id,
            encode_json(detail or {}),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def record_notification(
    conn: sqlite3.Connection,
    workspace_id: str,
    event_type: str,
    title: str,
    message: str,
    severity: str = "info",
    resource_type: str = "",
    resource_id: str = "",
    recipient_user_id: Optional[str] = None,
    delivery_key: Optional[str] = None,
) -> str:
    notification_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO notifications (
            id, workspace_id, recipient_user_id, event_type, title, message,
            severity, resource_type, resource_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            notification_id,
            workspace_id,
            recipient_user_id,
            event_type,
            title[:160],
            message[:1000],
            severity,
            resource_type,
            resource_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    # Import lazily so database initialization stays independent from delivery transports.
    from .notification_delivery import enqueue_notification_deliveries

    enqueue_notification_deliveries(conn, notification_id, delivery_key)
    return notification_id
