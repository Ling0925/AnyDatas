from __future__ import annotations

import asyncio
import csv
import contextlib
import html
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .auth import (
    SESSION_COOKIE_NAME,
    accept_workspace_invitation,
    add_workspace_member,
    authenticate_password,
    create_api_token,
    create_password_reset_token,
    create_service_account,
    create_session,
    create_workspace_invitation,
    get_active_invitation,
    get_active_password_reset,
    get_or_create_login_identity,
    get_request_context,
    hash_password,
    password_auth_enabled,
    register_password_identity,
    reset_password_with_token,
    require_role,
    revoke_session,
    secure_cookie_enabled,
    self_signup_enabled,
    session_ttl_days,
    verify_password,
)
from .csv_tools import inspect_csv
from .clickhouse_tools import (
    inspect_clickhouse_table,
    parse_clickhouse_connection_url,
    parse_clickhouse_identifier,
)
from .data_source_access import (
    DATA_SOURCE_CLASSIFICATIONS,
    DATA_SOURCE_PERMISSIONS,
    DATA_SOURCE_VISIBILITIES,
    can_export_data_source,
    can_manage_data_source,
    can_query_data_source,
    can_query_data_source_for_member,
    can_view_data_source,
    data_source_access_level,
    require_data_source_access,
    require_data_source_export_access,
)
from .data_masking import FIELD_CLASSIFICATIONS, MASKING_POLICIES, apply_export_masking
from .db import UPLOAD_DIR, connect, create_default_report_widgets, decode_json, encode_json, init_db, record_audit, rows_to_dicts
from .metrics import render_prometheus_metrics, workspace_run_usage
from .lineage import data_source_impact
from .mysql_tools import inspect_mysql_table, parse_mysql_connection_url, parse_mysql_identifier
from .notification_delivery import dispatch_due_notification_deliveries, parse_notification_channel
from .parquet_tools import inspect_parquet
from .postgres_tools import inspect_postgres_table, parse_postgres_connection_url, parse_postgres_identifier
from .report_export import render_report_pdf, render_report_png
from .report_subscriptions import (
    available_subscription_channels,
    notify_report_subscribers,
    selected_subscription_channel_ids,
    set_subscription_channels,
)
from .runner import cancel_run_execution, claim_run_execution, create_report_snapshot, create_run, execute_run, now_iso, select_run_version
from .runtime_profiles import get_runtime_profiles, normalize_runtime_profile
from .run_search import RUN_STATUSES, RUN_TRIGGER_TYPES, RunSearchFilters, search_workspace_runs
from .s3_snapshots import import_s3_snapshot, refresh_s3_snapshot
from .s3_tools import (
    parse_s3_bucket,
    parse_s3_object_key,
    s3_secret_redaction_values,
)
from .schema_tools import LOGICAL_TYPES, build_column_metadata
from .secret_tools import (
    data_source_secret_environment_name,
    parse_secret_reference,
    parse_secret_target_environment_name,
    redact_text,
    resolve_secret_reference_value,
)
from .sqlite_tools import inspect_sqlite_table
from .storage_usage import (
    DEFAULT_WORKSPACE_STORAGE_BYTES,
    MEBIBYTE,
    ensure_workspace_storage_capacity,
    paths_storage_bytes,
    workspace_storage_bytes,
)
from .xlsx_tools import inspect_xlsx, render_xlsx, write_rows_csv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL = "SELECT * FROM data LIMIT 25;"
DEFAULT_PYTHON = """rows = load_csv()
result = rows[:25]
"""
DEFAULT_PARAMETERS_JSON = "{}"
DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
DEFAULT_WORKSPACE_QUOTA_LIMIT = 100
DEFAULT_WORKSPACE_RUN_CONCURRENCY_LIMIT = 2
PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUOTA_RESOURCES = ("data_sources", "projects", "schedules", "reports", "concurrent_runs", "storage_bytes")
QUOTA_RESOURCE_LABELS = {
    "data_sources": "data source",
    "projects": "project",
    "schedules": "schedule",
    "reports": "report",
    "concurrent_runs": "concurrent run",
    "storage_bytes": "data source storage",
}
REPORT_VISIBILITIES = {"workspace", "private"}
SCHEDULE_CONCURRENCY_POLICIES = {"skip", "queue_one", "queue_all", "cancel_previous"}
BACKFILL_MAX_RUNS = 100
BACKFILL_SCHEDULED_FOR_PARAMETER = "__anydatas_scheduled_for"
RUN_RESULT_PAGE_SIZE = 100
RUN_LOG_PAGE_SIZE = 200
REPORT_WIDGET_KINDS = {"metric", "table", "bar", "line", "scatter", "pie", "markdown"}
REPORT_WIDGET_WIDTHS = {"quarter", "half", "full"}
REPORT_METRIC_AGGREGATES = {"row_count", "column_count", "count", "sum", "average", "minimum", "maximum"}
REPORT_FILTER_TYPES = {"select", "contains", "range"}
REPORT_TABLE_HIGHLIGHT_RULES = {"none", "positive", "negative", "above", "below"}
PIE_COLORS = ("#1677c8", "#0f9d78", "#d96c3d", "#8b5cf6", "#d1495b", "#7c8f3f", "#b26bce", "#2a9d8f")


def parse_data_source_classification(value: str) -> str:
    classification = value.strip().lower()
    if classification not in DATA_SOURCE_CLASSIFICATIONS:
        raise ValueError("Data classification must be public, internal, confidential, or restricted.")
    return classification


def parse_project_parameters(raw_parameters: str) -> tuple[dict[str, Any], str]:
    normalized = raw_parameters.strip() or DEFAULT_PARAMETERS_JSON

    def reject_non_standard_constant(_value: str) -> None:
        raise ValueError("Non-standard JSON constant")

    try:
        parameters = json.loads(normalized, parse_constant=reject_non_standard_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Parameters must be a valid JSON object.") from exc
    if not isinstance(parameters, dict):
        raise ValueError("Parameters must be a JSON object.")
    invalid_names = [name for name in parameters if not PARAMETER_NAME_PATTERN.fullmatch(name)]
    if invalid_names:
        raise ValueError("Parameter names must start with a letter or underscore and use only letters, numbers, or underscores.")
    return parameters, encode_json(parameters)


def parse_report_widget_config(
    kind: str,
    title: str,
    aggregate: str,
    value_column: str,
    label_column: str,
    x_column: str,
    markdown_text: str,
    table_limit: int,
    table_highlight_column: str,
    table_highlight_rule: str,
    table_highlight_threshold: str,
    widget_width: str = "",
) -> tuple[str, str, dict[str, Any]]:
    normalized_kind = kind.strip().lower()
    if normalized_kind not in REPORT_WIDGET_KINDS:
        raise ValueError("Unsupported report component.")
    normalized_title = title.strip()
    normalized_value_column = value_column.strip()
    normalized_label_column = label_column.strip()
    normalized_x_column = x_column.strip()
    normalized_highlight_column = table_highlight_column.strip()
    normalized_width = widget_width.strip().lower()
    if normalized_width and normalized_width not in REPORT_WIDGET_WIDTHS:
        raise ValueError("Unsupported report component width.")

    def with_layout(config: dict[str, Any]) -> dict[str, Any]:
        config["width"] = normalized_width or default_report_widget_width(normalized_kind)
        return config
    if any(
        len(value) > 120
        for value in (
            normalized_title,
            normalized_value_column,
            normalized_label_column,
            normalized_x_column,
            normalized_highlight_column,
        )
    ):
        raise ValueError("Component labels and column names must be 120 characters or fewer.")
    default_titles = {
        "metric": "Metric",
        "table": "Result Table",
        "bar": "Comparison",
        "line": "Trend",
        "scatter": "Distribution",
        "pie": "Share",
        "markdown": "Notes",
    }
    if normalized_kind == "metric":
        normalized_aggregate = aggregate.strip().lower() or "sum"
        if normalized_aggregate not in REPORT_METRIC_AGGREGATES:
            raise ValueError("Unsupported metric aggregate.")
        return normalized_kind, normalized_title or default_titles[normalized_kind], with_layout(
            {
                "aggregate": normalized_aggregate,
                "value_column": normalized_value_column,
            }
        )
    if normalized_kind == "table":
        if table_limit < 1 or table_limit > 500:
            raise ValueError("Table row limit must be between 1 and 500.")
        normalized_highlight_rule = table_highlight_rule.strip().lower() or "none"
        if normalized_highlight_rule not in REPORT_TABLE_HIGHLIGHT_RULES:
            raise ValueError("Unsupported table highlight rule.")
        normalized_highlight_threshold = table_highlight_threshold.strip()
        threshold = as_number(normalized_highlight_threshold) if normalized_highlight_threshold else None
        if normalized_highlight_rule in {"above", "below"} and threshold is None:
            raise ValueError("Table threshold rules require a finite numeric threshold.")
        return normalized_kind, normalized_title or default_titles[normalized_kind], with_layout(
            {
                "limit": table_limit,
                "highlight_column": normalized_highlight_column,
                "highlight_rule": normalized_highlight_rule,
                "highlight_threshold": threshold,
            }
        )
    if normalized_kind in {"bar", "line", "pie"}:
        return normalized_kind, normalized_title or default_titles[normalized_kind], with_layout(
            {
                "label_column": normalized_label_column,
                "value_column": normalized_value_column,
            }
        )
    if normalized_kind == "scatter":
        return normalized_kind, normalized_title or default_titles[normalized_kind], with_layout(
            {
                "x_column": normalized_x_column,
                "value_column": normalized_value_column,
            }
        )
    content = markdown_text.strip()
    if len(content) > 20000:
        raise ValueError("Markdown content must be 20,000 characters or fewer.")
    return normalized_kind, normalized_title or default_titles[normalized_kind], with_layout({"content": content})


def default_report_widget_width(kind: str) -> str:
    if kind == "metric":
        return "quarter"
    if kind in {"bar", "line", "scatter", "pie"}:
        return "half"
    return "full"


def parse_report_filter_config(name: str, column_name: str, filter_type: str, default_value: str) -> tuple[str, str, str, str]:
    normalized_name = name.strip()
    normalized_column_name = column_name.strip()
    normalized_filter_type = filter_type.strip().lower()
    normalized_default_value = default_value.strip()
    if not normalized_name or not normalized_column_name:
        raise ValueError("Filter name and column are required.")
    if normalized_filter_type not in REPORT_FILTER_TYPES:
        raise ValueError("Unsupported report filter type.")
    if len(normalized_name) > 120 or len(normalized_column_name) > 120:
        raise ValueError("Filter names and columns must be 120 characters or fewer.")
    if len(normalized_default_value) > 250:
        raise ValueError("Filter default values must be 250 characters or fewer.")
    if normalized_filter_type == "range" and normalized_default_value:
        bounds = normalized_default_value.split(",", 1)
        if len(bounds) != 2 or any(as_number(bound.strip()) is None for bound in bounds):
            raise ValueError("Range filter defaults must use min,max numeric values.")
    return normalized_name, normalized_column_name, normalized_filter_type, normalized_default_value


def default_workspace_quota(resource: str) -> int:
    if resource == "storage_bytes":
        default = DEFAULT_WORKSPACE_STORAGE_BYTES
    else:
        default = DEFAULT_WORKSPACE_RUN_CONCURRENCY_LIMIT if resource == "concurrent_runs" else DEFAULT_WORKSPACE_QUOTA_LIMIT
    configured = os.getenv(f"ANYDATAS_DEFAULT_MAX_{resource.upper()}", str(default))
    try:
        return max(int(configured), 0)
    except ValueError:
        return default


def runner_hourly_cost_cny() -> float:
    try:
        configured = float(os.getenv("ANYDATAS_RUNNER_COST_PER_HOUR_CNY", "0"))
    except ValueError:
        return 0
    return configured if math.isfinite(configured) and configured > 0 else 0


def get_workspace_quota(conn, workspace_id: str) -> dict[str, Any]:
    defaults = {resource: default_workspace_quota(resource) for resource in QUOTA_RESOURCES}
    conn.execute(
        """
        INSERT OR IGNORE INTO workspace_quotas (
            workspace_id, max_data_sources, max_projects, max_schedules, max_reports, max_concurrent_runs, max_storage_bytes, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workspace_id,
            defaults["data_sources"],
            defaults["projects"],
            defaults["schedules"],
            defaults["reports"],
            defaults["concurrent_runs"],
            defaults["storage_bytes"],
            now_iso(),
        ),
    )
    quota = conn.execute(
        "SELECT * FROM workspace_quotas WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return dict(quota)


def get_workspace_usage(conn, workspace_id: str) -> dict[str, int]:
    data_source_count = conn.execute(
        "SELECT COUNT(*) AS count FROM data_sources WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()["count"]
    project_count = conn.execute(
        "SELECT COUNT(*) AS count FROM projects WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()["count"]
    schedule_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM schedules s
        JOIN projects p ON p.id = s.project_id
        WHERE p.workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()["count"]
    report_count = conn.execute(
        "SELECT COUNT(*) AS count FROM reports WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()["count"]
    concurrent_run_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM runs r
        JOIN projects p ON p.id = r.project_id
        WHERE p.workspace_id = ?
          AND r.status IN ('running', 'canceling')
        """,
        (workspace_id,),
    ).fetchone()["count"]
    return {
        "data_sources": int(data_source_count),
        "projects": int(project_count),
        "schedules": int(schedule_count),
        "reports": int(report_count),
        "concurrent_runs": int(concurrent_run_count),
        "storage_bytes": workspace_storage_bytes(conn, workspace_id),
    }


def ensure_workspace_capacity(conn, workspace_id: str, resource: str) -> None:
    quota = get_workspace_quota(conn, workspace_id)
    usage = get_workspace_usage(conn, workspace_id)
    limit = int(quota[f"max_{resource}"])
    if usage[resource] >= limit:
        raise ValueError(f"Workspace has reached the {QUOTA_RESOURCE_LABELS[resource]} limit of {limit}.")


def can_view_report(conn, context, report) -> bool:
    if report["visibility"] != "private":
        return True
    if context.role in {"owner", "admin"} or report["created_by_user_id"] == context.user_id:
        return True
    return (
        conn.execute(
            """
            SELECT 1
            FROM report_access_grants
            WHERE report_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (report["id"], context.workspace_id, context.user_id),
        ).fetchone()
        is not None
    )


def queryable_workspace_source_ids(conn, context) -> set[str]:
    sources = conn.execute(
        "SELECT * FROM data_sources WHERE workspace_id = ?",
        (context.workspace_id,),
    ).fetchall()
    return {source["id"] for source in sources if can_query_data_source(conn, context, source)}


def can_manage_report(context, report) -> bool:
    return context.role in {"owner", "admin"} or report["created_by_user_id"] == context.user_id


def get_workspace_data_source(conn, workspace_id: str, source_id: str):
    return conn.execute(
        "SELECT * FROM data_sources WHERE id = ? AND workspace_id = ?",
        (source_id, workspace_id),
    ).fetchone()


def require_workspace_data_source_access(conn, context, source_id: str, minimum_permission: str):
    source = get_workspace_data_source(conn, context.workspace_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    require_data_source_access(conn, context, source, minimum_permission)
    return source


def require_workspace_project_query_access(conn, context, project_id: str):
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ? AND workspace_id = ?",
        (project_id, context.workspace_id),
    ).fetchone()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    source = get_workspace_data_source(conn, context.workspace_id, project["data_source_id"])
    if source is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    require_data_source_access(conn, context, source, "query")
    return project, source


def require_workspace_run_query_access(conn, context, run_id: str):
    run = get_workspace_run(conn, context.workspace_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    source = get_workspace_data_source(conn, context.workspace_id, run["data_source_id"])
    if source is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    require_data_source_access(conn, context, source, "query")
    return run


def require_workspace_report_query_access(conn, context, report_id: str):
    report = get_workspace_report(conn, context.workspace_id, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    require_report_view(conn, context, report)
    _project, source = require_workspace_project_query_access(conn, context, report["project_id"])
    return report, source


def can_view_source_bound_resource(conn, context, resource_type: str, resource_id: str) -> bool:
    if context.role in {"owner", "admin"}:
        return True
    if not resource_type or not resource_id:
        return True
    if resource_type == "data_source":
        source = get_workspace_data_source(conn, context.workspace_id, resource_id)
        return source is not None and can_view_data_source(conn, context, source)
    if resource_type == "project":
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND workspace_id = ?",
            (resource_id, context.workspace_id),
        ).fetchone()
        if project is None:
            return False
        source = get_workspace_data_source(conn, context.workspace_id, project["data_source_id"])
        return source is not None and can_query_data_source(conn, context, source)
    if resource_type == "run":
        run = get_workspace_run(conn, context.workspace_id, resource_id)
        if run is None:
            return False
        source = get_workspace_data_source(conn, context.workspace_id, run["data_source_id"])
        return source is not None and can_query_data_source(conn, context, source)
    if resource_type == "schedule":
        schedule = conn.execute(
            """
            SELECT s.project_id
            FROM schedules s
            JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.workspace_id = ?
            """,
            (resource_id, context.workspace_id),
        ).fetchone()
        return schedule is not None and can_view_source_bound_resource(conn, context, "project", schedule["project_id"])
    if resource_type == "report":
        report = get_workspace_report(conn, context.workspace_id, resource_id)
        if report is None or not can_view_report(conn, context, report):
            return False
        return can_view_source_bound_resource(conn, context, "project", report["project_id"])
    return True


def require_report_view(conn, context, report) -> None:
    if not can_view_report(conn, context, report):
        raise HTTPException(status_code=404, detail="Report not found")


def prune_private_report_subscriptions(conn, report) -> int:
    ineligible_subscribers = conn.execute(
        """
        SELECT subscription.user_id
        FROM report_subscriptions subscription
        LEFT JOIN memberships member
          ON member.user_id = subscription.user_id
          AND member.workspace_id = subscription.workspace_id
        WHERE subscription.report_id = ?
          AND subscription.workspace_id = ?
          AND (
            member.user_id IS NULL
            OR (
              member.role NOT IN ('owner', 'admin')
              AND (? IS NULL OR subscription.user_id != ?)
              AND NOT EXISTS (
                SELECT 1
                FROM report_access_grants grant_record
                WHERE grant_record.report_id = subscription.report_id
                  AND grant_record.workspace_id = subscription.workspace_id
                  AND grant_record.user_id = subscription.user_id
              )
            )
          )
        """,
        (
            report["id"],
            report["workspace_id"],
            report["created_by_user_id"],
            report["created_by_user_id"],
        ),
    ).fetchall()
    for subscriber in ineligible_subscribers:
        user_id = subscriber["user_id"]
        conn.execute(
            """
            DELETE FROM notifications
            WHERE workspace_id = ?
              AND resource_type = 'report'
              AND resource_id = ?
              AND recipient_user_id = ?
            """,
            (report["workspace_id"], report["id"], user_id),
        )
        conn.execute(
            "DELETE FROM report_subscriptions WHERE report_id = ? AND workspace_id = ? AND user_id = ?",
            (report["id"], report["workspace_id"], user_id),
        )
    return len(ineligible_subscribers)


def prune_ineligible_data_source_subscriptions(conn, source) -> int:
    reports = conn.execute(
        """
        SELECT report.id, report.workspace_id, report.project_id
        FROM reports report
        JOIN projects project ON project.id = report.project_id
        WHERE project.data_source_id = ? AND report.workspace_id = ?
        """,
        (source["id"], source["workspace_id"]),
    ).fetchall()
    removed = 0
    for report in reports:
        subscribers = conn.execute(
            """
            SELECT subscription.user_id, member.role
            FROM report_subscriptions subscription
            JOIN memberships member
              ON member.user_id = subscription.user_id
              AND member.workspace_id = subscription.workspace_id
            WHERE subscription.report_id = ? AND subscription.workspace_id = ?
            """,
            (report["id"], report["workspace_id"]),
        ).fetchall()
        for subscriber in subscribers:
            if can_query_data_source_for_member(
                conn,
                report["workspace_id"],
                subscriber["user_id"],
                subscriber["role"],
                source,
            ):
                continue
            conn.execute(
                """
                DELETE FROM notifications
                WHERE workspace_id = ?
                  AND resource_type = 'report'
                  AND resource_id = ?
                  AND recipient_user_id = ?
                """,
                (report["workspace_id"], report["id"], subscriber["user_id"]),
            )
            conn.execute(
                "DELETE FROM report_subscriptions WHERE report_id = ? AND workspace_id = ? AND user_id = ?",
                (report["id"], report["workspace_id"], subscriber["user_id"]),
            )
            removed += 1
    return removed


def list_visible_notifications(conn, context, limit: int) -> list[dict[str, Any]]:
    candidates = rows_to_dicts(
        conn.execute(
            """
            SELECT *
            FROM notifications
            WHERE workspace_id = ?
              AND (recipient_user_id IS NULL OR recipient_user_id = ?)
            ORDER BY is_read ASC, created_at DESC
            LIMIT ?
            """,
            (context.workspace_id, context.user_id, max(limit * 10, 100)),
        ).fetchall()
    )
    return [
        notification
        for notification in candidates
        if can_view_source_bound_resource(conn, context, notification["resource_type"], notification["resource_id"])
    ][:limit]


def count_visible_unread_notifications(conn, context) -> int:
    notifications = conn.execute(
        """
        SELECT resource_type, resource_id
        FROM notifications
        WHERE workspace_id = ?
          AND is_read = 0
          AND (recipient_user_id IS NULL OR recipient_user_id = ?)
        """,
        (context.workspace_id, context.user_id),
    ).fetchall()
    return sum(
        1
        for notification in notifications
        if can_view_source_bound_resource(conn, context, notification["resource_type"], notification["resource_id"])
    )


def list_visible_audit_events(conn, context, limit: int) -> list[dict[str, Any]]:
    candidates = rows_to_dicts(
        conn.execute(
            """
            SELECT *
            FROM audit_events
            WHERE workspace_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (context.workspace_id, max(limit * 10, 100)),
        ).fetchall()
    )
    return [
        event
        for event in candidates
        if can_view_source_bound_resource(conn, context, event["resource_type"], event["resource_id"])
    ][:limit]


def get_workspace_members(conn, workspace_id: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.name, m.role, m.created_at
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.workspace_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM service_accounts service_account
                WHERE service_account.user_id = u.id
              )
            ORDER BY m.created_at ASC
            """,
            (workspace_id,),
        ).fetchall()
    )


def get_workspace_secret_references(conn, workspace_id: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT *
            FROM secret_references
            WHERE workspace_id = ?
            ORDER BY name ASC
            """,
            (workspace_id,),
        ).fetchall()
    )


def get_workspace_notification_channels(conn, workspace_id: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT channel.*, reference.name AS secret_name
            FROM notification_channels channel
            LEFT JOIN secret_references reference ON reference.id = channel.secret_id
            WHERE channel.workspace_id = ?
            ORDER BY channel.created_at DESC
            """,
            (workspace_id,),
        ).fetchall()
    )


def get_workspace_notification_deliveries(conn, workspace_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT
                delivery.*,
                notification.event_type,
                notification.title,
                channel.id AS current_channel_id,
                channel.is_active AS channel_is_active
            FROM notification_deliveries delivery
            JOIN notifications notification ON notification.id = delivery.notification_id
            LEFT JOIN notification_channels channel
              ON channel.id = delivery.channel_id
             AND channel.workspace_id = delivery.workspace_id
            WHERE delivery.workspace_id = ?
            ORDER BY delivery.created_at DESC
            LIMIT ?
            """,
            (workspace_id, limit),
        ).fetchall()
    )


def get_project_secret_bindings(conn, workspace_id: str, project_id: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT
                binding.secret_id,
                binding.environment_name,
                reference.name AS secret_name,
                reference.environment_variable,
                reference.description
            FROM project_secret_bindings binding
            JOIN secret_references reference ON reference.id = binding.secret_id
            WHERE binding.workspace_id = ? AND binding.project_id = ?
            ORDER BY binding.environment_name ASC
            """,
            (workspace_id, project_id),
        ).fetchall()
    )


def get_project_secret_binding_snapshot(conn, workspace_id: str, project_id: str) -> list[dict[str, str]]:
    return [
        {"secret_id": binding["secret_id"], "environment_name": binding["environment_name"]}
        for binding in get_project_secret_bindings(conn, workspace_id, project_id)
    ]


def create_project_version_for_secret_bindings(conn, project, workspace_id: str, timestamp: str) -> str:
    secret_bindings_json = encode_json(get_project_secret_binding_snapshot(conn, workspace_id, project["id"]))
    version_id = create_project_version(
        conn,
        project["id"],
        project["language"],
        project["script"],
        project["parameters_json"],
        project["runtime_profile"],
        project["data_source_id"],
        timestamp,
        secret_bindings_json,
    )
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, project["id"]))
    return version_id


def get_report_grantees(conn, workspace_id: str, report_id: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.name, m.role, grant_record.created_at
            FROM report_access_grants grant_record
            JOIN memberships m
              ON m.user_id = grant_record.user_id
              AND m.workspace_id = grant_record.workspace_id
            JOIN users u ON u.id = grant_record.user_id
            WHERE grant_record.report_id = ? AND grant_record.workspace_id = ?
            ORDER BY u.name ASC, u.email ASC
            """,
            (report_id, workspace_id),
        ).fetchall()
    )


def get_data_source_grantees(conn, workspace_id: str, source_id: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.name, m.role, grant_record.permission, grant_record.created_at
            FROM data_source_access_grants grant_record
            JOIN memberships m
              ON m.user_id = grant_record.user_id
              AND m.workspace_id = grant_record.workspace_id
            JOIN users u ON u.id = grant_record.user_id
            WHERE grant_record.data_source_id = ? AND grant_record.workspace_id = ?
            ORDER BY u.name ASC, u.email ASC
            """,
            (source_id, workspace_id),
        ).fetchall()
    )


def parse_cron_field(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    if not field:
        raise ValueError("Cron fields cannot be empty.")
    for item in field.split(","):
        token = item.strip()
        if not token:
            raise ValueError("Cron fields cannot contain empty list items.")
        if "/" in token:
            base, step_value = token.split("/", 1)
            if not step_value.isdigit() or int(step_value) < 1:
                raise ValueError("Cron step values must be positive integers.")
            step = int(step_value)
        else:
            base = token
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_value, end_value = base.split("-", 1)
            if not start_value.isdigit() or not end_value.isdigit():
                raise ValueError("Cron ranges must use integer boundaries.")
            start, end = int(start_value), int(end_value)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise ValueError("Cron fields support *, ranges, steps, and comma-separated integers.")

        if start < minimum or end > maximum or start > end:
            raise ValueError(f"Cron field value must be between {minimum} and {maximum}.")
        values.update(range(start, end + 1, step))
    return values


def parse_cron_expression(expression: str) -> dict[str, set[int]]:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have five fields.")
    weekday_values = parse_cron_field(fields[4], 0, 7)
    if 7 in weekday_values:
        weekday_values.add(0)
        weekday_values.remove(7)
    return {
        "minute": parse_cron_field(fields[0], 0, 59),
        "hour": parse_cron_field(fields[1], 0, 23),
        "day": parse_cron_field(fields[2], 1, 31),
        "month": parse_cron_field(fields[3], 1, 12),
        "weekday": weekday_values,
    }


def cron_matches(local_dt: datetime, parsed: dict[str, set[int]]) -> bool:
    cron_weekday = (local_dt.weekday() + 1) % 7
    return (
        local_dt.minute in parsed["minute"]
        and local_dt.hour in parsed["hour"]
        and local_dt.day in parsed["day"]
        and local_dt.month in parsed["month"]
        and cron_weekday in parsed["weekday"]
    )


def next_cron_run(expression: str, from_dt: datetime, timezone_name: str = "UTC") -> str:
    if not expression.strip():
        raise ValueError("Cron expression is required.")
    try:
        target_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc

    parsed = parse_cron_expression(expression)
    aware_from = from_dt if from_dt.tzinfo is not None else from_dt.replace(tzinfo=timezone.utc)
    candidate = aware_from.astimezone(target_timezone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = candidate + timedelta(days=366)
    while candidate <= deadline:
        if cron_matches(candidate, parsed):
            return candidate.astimezone(timezone.utc).isoformat()
        candidate += timedelta(minutes=1)
    raise ValueError("Cron expression did not produce a run time within one year.")


def schedule_next_run_at(schedule, from_dt: datetime) -> str:
    if schedule["schedule_type"] == "cron":
        return next_cron_run(schedule["cron_expression"], from_dt, schedule["timezone"] or "UTC")
    return (from_dt + timedelta(minutes=schedule["interval_minutes"])).isoformat()


def parse_backfill_datetime(value: str, timezone_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("Backfill timestamps must be valid ISO-8601 date and time values.") from exc
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name or "UTC"))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    return parsed.astimezone(timezone.utc)


def schedule_backfill_occurrences(schedule, start_at: datetime, end_at: datetime, max_runs: int) -> list[str]:
    if start_at > end_at:
        raise ValueError("Backfill start time must be before the end time.")
    if max_runs < 1 or max_runs > BACKFILL_MAX_RUNS:
        raise ValueError(f"Backfill run limit must be between 1 and {BACKFILL_MAX_RUNS}.")

    occurrences: list[datetime] = []

    def append_occurrence(occurrence: datetime) -> None:
        if len(occurrences) >= max_runs:
            raise ValueError(f"Backfill range produces more than {max_runs} runs. Narrow the range or raise the limit.")
        occurrences.append(occurrence)

    if schedule["schedule_type"] == "interval":
        try:
            anchor = datetime.fromisoformat(schedule["created_at"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Schedule creation timestamp is invalid.") from exc
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        anchor = anchor.astimezone(timezone.utc)
        interval = timedelta(minutes=int(schedule["interval_minutes"]))
        if interval <= timedelta(0):
            raise ValueError("Schedule interval must be at least 1 minute.")
        if anchor < start_at:
            steps, remainder = divmod(start_at - anchor, interval)
            anchor += interval * (steps + (1 if remainder else 0))
        while anchor <= end_at:
            append_occurrence(anchor)
            anchor += interval
    else:
        start_minute = start_at.replace(second=0, microsecond=0)
        cursor = start_minute - timedelta(minutes=1) if start_at == start_minute else start_minute
        while True:
            occurrence = datetime.fromisoformat(next_cron_run(schedule["cron_expression"], cursor, schedule["timezone"] or "UTC"))
            if occurrence > end_at:
                break
            append_occurrence(occurrence)
            cursor = occurrence

    return [occurrence.isoformat() for occurrence in occurrences]


def max_upload_bytes() -> int:
    configured = os.getenv("ANYDATAS_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    try:
        value = int(configured)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return max(value, 1)


def copy_upload_with_limit(upload: UploadFile, destination: Path, limit_bytes: int) -> int:
    total = 0
    with destination.open("wb") as handle:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit_bytes:
                destination.unlink(missing_ok=True)
                raise ValueError(f"File exceeds upload limit of {round(limit_bytes / (1024 * 1024), 2)} MB.")
            handle.write(chunk)
    return total


def build_data_source_schema_fields(source) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], dict[str, dict[str, str]]]:
    raw_columns = decode_json(source["columns_json"], [])
    columns = [str(column) for column in raw_columns] if isinstance(raw_columns, list) else []
    raw_preview = decode_json(source["preview_json"], [])
    preview = [row for row in raw_preview if isinstance(row, dict)] if isinstance(raw_preview, list) else []
    metadata = build_column_metadata(columns, preview, decode_json(source["column_metadata_json"], {}))
    quality = decode_json(source["quality_json"], {})
    quality_columns = quality.get("columns", []) if isinstance(quality, dict) else []
    quality_by_name = {
        item.get("name"): item
        for item in quality_columns
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    fields = []
    for column in columns:
        quality_summary = quality_by_name.get(column, {})
        fields.append(
            {
                "name": column,
                "type": metadata[column]["type"],
                "description": metadata[column]["description"],
                "classification": metadata[column]["classification"],
                "masking": metadata[column]["masking"],
                "empty": quality_summary.get("empty", 0),
                "unique": quality_summary.get("unique", 0),
                "samples": quality_summary.get("sample_values", []),
            }
        )
    return fields, columns, preview, metadata


@contextlib.asynccontextmanager
async def lifespan(app_instance: FastAPI):
    password_auth_enabled()
    get_runtime_profiles()
    init_db()
    if os.getenv("ANYDATAS_DISABLE_SCHEDULER") != "1":
        app_instance.state.scheduler_last_tick = time.time()
        app_instance.state.scheduler_task = asyncio.create_task(schedule_loop(app_instance))
    try:
        yield
    finally:
        scheduler_task = getattr(app_instance.state, "scheduler_task", None)
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task


app = FastAPI(title="AnyDatas MVP", lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def shell_context(context, nav_section: str = "") -> dict[str, Any]:
    """Shared chrome for workspace home and multi-page detail views."""
    return {
        "context": context,
        "password_auth": password_auth_enabled(),
        "nav_section": nav_section,
    }


@app.middleware("http")
async def redirect_unauthenticated_browser(request: Request, call_next):
    response = await call_next(request)
    if (
        response.status_code == 401
        and request.method == "GET"
        and request.url.path != "/login"
        and "text/html" in request.headers.get("accept", "")
    ):
        return RedirectResponse("/login", status_code=303)
    return response


@app.get("/healthz")
async def health_check(request: Request) -> dict[str, str]:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    scheduler_disabled = os.getenv("ANYDATAS_DISABLE_SCHEDULER") == "1"
    scheduler_task = getattr(request.app.state, "scheduler_task", None)
    scheduler_status = "disabled" if scheduler_disabled else "running" if scheduler_task and not scheduler_task.done() else "starting"
    return {
        "status": "ok",
        "database": "ok",
        "scheduler": scheduler_status,
        "runner": os.getenv("ANYDATAS_RUNNER", "local").lower(),
    }


@app.get("/readyz")
async def readiness_check(request: Request) -> dict[str, str]:
    return await health_check(request)


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    configured_token = os.getenv("ANYDATAS_METRICS_TOKEN", "").strip()
    token_file = os.getenv("ANYDATAS_METRICS_TOKEN_FILE", "").strip()
    if token_file:
        try:
            configured_token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Metrics token file is unavailable") from exc
        if not configured_token:
            raise HTTPException(status_code=503, detail="Metrics token file is empty")
    if configured_token:
        authorization = request.headers.get("authorization", "")
        if not secrets.compare_digest(authorization, f"Bearer {configured_token}"):
            raise HTTPException(status_code=401, detail="Metrics authentication required", headers={"WWW-Authenticate": "Bearer"})
    scheduler_enabled = os.getenv("ANYDATAS_DISABLE_SCHEDULER") != "1"
    scheduler_task = getattr(request.app.state, "scheduler_task", None)
    scheduler_running = bool(scheduler_task and not scheduler_task.done())
    try:
        with connect() as conn:
            payload = render_prometheus_metrics(
                conn,
                os.getenv("ANYDATAS_RUNNER", "local").lower(),
                scheduler_enabled,
                scheduler_running,
                getattr(request.app.state, "scheduler_last_tick", None),
            )
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    return Response(payload, headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, notice: Optional[str] = None) -> HTMLResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        raw_data_sources = rows_to_dicts(
            conn.execute(
                "SELECT * FROM data_sources WHERE workspace_id = ? ORDER BY created_at DESC",
                (context.workspace_id,),
            ).fetchall()
        )
        data_sources = []
        queryable_data_sources = []
        queryable_source_ids: set[str] = set()
        for source in raw_data_sources:
            access_level = data_source_access_level(conn, context, source)
            source["access_level"] = access_level
            source["can_query"] = can_query_data_source(conn, context, source)
            source["can_manage"] = can_manage_data_source(conn, context, source)
            if not can_view_data_source(conn, context, source):
                continue
            data_sources.append(source)
            if source["can_query"]:
                queryable_data_sources.append(source)
                queryable_source_ids.add(source["id"])
        projects = rows_to_dicts(
            conn.execute(
                """
                SELECT
                    p.*,
                    d.name AS data_source_name,
                    published.version_number AS published_version_number,
                    latest.version_number AS latest_version_number
                FROM projects p
                JOIN data_sources d ON d.id = p.data_source_id
                LEFT JOIN project_versions published ON published.id = p.published_version_id
                LEFT JOIN (
                    SELECT project_id, MAX(version_number) AS version_number
                    FROM project_versions
                    GROUP BY project_id
                ) latest ON latest.project_id = p.id
                WHERE p.workspace_id = ?
                ORDER BY p.updated_at DESC
                """,
                (context.workspace_id,),
            ).fetchall()
        )
        projects = [project for project in projects if project["data_source_id"] in queryable_source_ids]
        runs = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*, p.name AS project_name, COALESCE(project_version.data_source_id, p.data_source_id) AS data_source_id
                FROM runs r
                JOIN projects p ON p.id = r.project_id
                LEFT JOIN project_versions project_version ON project_version.id = r.project_version_id
                WHERE p.workspace_id = ?
                ORDER BY r.started_at DESC
                LIMIT 30
                """,
                (context.workspace_id,),
            ).fetchall()
        )
        runs = [run for run in runs if run["data_source_id"] in queryable_source_ids]
        schedules = rows_to_dicts(
            conn.execute(
                """
                SELECT s.*, p.name AS project_name, p.data_source_id
                FROM schedules s
                JOIN projects p ON p.id = s.project_id
                WHERE p.workspace_id = ?
                ORDER BY s.created_at DESC
                """,
                (context.workspace_id,),
            ).fetchall()
        )
        schedules = [schedule for schedule in schedules if schedule["data_source_id"] in queryable_source_ids]
        reports = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*, p.name AS project_name, p.data_source_id
                FROM reports r
                JOIN projects p ON p.id = r.project_id
                WHERE r.workspace_id = ?
                  AND (
                    r.visibility = 'workspace'
                    OR r.created_by_user_id = ?
                    OR ? IN ('owner', 'admin')
                    OR (
                        r.visibility = 'private'
                        AND EXISTS (
                            SELECT 1
                            FROM report_access_grants report_grant
                            WHERE report_grant.report_id = r.id
                              AND report_grant.workspace_id = r.workspace_id
                              AND report_grant.user_id = ?
                        )
                    )
                  )
                ORDER BY r.updated_at DESC
                """,
                (context.workspace_id, context.user_id, context.role, context.user_id),
            ).fetchall()
        )
        visible_reports = []
        for report in reports:
            latest_snapshot = get_latest_report_snapshot(conn, report["id"])
            if report["data_source_id"] not in queryable_source_ids or (
                latest_snapshot is not None
                and not can_view_source_bound_resource(conn, context, "run", latest_snapshot["run_id"])
            ):
                continue
            report["latest_snapshot"] = dict(latest_snapshot) if latest_snapshot is not None else None
            visible_reports.append(report)
        reports = visible_reports
        audit_events = list_visible_audit_events(conn, context, 20)
        notifications = list_visible_notifications(conn, context, 10)
        unread_notifications = count_visible_unread_notifications(conn, context)
        members = get_workspace_members(conn, context.workspace_id)
        if password_auth_enabled():
            api_tokens = rows_to_dicts(
                conn.execute(
                    """
                    SELECT *
                    FROM api_tokens
                    WHERE user_id = ?
                      AND workspace_id = ?
                      AND revoked_at IS NULL
                      AND datetime(expires_at) > datetime('now')
                    ORDER BY created_at DESC
                    """,
                    (context.user_id, context.workspace_id),
                ).fetchall()
            )
        else:
            api_tokens = []
        if password_auth_enabled() and context.role in {"owner", "admin"}:
            pending_invitations = rows_to_dicts(
                conn.execute(
                    """
                    SELECT *
                    FROM workspace_invitations
                    WHERE workspace_id = ?
                      AND accepted_at IS NULL
                      AND revoked_at IS NULL
                      AND datetime(expires_at) > datetime('now')
                    ORDER BY created_at DESC
                    """,
                    (context.workspace_id,),
                ).fetchall()
            )
            service_accounts = rows_to_dicts(
                conn.execute(
                    """
                    SELECT
                        service_account.*,
                        COUNT(token.id) AS active_token_count,
                        MAX(token.last_used_at) AS last_used_at
                    FROM service_accounts service_account
                    LEFT JOIN api_tokens token
                      ON token.user_id = service_account.user_id
                      AND token.workspace_id = service_account.workspace_id
                      AND token.revoked_at IS NULL
                      AND datetime(token.expires_at) > datetime('now')
                    WHERE service_account.workspace_id = ?
                      AND service_account.revoked_at IS NULL
                    GROUP BY service_account.id
                    ORDER BY service_account.created_at DESC
                    """,
                    (context.workspace_id,),
                ).fetchall()
            )
        else:
            pending_invitations = []
            service_accounts = []
        secret_references = get_workspace_secret_references(conn, context.workspace_id)
        notification_channels = get_workspace_notification_channels(conn, context.workspace_id)
        notification_deliveries = get_workspace_notification_deliveries(conn, context.workspace_id) if context.role in {"owner", "admin"} else []
        project_secret_bindings = {
            project["id"]: get_project_secret_bindings(conn, context.workspace_id, project["id"])
            for project in projects
        }
        workspace_quota = get_workspace_quota(conn, context.workspace_id)
        workspace_usage = get_workspace_usage(conn, context.workspace_id)
        runner_hourly_cost = runner_hourly_cost_cny()
        run_usage = (
            workspace_run_usage(conn, context.workspace_id, runner_hourly_cost)
            if context.role in {"owner", "admin"}
            else []
        )
    for source in data_sources:
        source["columns"] = decode_json(source["columns_json"], [])
        source["column_metadata"] = build_column_metadata(
            source["columns"],
            decode_json(source["preview_json"], []),
            decode_json(source["column_metadata_json"], {}),
        )
        source["preview"] = decode_json(source["preview_json"], [])
        source["quality"] = decode_json(source["quality_json"], {})
    for project in projects:
        parameters = decode_json(project["parameters_json"], {})
        project["parameter_count"] = len(parameters) if isinstance(parameters, dict) else 0
        project["secret_bindings"] = project_secret_bindings.get(project["id"], [])
        project["secret_binding_ids"] = {binding["secret_id"] for binding in project["secret_bindings"]}
        project["reports"] = []
    projects_by_id = {project["id"]: project for project in projects}
    for report in reports:
        report["can_manage"] = can_manage_report(context, report)
        project = projects_by_id.get(report["project_id"])
        if project is not None:
            project["reports"].append(report)
    for channel in notification_channels:
        event_types = decode_json(channel["event_types_json"], [])
        channel["event_types"] = event_types if isinstance(event_types, list) else []
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "notice": notice,
            "data_sources": data_sources,
            "queryable_data_sources": queryable_data_sources,
            "projects": projects,
            "runs": runs,
            "schedules": schedules,
            "reports": reports,
            "audit_events": audit_events,
            "notifications": notifications,
            "unread_notifications": unread_notifications,
            "members": members,
            "api_tokens": api_tokens,
            "pending_invitations": pending_invitations,
            "service_accounts": service_accounts,
            "password_auth": password_auth_enabled(),
            "secret_references": secret_references,
            "notification_channels": notification_channels,
            "notification_deliveries": notification_deliveries,
            "can_manage_secrets": context.role in {"owner", "admin"},
            "can_bind_secrets": context.role in {"owner", "admin", "analyst"},
            "can_manage_notification_channels": context.role in {"owner", "admin"},
            "workspace_quota": workspace_quota,
            "workspace_usage": workspace_usage,
            "run_usage": run_usage,
            "runner_hourly_cost_cny": runner_hourly_cost,
            "context": context,
            "default_sql": DEFAULT_SQL,
            "default_python": DEFAULT_PYTHON,
            "default_parameters_json": DEFAULT_PARAMETERS_JSON,
            "runtime_profiles": list(get_runtime_profiles().values()),
            "max_upload_mb": round(max_upload_bytes() / (1024 * 1024), 2),
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, notice: Optional[str] = None) -> HTMLResponse:
    password_auth = password_auth_enabled()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "notice": notice,
            "password_auth": password_auth,
            "signup_enabled": password_auth and self_signup_enabled(),
        },
    )


@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    password: str = Form(""),
) -> Response:
    with connect() as conn:
        if password_auth_enabled():
            try:
                context = authenticate_password(
                    conn,
                    email,
                    password,
                    request.client.host if request.client else "unknown",
                )
            except HTTPException as exc:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    {
                        "error": exc.detail,
                        "notice": None,
                        "password_auth": True,
                        "signup_enabled": self_signup_enabled(),
                    },
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            session_token = create_session(conn, context.user_id, context.workspace_id)
        else:
            context = get_or_create_login_identity(conn, email, name)
            session_token = ""
        record_audit(conn, "user.login", "user", context.user_id, {"email": context.user_email}, context.workspace_id)
    response = RedirectResponse("/?notice=Signed%20in", status_code=303)
    if password_auth_enabled():
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_token,
            max_age=session_ttl_days() * 86400,
            httponly=True,
            secure=secure_cookie_enabled(),
            samesite="lax",
        )
        response.delete_cookie("anydatas_user_id")
        response.delete_cookie("anydatas_workspace_id")
    else:
        response.set_cookie("anydatas_user_id", context.user_id, httponly=True, samesite="lax")
        response.set_cookie("anydatas_workspace_id", context.workspace_id, httponly=True, samesite="lax")
    return response


@app.get("/register", response_class=HTMLResponse)
async def registration_page(request: Request) -> HTMLResponse:
    if not password_auth_enabled() or not self_signup_enabled():
        raise HTTPException(status_code=404, detail="Registration is not enabled")
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    password_confirmation: str = Form(...),
) -> Response:
    if not password_auth_enabled() or not self_signup_enabled():
        raise HTTPException(status_code=404, detail="Registration is not enabled")
    if password != password_confirmation:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Password confirmation does not match."},
            status_code=400,
        )
    with connect() as conn:
        try:
            context = register_password_identity(conn, email, name, password)
        except ValueError as exc:
            status_code = 409 if "already exists" in str(exc) else 400
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": str(exc)},
                status_code=status_code,
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return templates.TemplateResponse(
                request,
                "register.html",
                {"error": "An account with this email already exists."},
                status_code=409,
            )
        session_token = create_session(conn, context.user_id, context.workspace_id)
        record_audit(
            conn,
            "user.registered",
            "user",
            context.user_id,
            {"email": context.user_email},
            context.workspace_id,
        )
    response = RedirectResponse("/?notice=Account%20created", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=session_ttl_days() * 86400,
        httponly=True,
        secure=secure_cookie_enabled(),
        samesite="lax",
    )
    response.delete_cookie("anydatas_user_id")
    response.delete_cookie("anydatas_workspace_id")
    return response


@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def password_reset_page(request: Request, token: str) -> HTMLResponse:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Password reset link not found")
    with connect() as conn:
        reset = get_active_password_reset(conn, token)
    if reset is None:
        raise HTTPException(status_code=404, detail="Password reset link is invalid or expired")
    return templates.TemplateResponse(
        request,
        "password_reset.html",
        {"error": None, "token": token, "reset": dict(reset)},
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/reset-password/{token}")
async def reset_password(
    request: Request,
    token: str,
    password: str = Form(...),
    password_confirmation: str = Form(...),
) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Password reset link not found")
    with connect() as conn:
        reset = get_active_password_reset(conn, token)
        if reset is None:
            raise HTTPException(status_code=404, detail="Password reset link is invalid or expired")
        if password != password_confirmation:
            return templates.TemplateResponse(
                request,
                "password_reset.html",
                {"error": "Password confirmation does not match.", "token": token, "reset": dict(reset)},
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        try:
            reset, revoked_tokens = reset_password_with_token(conn, token, password)
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            return templates.TemplateResponse(
                request,
                "password_reset.html",
                {"error": exc.detail, "token": token, "reset": dict(reset)},
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        record_audit(
            conn,
            "user.password_reset",
            "user",
            reset["user_id"],
            {"revoked_api_tokens": revoked_tokens},
            reset["workspace_id"],
        )
    response = RedirectResponse("/login?notice=Password%20reset", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie("anydatas_user_id")
    response.delete_cookie("anydatas_workspace_id")
    return response


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    password_auth = password_auth_enabled()
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if password_auth and token:
        with connect() as conn:
            with contextlib.suppress(HTTPException):
                context = get_request_context(request, conn)
                record_audit(conn, "user.logout", "user", context.user_id, {}, context.workspace_id)
            revoke_session(conn, token)
    response = RedirectResponse("/login" if password_auth else "/?notice=Signed%20out", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    response.delete_cookie("anydatas_user_id")
    response.delete_cookie("anydatas_workspace_id")
    return response


@app.post("/account/password")
async def update_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirmation: str = Form(...),
) -> RedirectResponse:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Password management is unavailable in demo mode")
    if new_password != new_password_confirmation:
        return redirect_with_notice("New passwords do not match.")
    with connect() as conn:
        context = get_request_context(request, conn)
        user = conn.execute("SELECT * FROM users WHERE id = ?", (context.user_id,)).fetchone()
        if user is None or not verify_password(current_password, user["password_hash"]):
            return redirect_with_notice("Current password is incorrect.")
        if verify_password(new_password, user["password_hash"]):
            return redirect_with_notice("New password must differ from the current password.")
        try:
            encoded_password = hash_password(new_password)
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (encoded_password, context.user_id))
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (context.user_id,))
        session_token = create_session(conn, context.user_id, context.workspace_id)
        record_audit(
            conn,
            "user.password_changed",
            "user",
            context.user_id,
            {},
            context.workspace_id,
        )
    response = redirect_with_notice("Password changed. Other sessions were signed out.")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=session_ttl_days() * 86400,
        httponly=True,
        secure=secure_cookie_enabled(),
        samesite="lax",
    )
    return response


@app.post("/account/api-tokens", response_class=HTMLResponse)
async def create_personal_api_token(
    request: Request,
    name: str = Form(...),
    expires_days: int = Form(30),
    scope: str = Form("read"),
) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="API tokens require password authentication")
    if not request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="A password-authenticated browser session is required")
    with connect() as conn:
        context = get_request_context(request, conn)
        try:
            token_id, token = create_api_token(conn, context.user_id, context.workspace_id, name, expires_days, scope)
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        token_record = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        record_audit(
            conn,
            "user.api_token_created",
            "api_token",
            token_id,
            {
                "name": token_record["name"],
                "scope": token_record["scope"],
                "expires_at": token_record["expires_at"],
            },
            context.workspace_id,
        )
    return templates.TemplateResponse(
        request,
        "api_token_created.html",
        {"token": token, "token_record": dict(token_record)},
        status_code=201,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/account/api-tokens/{token_id}/revoke")
async def revoke_personal_api_token(request: Request, token_id: str) -> RedirectResponse:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="API token not found")
    if not request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="A password-authenticated browser session is required")
    with connect() as conn:
        context = get_request_context(request, conn)
        token_record = conn.execute(
            "SELECT * FROM api_tokens WHERE id = ? AND user_id = ? AND workspace_id = ?",
            (token_id, context.user_id, context.workspace_id),
        ).fetchone()
        if token_record is None:
            raise HTTPException(status_code=404, detail="API token not found")
        if token_record["revoked_at"] is None:
            conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (now_iso(), token_id))
            record_audit(
                conn,
                "user.api_token_revoked",
                "api_token",
                token_id,
                {"name": token_record["name"]},
                context.workspace_id,
            )
    return redirect_with_notice("API token revoked.")


@app.post("/service-accounts", response_class=HTMLResponse)
async def create_workspace_service_account(
    request: Request,
    name: str = Form(...),
    role: str = Form("viewer"),
    scope: str = Form("read"),
    expires_days: int = Form(30),
) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Service accounts require password authentication")
    if not request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="A password-authenticated browser session is required")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        try:
            service_account_id, token_id, token = create_service_account(
                conn,
                context.workspace_id,
                context.user_id,
                name,
                role,
                scope,
                expires_days,
            )
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        token_record = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        service_account = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ?",
            (service_account_id,),
        ).fetchone()
        record_audit(
            conn,
            "service_account.created",
            "service_account",
            service_account_id,
            {
                "name": service_account["name"],
                "role": service_account["role"],
                "scope": token_record["scope"],
                "expires_at": token_record["expires_at"],
            },
            context.workspace_id,
        )
    return templates.TemplateResponse(
        request,
        "api_token_created.html",
        {
            "token": token,
            "token_record": dict(token_record),
            "display_name": f"{service_account['name']} credential",
            "done_href": "/#service-accounts",
        },
        status_code=201,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/service-accounts/{service_account_id}/tokens", response_class=HTMLResponse)
async def rotate_service_account_token(
    request: Request,
    service_account_id: str,
    scope: str = Form("read"),
    expires_days: int = Form(30),
) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Service account not found")
    if not request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="A password-authenticated browser session is required")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        service_account = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ? AND workspace_id = ? AND revoked_at IS NULL",
            (service_account_id, context.workspace_id),
        ).fetchone()
        if service_account is None:
            raise HTTPException(status_code=404, detail="Service account not found")
        try:
            token_id, token = create_api_token(
                conn,
                service_account["user_id"],
                context.workspace_id,
                "Rotated credential",
                expires_days,
                scope,
            )
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        token_record = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()
        record_audit(
            conn,
            "service_account.token_created",
            "service_account",
            service_account_id,
            {"scope": token_record["scope"], "expires_at": token_record["expires_at"]},
            context.workspace_id,
        )
    return templates.TemplateResponse(
        request,
        "api_token_created.html",
        {
            "token": token,
            "token_record": dict(token_record),
            "display_name": f"{service_account['name']} credential",
            "done_href": "/#service-accounts",
        },
        status_code=201,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/service-accounts/{service_account_id}/revoke")
async def revoke_workspace_service_account(request: Request, service_account_id: str) -> RedirectResponse:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Service account not found")
    if not request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="A password-authenticated browser session is required")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        service_account = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ? AND workspace_id = ?",
            (service_account_id, context.workspace_id),
        ).fetchone()
        if service_account is None:
            raise HTTPException(status_code=404, detail="Service account not found")
        if service_account["revoked_at"] is None:
            timestamp = now_iso()
            revoked_tokens = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (timestamp, service_account["user_id"]),
            ).rowcount
            conn.execute("UPDATE service_accounts SET revoked_at = ? WHERE id = ?", (timestamp, service_account_id))
            conn.execute(
                "DELETE FROM memberships WHERE user_id = ? AND workspace_id = ?",
                (service_account["user_id"], context.workspace_id),
            )
            record_audit(
                conn,
                "service_account.revoked",
                "service_account",
                service_account_id,
                {"name": service_account["name"], "revoked_tokens": revoked_tokens},
                context.workspace_id,
            )
    return redirect_with_notice("Service account revoked.")


@app.get("/accept-invitation/{token}", response_class=HTMLResponse)
async def invitation_page(request: Request, token: str) -> HTMLResponse:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Invitation not found")
    with connect() as conn:
        invitation = get_active_invitation(conn, token)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid or expired")
    return templates.TemplateResponse(
        request,
        "invitation.html",
        {
            "error": None,
            "token": token,
            "invitation": dict(invitation),
            "existing_account": bool(invitation["existing_account"]),
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/accept-invitation/{token}")
async def accept_invitation(
    request: Request,
    token: str,
    password: str = Form(...),
    password_confirmation: str = Form(""),
) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Invitation not found")
    with connect() as conn:
        invitation = get_active_invitation(conn, token)
        if invitation is None:
            raise HTTPException(status_code=404, detail="Invitation is invalid or expired")
        existing_account = bool(invitation["existing_account"])
        if not existing_account and password != password_confirmation:
            return templates.TemplateResponse(
                request,
                "invitation.html",
                {
                    "error": "Passwords do not match.",
                    "token": token,
                    "invitation": dict(invitation),
                    "existing_account": False,
                },
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        try:
            context = accept_workspace_invitation(conn, token, password)
        except HTTPException as exc:
            active_invitation = get_active_invitation(conn, token)
            if active_invitation is None:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    {"error": "Invitation is invalid or expired.", "password_auth": True},
                    status_code=exc.status_code,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            return templates.TemplateResponse(
                request,
                "invitation.html",
                {
                    "error": exc.detail,
                    "token": token,
                    "invitation": dict(active_invitation),
                    "existing_account": bool(active_invitation["existing_account"]),
                },
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        session_token = create_session(conn, context.user_id, context.workspace_id)
        record_audit(
            conn,
            "workspace.invitation_accepted",
            "workspace",
            context.workspace_id,
            {"user_id": context.user_id, "email": context.user_email, "role": context.role},
            context.workspace_id,
        )
    response = RedirectResponse("/?notice=Invitation%20accepted", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=session_ttl_days() * 86400,
        httponly=True,
        secure=secure_cookie_enabled(),
        samesite="lax",
    )
    response.delete_cookie("anydatas_user_id")
    response.delete_cookie("anydatas_workspace_id")
    return response


@app.post("/workspace/invitations", response_class=HTMLResponse)
async def create_invitation(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    role: str = Form("viewer"),
) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Invitations require password authentication")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        try:
            invitation_id, token = create_workspace_invitation(
                conn,
                context.workspace_id,
                context.user_id,
                email,
                name,
                role,
            )
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        invitation = conn.execute("SELECT * FROM workspace_invitations WHERE id = ?", (invitation_id,)).fetchone()
        record_audit(
            conn,
            "workspace.invitation_created",
            "workspace",
            context.workspace_id,
            {"invitation_id": invitation_id, "email": invitation["email"], "role": invitation["role"]},
            context.workspace_id,
        )
    invitation_url = f"{str(request.base_url).rstrip('/')}/accept-invitation/{token}"
    return templates.TemplateResponse(
        request,
        "invitation_created.html",
        {"invitation": dict(invitation), "invitation_url": invitation_url},
        status_code=201,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/workspace/invitations/{invitation_id}/revoke")
async def revoke_invitation(request: Request, invitation_id: str) -> RedirectResponse:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Invitation not found")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        invitation = conn.execute(
            "SELECT * FROM workspace_invitations WHERE id = ? AND workspace_id = ?",
            (invitation_id, context.workspace_id),
        ).fetchone()
        if invitation is None:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if invitation["accepted_at"] is not None:
            return redirect_with_notice("Accepted invitations cannot be revoked.")
        conn.execute("UPDATE workspace_invitations SET revoked_at = ? WHERE id = ?", (now_iso(), invitation_id))
        record_audit(
            conn,
            "workspace.invitation_revoked",
            "workspace",
            context.workspace_id,
            {"invitation_id": invitation_id, "email": invitation["email"]},
            context.workspace_id,
        )
    return redirect_with_notice("Invitation revoked.")


@app.post("/workspace/members/{user_id}/password-reset", response_class=HTMLResponse)
async def create_member_password_reset(request: Request, user_id: str) -> Response:
    if not password_auth_enabled():
        raise HTTPException(status_code=404, detail="Workspace member not found")
    if not request.cookies.get(SESSION_COOKIE_NAME) or request.headers.get("authorization"):
        raise HTTPException(status_code=403, detail="A password-authenticated browser session is required")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        member = conn.execute(
            """
            SELECT user.id AS user_id, user.email, user.name, membership.role
            FROM memberships membership
            JOIN users user ON user.id = membership.user_id
            WHERE membership.workspace_id = ? AND user.id = ?
              AND NOT EXISTS (
                SELECT 1 FROM service_accounts service_account
                WHERE service_account.user_id = user.id
              )
            """,
            (context.workspace_id, user_id),
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=404, detail="Workspace member not found")
        if context.role == "admin" and member["role"] in {"owner", "admin"}:
            raise HTTPException(status_code=403, detail="Only an owner can reset an owner or administrator password")
        reset_id, token = create_password_reset_token(
            conn,
            member["user_id"],
            context.workspace_id,
            context.user_id,
        )
        reset = conn.execute("SELECT * FROM password_reset_tokens WHERE id = ?", (reset_id,)).fetchone()
        record_audit(
            conn,
            "user.password_reset_created",
            "user",
            member["user_id"],
            {"reset_id": reset_id, "expires_at": reset["expires_at"]},
            context.workspace_id,
        )
    reset_url = f"{str(request.base_url).rstrip('/')}/reset-password/{quote(token, safe='')}"
    return templates.TemplateResponse(
        request,
        "password_reset_created.html",
        {"member": dict(member), "reset_url": reset_url, "expires_at": reset["expires_at"]},
        status_code=201,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/workspace/members")
async def add_member(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    role: str = Form(...),
) -> RedirectResponse:
    if password_auth_enabled():
        raise HTTPException(status_code=403, detail="Use a password-mode invitation to add members")
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        user_id = add_workspace_member(conn, context.workspace_id, email, name, role)
        record_audit(
            conn,
            "membership.upserted",
            "membership",
            user_id,
            {"email": email.strip().lower(), "role": role},
            context.workspace_id,
        )
    return redirect_with_notice("Member updated.")


@app.post("/workspace/quotas")
async def update_workspace_quotas(
    request: Request,
    max_data_sources: int = Form(...),
    max_projects: int = Form(...),
    max_schedules: int = Form(...),
    max_reports: int = Form(...),
    max_concurrent_runs: int = Form(...),
    max_storage_mb: Optional[int] = Form(None),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        current_quota = get_workspace_quota(conn, context.workspace_id)
        limits = {
            "data_sources": max_data_sources,
            "projects": max_projects,
            "schedules": max_schedules,
            "reports": max_reports,
            "concurrent_runs": max_concurrent_runs,
            "storage_bytes": (
                int(current_quota["max_storage_bytes"])
                if max_storage_mb is None
                else max_storage_mb * MEBIBYTE
            ),
        }
        if any(limit < 0 for limit in limits.values()):
            return redirect_with_notice("Workspace limits must be zero or greater.")
        conn.execute(
            """
            INSERT INTO workspace_quotas (
                workspace_id, max_data_sources, max_projects, max_schedules, max_reports, max_concurrent_runs, max_storage_bytes, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                max_data_sources = excluded.max_data_sources,
                max_projects = excluded.max_projects,
                max_schedules = excluded.max_schedules,
                max_reports = excluded.max_reports,
                max_concurrent_runs = excluded.max_concurrent_runs,
                max_storage_bytes = excluded.max_storage_bytes,
                updated_at = excluded.updated_at
            """,
            (
                context.workspace_id,
                limits["data_sources"],
                limits["projects"],
                limits["schedules"],
                limits["reports"],
                limits["concurrent_runs"],
                limits["storage_bytes"],
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "workspace.quota_updated",
            "workspace",
            context.workspace_id,
            limits,
            context.workspace_id,
        )
    return redirect_with_notice("Workspace limits updated.")


@app.post("/secrets")
async def create_secret_reference(
    request: Request,
    name: str = Form(...),
    environment_variable: str = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        try:
            normalized_name, normalized_environment_variable, normalized_description = parse_secret_reference(
                name,
                environment_variable,
                description,
            )
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        secret_id = uuid.uuid4().hex
        timestamp = now_iso()
        try:
            conn.execute(
                """
                INSERT INTO secret_references (
                    id, workspace_id, name, environment_variable, description,
                    created_by_user_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secret_id,
                    context.workspace_id,
                    normalized_name,
                    normalized_environment_variable,
                    normalized_description,
                    context.user_id,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            return redirect_with_notice("A secret reference with that name or source variable already exists.")
        record_audit(
            conn,
            "secret.reference_created",
            "secret_reference",
            secret_id,
            {"name": normalized_name, "environment_variable": normalized_environment_variable},
            context.workspace_id,
        )
    return redirect_with_notice("Secret reference created.")


@app.post("/secrets/{secret_id}/delete")
async def delete_secret_reference(request: Request, secret_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        reference = conn.execute(
            "SELECT * FROM secret_references WHERE id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()
        if reference is None:
            raise HTTPException(status_code=404, detail="Secret reference not found")
        binding_count = conn.execute(
            "SELECT COUNT(*) AS count FROM project_secret_bindings WHERE secret_id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()["count"]
        if binding_count:
            return redirect_with_notice("Unbind this secret reference from projects before deleting it.")
        binding_snapshot_pattern = f"%{secret_id}%"
        data_source = conn.execute(
            """
            SELECT id
            FROM data_sources
            WHERE workspace_id = ? AND connection_json LIKE ?
            LIMIT 1
            """,
            (context.workspace_id, binding_snapshot_pattern),
        ).fetchone()
        if data_source is not None:
            return redirect_with_notice("This secret reference is still used by a data source.")
        notification_channel = conn.execute(
            """
            SELECT id
            FROM notification_channels
            WHERE workspace_id = ? AND secret_id = ?
            LIMIT 1
            """,
            (context.workspace_id, secret_id),
        ).fetchone()
        if notification_channel is not None:
            return redirect_with_notice("This secret reference is still used by a notification channel.")
        pending_delivery = conn.execute(
            """
            SELECT id
            FROM notification_deliveries
            WHERE workspace_id = ?
              AND secret_id = ?
              AND status IN ('queued', 'sending')
            LIMIT 1
            """,
            (context.workspace_id, secret_id),
        ).fetchone()
        if pending_delivery is not None:
            return redirect_with_notice("Wait for pending notification deliveries before deleting this secret reference.")
        pending_run = conn.execute(
            """
            SELECT r.id
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            WHERE p.workspace_id = ?
              AND r.status IN ('queued', 'running', 'canceling')
              AND r.secret_bindings_json LIKE ?
            LIMIT 1
            """,
            (context.workspace_id, binding_snapshot_pattern),
        ).fetchone()
        if pending_run is not None:
            return redirect_with_notice("Wait for or cancel active runs using this secret reference before deleting it.")
        published_version = conn.execute(
            """
            SELECT p.id
            FROM projects p
            JOIN project_versions version ON version.id = p.published_version_id
            WHERE p.workspace_id = ? AND version.secret_bindings_json LIKE ?
            LIMIT 1
            """,
            (context.workspace_id, binding_snapshot_pattern),
        ).fetchone()
        if published_version is not None:
            return redirect_with_notice("Publish a newer project version without this secret reference before deleting it.")
        conn.execute("DELETE FROM secret_references WHERE id = ?", (secret_id,))
        record_audit(
            conn,
            "secret.reference_deleted",
            "secret_reference",
            secret_id,
            {"name": reference["name"]},
            context.workspace_id,
        )
    return redirect_with_notice("Secret reference deleted.")


@app.post("/notification-channels")
async def create_notification_channel(
    request: Request,
    name: str = Form(...),
    channel_type: str = Form(...),
    destination: str = Form(""),
    secret_id: str = Form(""),
    event_types: list[str] = Form(...),
    max_retries: int = Form(3),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        try:
            normalized_name, normalized_type, normalized_destination, normalized_secret_id, normalized_event_types, normalized_max_retries = (
                parse_notification_channel(name, channel_type, destination, secret_id, event_types, max_retries)
            )
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        if normalized_type in {"webhook", "slack", "teams"}:
            reference = conn.execute(
                "SELECT id FROM secret_references WHERE id = ? AND workspace_id = ?",
                (normalized_secret_id, context.workspace_id),
            ).fetchone()
            if reference is None:
                return redirect_with_notice("Select an available webhook URL Secret Reference.")
        channel_id = uuid.uuid4().hex
        timestamp = now_iso()
        try:
            conn.execute(
                """
                INSERT INTO notification_channels (
                    id, workspace_id, name, channel_type, destination, secret_id,
                    event_types_json, max_retries, is_active, created_by_user_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    channel_id,
                    context.workspace_id,
                    normalized_name,
                    normalized_type,
                    normalized_destination,
                    normalized_secret_id or None,
                    encode_json(normalized_event_types),
                    normalized_max_retries,
                    context.user_id,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            return redirect_with_notice("A notification channel with that name already exists.")
        record_audit(
            conn,
            "notification.channel_created",
            "notification_channel",
            channel_id,
            {
                "name": normalized_name,
                "channel_type": normalized_type,
                "event_types": normalized_event_types,
                "max_retries": normalized_max_retries,
                "recipient_count": len(normalized_destination.split(",")) if normalized_destination else 0,
                "secret_reference_id": normalized_secret_id or None,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Notification channel created.")


@app.post("/notification-channels/{channel_id}/toggle")
async def toggle_notification_channel(request: Request, channel_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        channel = conn.execute(
            "SELECT * FROM notification_channels WHERE id = ? AND workspace_id = ?",
            (channel_id, context.workspace_id),
        ).fetchone()
        if channel is None:
            raise HTTPException(status_code=404, detail="Notification channel not found")
        is_active = 0 if int(channel["is_active"]) else 1
        timestamp = now_iso()
        conn.execute(
            "UPDATE notification_channels SET is_active = ?, updated_at = ? WHERE id = ?",
            (is_active, timestamp, channel_id),
        )
        canceled = 0
        if not is_active:
            canceled = conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'canceled', last_error = 'Notification channel deactivated before delivery.',
                    finished_at = ?, updated_at = ?
                WHERE channel_id = ? AND status = 'queued'
                """,
                (timestamp, timestamp, channel_id),
            ).rowcount
        record_audit(
            conn,
            "notification.channel_enabled" if is_active else "notification.channel_disabled",
            "notification_channel",
            channel_id,
            {"canceled_queued_deliveries": canceled},
            context.workspace_id,
        )
    return redirect_with_notice("Notification channel updated.")


@app.post("/notification-channels/{channel_id}/delete")
async def delete_notification_channel(request: Request, channel_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        channel = conn.execute(
            "SELECT * FROM notification_channels WHERE id = ? AND workspace_id = ?",
            (channel_id, context.workspace_id),
        ).fetchone()
        if channel is None:
            raise HTTPException(status_code=404, detail="Notification channel not found")
        sending = conn.execute(
            "SELECT id FROM notification_deliveries WHERE channel_id = ? AND status = 'sending' LIMIT 1",
            (channel_id,),
        ).fetchone()
        if sending is not None:
            return redirect_with_notice("Wait for the active notification delivery before removing this channel.")
        timestamp = now_iso()
        canceled = conn.execute(
            """
            UPDATE notification_deliveries
            SET status = 'canceled', last_error = 'Notification channel deleted before delivery.',
                finished_at = ?, updated_at = ?
            WHERE channel_id = ? AND status = 'queued'
            """,
            (timestamp, timestamp, channel_id),
        ).rowcount
        conn.execute("DELETE FROM notification_channels WHERE id = ?", (channel_id,))
        record_audit(
            conn,
            "notification.channel_deleted",
            "notification_channel",
            channel_id,
            {"name": channel["name"], "canceled_queued_deliveries": canceled},
            context.workspace_id,
        )
    return redirect_with_notice("Notification channel removed.")


@app.post("/notification-deliveries/{delivery_id}/requeue")
async def requeue_notification_delivery(request: Request, delivery_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "admin")
        delivery = conn.execute(
            """
            SELECT
                delivery.*,
                channel.id AS current_channel_id,
                channel.is_active AS channel_is_active,
                channel.max_retries AS channel_max_retries
            FROM notification_deliveries delivery
            LEFT JOIN notification_channels channel
              ON channel.id = delivery.channel_id
             AND channel.workspace_id = delivery.workspace_id
            WHERE delivery.id = ? AND delivery.workspace_id = ?
            """,
            (delivery_id, context.workspace_id),
        ).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Notification delivery not found")
        if delivery["status"] != "failed":
            return redirect_with_notice("Only failed notification deliveries can be retried.")
        if delivery["current_channel_id"] is None or not int(delivery["channel_is_active"] or 0):
            return redirect_with_notice("Enable the notification channel before retrying this delivery.")
        max_attempts = int(delivery["channel_max_retries"]) + 1
        timestamp = now_iso()
        requeued = conn.execute(
            """
            UPDATE notification_deliveries
            SET status = 'queued', attempt = 0, max_attempts = ?, next_attempt_at = ?,
                last_error = '', finished_at = NULL, updated_at = ?
            WHERE id = ? AND workspace_id = ? AND status = 'failed'
              AND EXISTS (
                  SELECT 1
                  FROM notification_channels channel
                  WHERE channel.id = notification_deliveries.channel_id
                    AND channel.workspace_id = notification_deliveries.workspace_id
                    AND channel.is_active = 1
              )
            """,
            (max_attempts, timestamp, timestamp, delivery_id, context.workspace_id),
        )
        if requeued.rowcount != 1:
            return redirect_with_notice("Notification delivery changed before it could be retried.")
        record_audit(
            conn,
            "notification.delivery_requeued",
            "notification_delivery",
            delivery_id,
            {
                "channel_id": delivery["channel_id"],
                "channel_type": delivery["channel_type"],
                "prior_attempt": delivery["attempt"],
                "max_attempts": max_attempts,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Notification delivery requeued.")


@app.post("/projects/{project_id}/secrets")
async def bind_project_secret(
    request: Request,
    project_id: str,
    secret_id: str = Form(...),
    environment_name: str = Form(...),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        project, _source = require_workspace_project_query_access(conn, context, project_id)
        reference = conn.execute(
            "SELECT * FROM secret_references WHERE id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()
        if reference is None:
            raise HTTPException(status_code=404, detail="Project or secret reference not found")
        try:
            normalized_environment_name = parse_secret_target_environment_name(environment_name)
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        existing_secret = conn.execute(
            "SELECT 1 FROM project_secret_bindings WHERE project_id = ? AND secret_id = ?",
            (project_id, secret_id),
        ).fetchone()
        existing_environment = conn.execute(
            "SELECT 1 FROM project_secret_bindings WHERE project_id = ? AND environment_name = ?",
            (project_id, normalized_environment_name),
        ).fetchone()
        if existing_secret is not None:
            return redirect_with_notice("That secret reference is already bound to the project.")
        if existing_environment is not None:
            return redirect_with_notice("That project environment name is already bound.")
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO project_secret_bindings (
                project_id, secret_id, workspace_id, environment_name, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, secret_id, context.workspace_id, normalized_environment_name, timestamp, timestamp),
        )
        version_id = create_project_version_for_secret_bindings(conn, project, context.workspace_id, timestamp)
        record_audit(
            conn,
            "project.secret_bound",
            "project",
            project_id,
            {
                "secret_id": secret_id,
                "secret_name": reference["name"],
                "environment_name": normalized_environment_name,
                "project_version_id": version_id,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Secret reference bound in a new project version. Publish it before scheduled runs use it.")


@app.post("/projects/{project_id}/secrets/{secret_id}/delete")
async def unbind_project_secret(request: Request, project_id: str, secret_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        project, _source = require_workspace_project_query_access(conn, context, project_id)
        binding = conn.execute(
            """
            SELECT binding.*, reference.name AS secret_name
            FROM project_secret_bindings binding
            JOIN secret_references reference ON reference.id = binding.secret_id
            WHERE binding.project_id = ? AND binding.secret_id = ? AND binding.workspace_id = ?
            """,
            (project_id, secret_id, context.workspace_id),
        ).fetchone()
        if binding is None:
            raise HTTPException(status_code=404, detail="Project secret binding not found")
        conn.execute(
            "DELETE FROM project_secret_bindings WHERE project_id = ? AND secret_id = ?",
            (project_id, secret_id),
        )
        timestamp = now_iso()
        version_id = create_project_version_for_secret_bindings(conn, project, context.workspace_id, timestamp)
        record_audit(
            conn,
            "project.secret_unbound",
            "project",
            project_id,
            {
                "secret_id": secret_id,
                "secret_name": binding["secret_name"],
                "environment_name": binding["environment_name"],
                "project_version_id": version_id,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Secret reference unbound in a new project version. Publish it before scheduled runs use it.")


@app.post("/data-sources")
async def upload_data_source(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    classification: str = Form("internal"),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))

    filename = Path(file.filename or "dataset.csv").name
    extension = Path(filename).suffix.lower()
    if extension not in {".csv", ".parquet", ".xlsx"}:
        return redirect_with_notice("MVP currently accepts CSV, XLSX, or Parquet files.")

    source_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{source_id}_{filename}"
    try:
        copy_upload_with_limit(file, stored_path, max_upload_bytes())
    except ValueError as exc:
        return redirect_with_notice(str(exc))

    try:
        if extension == ".parquet":
            source_type = "parquet"
            dataset_path = stored_path
            connection = {}
            columns, preview, row_count, quality = inspect_parquet(stored_path)
        elif extension == ".xlsx":
            source_type = "xlsx"
            columns, preview, row_count, quality, sheet_name, rows = inspect_xlsx(stored_path)
            dataset_path = UPLOAD_DIR / f"{source_id}_{Path(filename).stem}.csv"
            write_rows_csv(dataset_path, columns, rows)
            connection = {"sheet": sheet_name, "original_path": str(stored_path)}
        else:
            source_type = "file"
            dataset_path = stored_path
            connection = {}
            columns, preview, row_count, quality = inspect_csv(stored_path)
    except UnicodeDecodeError:
        stored_path.unlink(missing_ok=True)
        return redirect_with_notice("CSV must be UTF-8 encoded.")
    except Exception as exc:  # noqa: BLE001
        stored_path.unlink(missing_ok=True)
        return redirect_with_notice(f"Data source inspection failed: {exc}")

    with connect() as conn:
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
            quota = get_workspace_quota(conn, context.workspace_id)
            ensure_workspace_storage_capacity(
                workspace_storage_bytes(conn, context.workspace_id),
                paths_storage_bytes([stored_path, dataset_path]),
                int(quota["max_storage_bytes"]),
            )
        except ValueError as exc:
            stored_path.unlink(missing_ok=True)
            if dataset_path != stored_path:
                dataset_path.unlink(missing_ok=True)
            return redirect_with_notice(str(exc))
        conn.execute(
            """
            INSERT INTO data_sources (id, workspace_id, created_by_user_id, visibility, classification, source_type, name, filename, path, columns_json, column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at)
            VALUES (?, ?, ?, 'workspace', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                context.workspace_id,
                context.user_id,
                normalized_classification,
                source_type,
                name.strip() or filename,
                filename,
                str(dataset_path),
                encode_json(columns),
                encode_json(build_column_metadata(columns, preview)),
                encode_json(preview),
                row_count,
                encode_json(quality),
                encode_json(connection),
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "data_source.created",
            "data_source",
            source_id,
            {
                "name": name.strip() or filename,
                "filename": filename,
                "row_count": row_count,
                "classification": normalized_classification,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Data source uploaded.")


@app.post("/data-sources/s3")
async def create_s3_data_source(
    request: Request,
    name: str = Form(...),
    secret_id: str = Form(...),
    bucket_name: str = Form(...),
    object_key: str = Form(...),
    classification: str = Form("internal"),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
        bucket = parse_s3_bucket(bucket_name)
        key = parse_s3_object_key(object_key)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        reference = conn.execute(
            "SELECT id, name FROM secret_references WHERE id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()
    if reference is None:
        return redirect_with_notice("Select an available S3 connection reference.")

    source_id = uuid.uuid4().hex
    secret_value = ""
    try:
        with connect() as conn:
            secret_value, _resolved_reference = resolve_secret_reference_value(conn, context.workspace_id, secret_id)
        snapshot = import_s3_snapshot(
            source_id,
            secret_value,
            bucket,
            key,
            max_upload_bytes(),
        )
    except UnicodeDecodeError:
        return redirect_with_notice("S3 CSV objects must be UTF-8 encoded.")
    except Exception as exc:  # noqa: BLE001
        message = redact_text(str(exc), s3_secret_redaction_values(secret_value))
        return redirect_with_notice(f"S3 import failed: {message}")

    connection = {
        "driver": "s3",
        "secret_id": secret_id,
        "bucket": bucket,
        "object_key": key,
        "runtime_format": snapshot.runtime_format,
        **snapshot.object_metadata,
        **snapshot.format_metadata,
    }
    with connect() as conn:
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
            quota = get_workspace_quota(conn, context.workspace_id)
            ensure_workspace_storage_capacity(
                workspace_storage_bytes(conn, context.workspace_id),
                snapshot.size_bytes,
                int(quota["max_storage_bytes"]),
            )
        except ValueError as exc:
            snapshot.remove_files()
            return redirect_with_notice(str(exc))
        conn.execute(
            """
            INSERT INTO data_sources (id, workspace_id, created_by_user_id, visibility, classification, source_type, name, filename, path, columns_json, column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at)
            VALUES (?, ?, ?, 'workspace', ?, 's3', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                context.workspace_id,
                context.user_id,
                normalized_classification,
                name.strip() or snapshot.filename,
                snapshot.filename,
                str(snapshot.dataset_path),
                encode_json(snapshot.columns),
                encode_json(build_column_metadata(snapshot.columns, snapshot.preview)),
                encode_json(snapshot.preview),
                snapshot.row_count,
                encode_json(snapshot.quality),
                encode_json(connection),
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "data_source.created",
            "data_source",
            source_id,
            {
                "name": name.strip() or snapshot.filename,
                "driver": "s3",
                "bucket": bucket,
                "object_key": key,
                "size_bytes": snapshot.object_metadata["size_bytes"],
                "etag": snapshot.object_metadata["etag"],
                "version_id": snapshot.object_metadata["version_id"],
                "row_count": snapshot.row_count,
                "secret_reference_id": secret_id,
                "classification": normalized_classification,
            },
            context.workspace_id,
        )
    return redirect_with_notice("S3 object imported as a snapshot data source.")
@app.post("/data-sources/sqlite")
async def create_sqlite_data_source(
    request: Request,
    name: str = Form(...),
    database_path: str = Form(...),
    table_name: str = Form(...),
    classification: str = Form("internal"),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))

    db_path = Path(database_path).expanduser().resolve()
    table = table_name.strip()
    if not table:
        return redirect_with_notice("Table name is required.")

    try:
        columns, preview, row_count, quality = inspect_sqlite_table(db_path, table)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        return redirect_with_notice(f"SQLite connection failed: {exc}")

    source_id = uuid.uuid4().hex
    with connect() as conn:
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        conn.execute(
            """
            INSERT INTO data_sources (id, workspace_id, created_by_user_id, visibility, classification, source_type, name, filename, path, columns_json, column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at)
            VALUES (?, ?, ?, 'workspace', ?, 'sqlite', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                context.workspace_id,
                context.user_id,
                normalized_classification,
                name.strip() or table,
                db_path.name,
                str(db_path),
                encode_json(columns),
                encode_json(build_column_metadata(columns, preview)),
                encode_json(preview),
                row_count,
                encode_json(quality),
                encode_json({"driver": "sqlite", "table": table}),
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "data_source.created",
            "data_source",
            source_id,
            {
                "name": name.strip() or table,
                "driver": "sqlite",
                "table": table,
                "row_count": row_count,
                "classification": normalized_classification,
            },
            context.workspace_id,
        )
    return redirect_with_notice("SQLite data source connected.")


@app.post("/data-sources/postgres")
async def create_postgres_data_source(
    request: Request,
    name: str = Form(...),
    secret_id: str = Form(...),
    schema_name: str = Form("public"),
    table_name: str = Form(...),
    classification: str = Form("internal"),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        reference = conn.execute(
            "SELECT id, name FROM secret_references WHERE id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()
    if reference is None:
        return redirect_with_notice("Select an available PostgreSQL connection reference.")

    connection_url = ""
    try:
        schema = parse_postgres_identifier(schema_name, "schema")
        table = parse_postgres_identifier(table_name, "table")
        with connect() as conn:
            connection_url, _resolved_reference = resolve_secret_reference_value(conn, context.workspace_id, secret_id)
        connection_url = parse_postgres_connection_url(connection_url)
        columns, preview, row_count, quality = inspect_postgres_table(connection_url, schema, table)
    except Exception as exc:  # noqa: BLE001
        message = redact_text(str(exc), [connection_url])
        return redirect_with_notice(f"PostgreSQL connection failed: {message}")

    source_id = uuid.uuid4().hex
    connection = {
        "driver": "postgres",
        "secret_id": secret_id,
        "schema": schema,
        "table": table,
        "url_environment": data_source_secret_environment_name(source_id),
    }
    with connect() as conn:
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        conn.execute(
            """
            INSERT INTO data_sources (id, workspace_id, created_by_user_id, visibility, classification, source_type, name, filename, path, columns_json, column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at)
            VALUES (?, ?, ?, 'workspace', ?, 'postgres', ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                context.workspace_id,
                context.user_id,
                normalized_classification,
                name.strip() or f"{schema}.{table}",
                f"{schema}.{table}",
                encode_json(columns),
                encode_json(build_column_metadata(columns, preview)),
                encode_json(preview),
                row_count,
                encode_json(quality),
                encode_json(connection),
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "data_source.created",
            "data_source",
            source_id,
            {
                "name": name.strip() or f"{schema}.{table}",
                "driver": "postgres",
                "schema": schema,
                "table": table,
                "row_count": row_count,
                "secret_reference_id": secret_id,
                "classification": normalized_classification,
            },
            context.workspace_id,
        )
    return redirect_with_notice("PostgreSQL data source connected.")


@app.post("/data-sources/mysql")
async def create_mysql_data_source(
    request: Request,
    name: str = Form(...),
    secret_id: str = Form(...),
    database_name: str = Form(...),
    table_name: str = Form(...),
    classification: str = Form("internal"),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        reference = conn.execute(
            "SELECT id, name FROM secret_references WHERE id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()
    if reference is None:
        return redirect_with_notice("Select an available MySQL connection reference.")

    connection_url = ""
    try:
        database = parse_mysql_identifier(database_name, "database")
        table = parse_mysql_identifier(table_name, "table")
        with connect() as conn:
            connection_url, _resolved_reference = resolve_secret_reference_value(conn, context.workspace_id, secret_id)
        connection_url = parse_mysql_connection_url(connection_url)
        columns, preview, row_count, quality = inspect_mysql_table(connection_url, database, table)
    except Exception as exc:  # noqa: BLE001
        message = redact_text(str(exc), [connection_url])
        return redirect_with_notice(f"MySQL connection failed: {message}")

    source_id = uuid.uuid4().hex
    connection = {
        "driver": "mysql",
        "secret_id": secret_id,
        "database": database,
        "table": table,
        "url_environment": data_source_secret_environment_name(source_id),
    }
    with connect() as conn:
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        conn.execute(
            """
            INSERT INTO data_sources (id, workspace_id, created_by_user_id, visibility, classification, source_type, name, filename, path, columns_json, column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at)
            VALUES (?, ?, ?, 'workspace', ?, 'mysql', ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                context.workspace_id,
                context.user_id,
                normalized_classification,
                name.strip() or f"{database}.{table}",
                f"{database}.{table}",
                encode_json(columns),
                encode_json(build_column_metadata(columns, preview)),
                encode_json(preview),
                row_count,
                encode_json(quality),
                encode_json(connection),
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "data_source.created",
            "data_source",
            source_id,
            {
                "name": name.strip() or f"{database}.{table}",
                "driver": "mysql",
                "database": database,
                "table": table,
                "row_count": row_count,
                "secret_reference_id": secret_id,
                "classification": normalized_classification,
            },
            context.workspace_id,
        )
    return redirect_with_notice("MySQL data source connected.")


@app.post("/data-sources/clickhouse")
async def create_clickhouse_data_source(
    request: Request,
    name: str = Form(...),
    secret_id: str = Form(...),
    database_name: str = Form(...),
    table_name: str = Form(...),
    classification: str = Form("internal"),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        reference = conn.execute(
            "SELECT id, name FROM secret_references WHERE id = ? AND workspace_id = ?",
            (secret_id, context.workspace_id),
        ).fetchone()
    if reference is None:
        return redirect_with_notice("Select an available ClickHouse connection reference.")

    connection_url = ""
    try:
        database = parse_clickhouse_identifier(database_name, "database")
        table = parse_clickhouse_identifier(table_name, "table")
        with connect() as conn:
            connection_url, _resolved_reference = resolve_secret_reference_value(conn, context.workspace_id, secret_id)
        connection_url = parse_clickhouse_connection_url(connection_url)
        columns, preview, row_count, quality = inspect_clickhouse_table(connection_url, database, table)
    except Exception as exc:  # noqa: BLE001
        message = redact_text(str(exc), [connection_url])
        return redirect_with_notice(f"ClickHouse connection failed: {message}")

    source_id = uuid.uuid4().hex
    connection = {
        "driver": "clickhouse",
        "secret_id": secret_id,
        "database": database,
        "table": table,
        "url_environment": data_source_secret_environment_name(source_id),
    }
    with connect() as conn:
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "data_sources")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        conn.execute(
            """
            INSERT INTO data_sources (id, workspace_id, created_by_user_id, visibility, classification, source_type, name, filename, path, columns_json, column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at)
            VALUES (?, ?, ?, 'workspace', ?, 'clickhouse', ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                context.workspace_id,
                context.user_id,
                normalized_classification,
                name.strip() or f"{database}.{table}",
                f"{database}.{table}",
                encode_json(columns),
                encode_json(build_column_metadata(columns, preview)),
                encode_json(preview),
                row_count,
                encode_json(quality),
                encode_json(connection),
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "data_source.created",
            "data_source",
            source_id,
            {
                "name": name.strip() or f"{database}.{table}",
                "driver": "clickhouse",
                "database": database,
                "table": table,
                "row_count": row_count,
                "secret_reference_id": secret_id,
                "classification": normalized_classification,
            },
            context.workspace_id,
        )
    return redirect_with_notice("ClickHouse data source connected.")


@app.get("/data-sources/{source_id}", response_class=HTMLResponse)
async def view_data_source(request: Request, source_id: str, notice: Optional[str] = None) -> HTMLResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        source = require_workspace_data_source_access(conn, context, source_id, "view")
        can_manage = can_manage_data_source(conn, context, source)
        impact = data_source_impact(conn, context.workspace_id, source_id) if can_manage else None
        if impact is not None:
            impact["reports"] = [report for report in impact["reports"] if can_view_report(conn, context, report)]
            for project in impact["projects"]:
                project["report_count"] = sum(
                    1 for report in impact["reports"] if report["project_id"] == project["id"]
                )
        source_grantees = get_data_source_grantees(conn, context.workspace_id, source_id) if can_manage else []
        grantable_members = [
            member
            for member in get_workspace_members(conn, context.workspace_id)
            if member["user_id"] != source["created_by_user_id"] and member["role"] not in {"owner", "admin"}
        ] if can_manage else []
        source_payload = dict(source)
        source_payload["access_level"] = data_source_access_level(conn, context, source)
        source_connection = decode_json(source["connection_json"], {})
        s3_connection = (
            {
                key: source_connection.get(key, "")
                for key in (
                    "bucket",
                    "object_key",
                    "runtime_format",
                    "size_bytes",
                    "etag",
                    "version_id",
                    "last_modified",
                    "refreshed_at",
                )
            }
            if source["source_type"] == "s3" and isinstance(source_connection, dict)
            else None
        )
    fields, columns, preview, _metadata = build_data_source_schema_fields(source)
    quality = decode_json(source["quality_json"], {})
    return templates.TemplateResponse(
        request,
        "data_source.html",
        {
            **shell_context(context, "data"),
            "notice": notice,
            "source": source_payload,
            "s3_connection": s3_connection,
            "fields": fields,
            "columns": columns,
            "preview": preview,
            "quality": quality if isinstance(quality, dict) else {},
            "logical_types": LOGICAL_TYPES,
            "field_classifications": FIELD_CLASSIFICATIONS,
            "masking_policies": MASKING_POLICIES,
            "can_edit": context.role in {"owner", "admin", "analyst"} and can_manage,
            "can_manage": can_manage,
            "impact": impact,
            "source_grantees": source_grantees,
            "grantable_members": grantable_members,
            "data_source_permissions": ("view", "query", "manage"),
            "data_source_classifications": DATA_SOURCE_CLASSIFICATIONS,
        },
    )


@app.post("/data-sources/{source_id}/refresh-s3")
async def refresh_s3_data_source(request: Request, source_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        source = require_workspace_data_source_access(conn, context, source_id, "manage")
        if source["source_type"] != "s3":
            return redirect_to_data_source(source_id, "Only S3 snapshot data sources can be refreshed here.")
        connection = decode_json(source["connection_json"], {})
        if not isinstance(connection, dict) or connection.get("driver") != "s3":
            return redirect_to_data_source(source_id, "S3 data source metadata is invalid.")
        secret_id = connection.get("secret_id")
        bucket = connection.get("bucket")
        object_key = connection.get("object_key")
        if not all(isinstance(value, str) and value for value in (secret_id, bucket, object_key)):
            return redirect_to_data_source(source_id, "S3 data source metadata is incomplete.")
        try:
            secret_value, _reference = resolve_secret_reference_value(conn, context.workspace_id, secret_id)
        except Exception as exc:  # noqa: BLE001
            return redirect_to_data_source(source_id, f"S3 refresh failed: {exc}")
        quota = get_workspace_quota(conn, context.workspace_id)
        available_storage_bytes = max(
            int(quota["max_storage_bytes"])
            - workspace_storage_bytes(conn, context.workspace_id, exclude_source_id=source_id),
            0,
        )

    secret_redactions = s3_secret_redaction_values(secret_value)
    try:
        snapshot = refresh_s3_snapshot(
            source,
            connection,
            secret_value,
            max_upload_bytes(),
            available_storage_bytes,
        )
        refreshed_at = now_iso()
        updated_connection = {
            **connection,
            "runtime_format": snapshot.runtime_format,
            **snapshot.object_metadata,
            **snapshot.format_metadata,
            "refreshed_at": refreshed_at,
        }
        existing_metadata = decode_json(source["column_metadata_json"], {})
        with connect() as conn:
            current_source = require_workspace_data_source_access(conn, context, source_id, "manage")
            if current_source["source_type"] != "s3":
                raise ValueError("Data source type changed during refresh.")
            conn.execute(
                """
                UPDATE data_sources
                SET columns_json = ?, column_metadata_json = ?, preview_json = ?, row_count = ?, quality_json = ?, connection_json = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (
                    encode_json(snapshot.columns),
                    encode_json(build_column_metadata(snapshot.columns, snapshot.preview, existing_metadata)),
                    encode_json(snapshot.preview),
                    snapshot.row_count,
                    encode_json(snapshot.quality),
                    encode_json(updated_connection),
                    source_id,
                    context.workspace_id,
                ),
            )
            record_audit(
                conn,
                "data_source.s3_refreshed",
                "data_source",
                source_id,
                {
                    "bucket": bucket,
                    "object_key": object_key,
                    "previous_etag": connection.get("etag", ""),
                    "etag": snapshot.object_metadata["etag"],
                    "version_id": snapshot.object_metadata["version_id"],
                    "size_bytes": snapshot.object_metadata["size_bytes"],
                    "row_count": snapshot.row_count,
                },
                context.workspace_id,
            )
    except UnicodeDecodeError:
        return redirect_to_data_source(source_id, "S3 CSV objects must be UTF-8 encoded.")
    except Exception as exc:  # noqa: BLE001
        message = redact_text(str(exc), secret_redactions)
        return redirect_to_data_source(source_id, f"S3 refresh failed: {message}")
    return redirect_to_data_source(source_id, "S3 snapshot refreshed.")


@app.post("/data-sources/{source_id}/schema")
async def update_data_source_schema(
    request: Request,
    source_id: str,
    field_names: list[str] = Form(...),
    field_types: list[str] = Form(...),
    descriptions: list[str] = Form(...),
    field_classifications: list[str] = Form([]),
    masking_policies: list[str] = Form([]),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        source = require_workspace_data_source_access(conn, context, source_id, "manage")
        _fields, columns, _preview, existing_metadata = build_data_source_schema_fields(source)
        if not field_classifications:
            field_classifications = [existing_metadata[column]["classification"] for column in columns]
        if not masking_policies:
            masking_policies = [existing_metadata[column]["masking"] for column in columns]
        if field_names != columns or len(field_types) != len(columns) or len(descriptions) != len(columns):
            return redirect_to_data_source(source_id, "Schema fields no longer match this data source.")
        if len(field_classifications) != len(columns) or len(masking_policies) != len(columns):
            return redirect_to_data_source(source_id, "Field governance settings no longer match this data source.")
        if any(field_type not in LOGICAL_TYPES for field_type in field_types):
            return redirect_to_data_source(source_id, "Unsupported field type.")
        if any(len(description.strip()) > 500 for description in descriptions):
            return redirect_to_data_source(source_id, "Field descriptions must be 500 characters or fewer.")
        if any(classification not in FIELD_CLASSIFICATIONS for classification in field_classifications):
            return redirect_to_data_source(source_id, "Unsupported field classification.")
        if any(policy not in MASKING_POLICIES for policy in masking_policies):
            return redirect_to_data_source(source_id, "Unsupported field masking policy.")
        metadata = {
            column: {
                "type": field_type,
                "description": description.strip(),
                "classification": classification,
                "masking": masking,
            }
            for column, field_type, description, classification, masking in zip(
                columns,
                field_types,
                descriptions,
                field_classifications,
                masking_policies,
            )
        }
        conn.execute(
            "UPDATE data_sources SET column_metadata_json = ? WHERE id = ?",
            (encode_json(metadata), source_id),
        )
        record_audit(
            conn,
            "data_source.schema_updated",
            "data_source",
            source_id,
            {
                "columns": columns,
                "classified_columns": [
                    column for column, classification in zip(columns, field_classifications) if classification != "none"
                ],
                "masked_export_columns": [
                    column for column, policy in zip(columns, masking_policies) if policy != "none"
                ],
            },
            context.workspace_id,
        )
    return redirect_to_data_source(source_id, "Schema updated.")


@app.post("/data-sources/{source_id}/visibility")
async def update_data_source_visibility(
    request: Request,
    source_id: str,
    visibility: str = Form(...),
) -> RedirectResponse:
    normalized_visibility = visibility.strip().lower()
    if normalized_visibility not in DATA_SOURCE_VISIBILITIES:
        return redirect_to_data_source(source_id, "Data source visibility must be workspace or private.")
    with connect() as conn:
        context = get_request_context(request, conn)
        source = require_workspace_data_source_access(conn, context, source_id, "manage")
        claimed_legacy_creator = source["created_by_user_id"] is None
        if claimed_legacy_creator:
            conn.execute(
                "UPDATE data_sources SET created_by_user_id = ? WHERE id = ?",
                (context.user_id, source_id),
            )
        conn.execute(
            "UPDATE data_sources SET visibility = ? WHERE id = ?",
            (normalized_visibility, source_id),
        )
        updated_source = get_workspace_data_source(conn, context.workspace_id, source_id)
        removed_subscriptions = prune_ineligible_data_source_subscriptions(conn, updated_source)
        record_audit(
            conn,
            "data_source.visibility_updated",
            "data_source",
            source_id,
            {
                "visibility": normalized_visibility,
                "claimed_legacy_creator": claimed_legacy_creator,
                "removed_subscriptions": removed_subscriptions,
            },
            context.workspace_id,
        )
    return redirect_to_data_source(source_id, "Data source visibility updated.")


@app.post("/data-sources/{source_id}/classification")
async def update_data_source_classification(
    request: Request,
    source_id: str,
    classification: str = Form(...),
) -> RedirectResponse:
    try:
        normalized_classification = parse_data_source_classification(classification)
    except ValueError as exc:
        return redirect_to_data_source(source_id, str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        source = require_workspace_data_source_access(conn, context, source_id, "manage")
        previous_classification = source["classification"] or "internal"
        conn.execute("UPDATE data_sources SET classification = ? WHERE id = ?", (normalized_classification, source_id))
        record_audit(
            conn,
            "data_source.classification_updated",
            "data_source",
            source_id,
            {"previous": previous_classification, "classification": normalized_classification},
            context.workspace_id,
        )
    return redirect_to_data_source(source_id, "Data classification updated.")


@app.post("/data-sources/{source_id}/grants")
async def grant_data_source_access(
    request: Request,
    source_id: str,
    user_id: str = Form(...),
    permission: str = Form(...),
) -> RedirectResponse:
    normalized_permission = permission.strip().lower()
    if normalized_permission not in DATA_SOURCE_PERMISSIONS:
        return redirect_to_data_source(source_id, "Unsupported data source permission.")
    with connect() as conn:
        context = get_request_context(request, conn)
        source = require_workspace_data_source_access(conn, context, source_id, "manage")
        if source["visibility"] != "private":
            return redirect_to_data_source(source_id, "Set the data source to private before granting member access.")
        member = conn.execute(
            """
            SELECT u.id, u.email, m.role
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.workspace_id = ? AND u.id = ?
            """,
            (context.workspace_id, user_id),
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=404, detail="Workspace member not found")
        if member["id"] == source["created_by_user_id"] or member["role"] in {"owner", "admin"}:
            return redirect_to_data_source(source_id, "That member already has data source access.")
        if member["role"] == "viewer" and normalized_permission != "view":
            return redirect_to_data_source(source_id, "Viewer members can receive view access only.")
        existing = conn.execute(
            """
            SELECT permission
            FROM data_source_access_grants
            WHERE data_source_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (source_id, context.workspace_id, member["id"]),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO data_source_access_grants (
                data_source_id, user_id, workspace_id, permission, granted_by_user_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(data_source_id, user_id) DO UPDATE SET
                permission = excluded.permission,
                granted_by_user_id = excluded.granted_by_user_id,
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                member["id"],
                context.workspace_id,
                normalized_permission,
                context.user_id,
                now_iso(),
                now_iso(),
            ),
        )
        updated_source = get_workspace_data_source(conn, context.workspace_id, source_id)
        removed_subscriptions = prune_ineligible_data_source_subscriptions(conn, updated_source)
        record_audit(
            conn,
            "data_source.access_granted" if existing is None else "data_source.access_updated",
            "data_source",
            source_id,
            {
                "user_id": member["id"],
                "email": member["email"],
                "permission": normalized_permission,
                "removed_subscriptions": removed_subscriptions,
            },
            context.workspace_id,
        )
    return redirect_to_data_source(source_id, "Member access saved.")


@app.post("/data-sources/{source_id}/grants/{user_id}/delete")
async def revoke_data_source_access(request: Request, source_id: str, user_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        source = require_workspace_data_source_access(conn, context, source_id, "manage")
        deleted = conn.execute(
            """
            DELETE FROM data_source_access_grants
            WHERE data_source_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (source_id, context.workspace_id, user_id),
        )
        if deleted.rowcount:
            removed_subscriptions = prune_ineligible_data_source_subscriptions(conn, source)
            record_audit(
                conn,
                "data_source.access_revoked",
                "data_source",
                source_id,
                {"user_id": user_id, "removed_subscriptions": removed_subscriptions},
                context.workspace_id,
            )
            notice = "Member access revoked."
        else:
            notice = "That member does not have explicit data source access."
    return redirect_to_data_source(source_id, notice)


@app.post("/projects")
async def create_project(
    request: Request,
    name: str = Form(...),
    language: str = Form(...),
    data_source_id: str = Form(...),
    script: str = Form(...),
    parameters_json: str = Form(DEFAULT_PARAMETERS_JSON),
    runtime_profile: str = Form("standard"),
) -> RedirectResponse:
    if language not in {"sql", "python"}:
        raise HTTPException(status_code=400, detail="Unsupported language")
    try:
        parameters, normalized_parameters_json = parse_project_parameters(parameters_json)
        normalized_runtime_profile = normalize_runtime_profile(runtime_profile)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    project_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "projects")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        try:
            require_workspace_data_source_access(conn, context, data_source_id, "query")
        except HTTPException as exc:
            if exc.status_code == 404:
                return redirect_with_notice("Select a data source before creating a project.")
            raise
        conn.execute(
            """
            INSERT INTO projects (id, workspace_id, name, language, script, parameters_json, runtime_profile, data_source_id, published_version_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                project_id,
                context.workspace_id,
                name.strip() or "Untitled project",
                language,
                script,
                normalized_parameters_json,
                normalized_runtime_profile,
                data_source_id,
                timestamp,
                timestamp,
            ),
        )
        version_id = create_project_version(
            conn,
            project_id,
            language,
            script,
            normalized_parameters_json,
            normalized_runtime_profile,
            data_source_id,
            timestamp,
        )
        conn.execute("UPDATE projects SET published_version_id = ? WHERE id = ?", (version_id, project_id))
        record_audit(
            conn,
            "project.created",
            "project",
            project_id,
            {
                "name": name.strip() or "Untitled project",
                "language": language,
                "runtime_profile": normalized_runtime_profile,
                "parameter_names": sorted(parameters),
            },
            context.workspace_id,
        )
    return redirect_with_notice("Project created.")


@app.post("/projects/{project_id}")
async def update_project(
    request: Request,
    project_id: str,
    name: str = Form(...),
    language: str = Form(...),
    data_source_id: str = Form(...),
    script: str = Form(...),
    parameters_json: str = Form(DEFAULT_PARAMETERS_JSON),
    runtime_profile: str = Form("standard"),
) -> RedirectResponse:
    if language not in {"sql", "python"}:
        raise HTTPException(status_code=400, detail="Unsupported language")
    try:
        parameters, normalized_parameters_json = parse_project_parameters(parameters_json)
        normalized_runtime_profile = normalize_runtime_profile(runtime_profile)
    except ValueError as exc:
        return redirect_with_notice(str(exc))
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        require_workspace_project_query_access(conn, context, project_id)
        require_workspace_data_source_access(conn, context, data_source_id, "query")
        conn.execute(
            """
            UPDATE projects
            SET name = ?, language = ?, data_source_id = ?, script = ?, parameters_json = ?, runtime_profile = ?, updated_at = ?
            WHERE id = ? AND workspace_id = ?
            """,
            (
                name.strip() or "Untitled project",
                language,
                data_source_id,
                script,
                normalized_parameters_json,
                normalized_runtime_profile,
                now_iso(),
                project_id,
                context.workspace_id,
            ),
        )
        secret_bindings_json = encode_json(get_project_secret_binding_snapshot(conn, context.workspace_id, project_id))
        version_id = create_project_version(
            conn,
            project_id,
            language,
            script,
            normalized_parameters_json,
            normalized_runtime_profile,
            data_source_id,
            now_iso(),
            secret_bindings_json,
        )
        record_audit(
            conn,
            "project.version_saved",
            "project",
            project_id,
            {
                "name": name.strip() or "Untitled project",
                "language": language,
                "runtime_profile": normalized_runtime_profile,
                "parameter_names": sorted(parameters),
                "project_version_id": version_id,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Project version saved. Publish it before scheduled runs use it.")


@app.post("/projects/{project_id}/publish")
async def publish_project(request: Request, project_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        project, _source = require_workspace_project_query_access(conn, context, project_id)
        version = conn.execute(
            """
            SELECT id, version_number
            FROM project_versions
            WHERE project_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if version is None:
            return redirect_with_notice("Project has no saved version to publish.")
        conn.execute("UPDATE projects SET published_version_id = ?, updated_at = ? WHERE id = ?", (version["id"], now_iso(), project_id))
        record_audit(
            conn,
            "project.published",
            "project",
            project_id,
            {"project_version_id": version["id"], "version_number": version["version_number"]},
            context.workspace_id,
        )
    return redirect_with_notice(f"Published project version v{version['version_number']}.")


@app.post("/projects/{project_id}/run")
async def run_project_now(request: Request, project_id: str, background_tasks: BackgroundTasks) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        require_workspace_project_query_access(conn, context, project_id)
    run_id = await asyncio.to_thread(create_run, project_id, "manual")
    background_tasks.add_task(execute_run, run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/schedules")
async def create_schedule(
    request: Request,
    project_id: str = Form(...),
    name: str = Form(...),
    schedule_type: str = Form("interval"),
    interval_minutes: int = Form(60),
    cron_expression: str = Form(""),
    timezone_name: str = Form("UTC"),
    max_retries: int = Form(0),
    retry_delay_minutes: int = Form(5),
    concurrency_policy: str = Form("skip"),
) -> RedirectResponse:
    normalized_type = schedule_type.strip().lower()
    normalized_concurrency_policy = concurrency_policy.strip().lower()
    if normalized_type not in {"interval", "cron"}:
        return redirect_with_notice("Schedule type must be interval or cron.")
    if normalized_concurrency_policy not in SCHEDULE_CONCURRENCY_POLICIES:
        return redirect_with_notice("Concurrency policy must be skip, queue_one, queue_all, or cancel_previous.")
    normalized_timezone = timezone_name.strip() or "UTC"
    normalized_cron = cron_expression.strip()
    if max_retries < 0 or max_retries > 10:
        return redirect_with_notice("Retries must be between 0 and 10.")
    if retry_delay_minutes < 1 or retry_delay_minutes > 1440:
        return redirect_with_notice("Retry delay must be between 1 and 1440 minutes.")
    if normalized_type == "interval":
        if interval_minutes < 1:
            return redirect_with_notice("Interval must be at least 1 minute.")
        next_run_at = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
        normalized_cron = ""
        normalized_timezone = "UTC"
    else:
        try:
            next_run_at = next_cron_run(normalized_cron, datetime.now(timezone.utc), normalized_timezone)
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        interval_minutes = 0
    schedule_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "schedules")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        require_workspace_project_query_access(conn, context, project_id)
        conn.execute(
            """
            INSERT INTO schedules (id, project_id, name, schedule_type, interval_minutes, cron_expression, timezone, max_retries, retry_delay_minutes, concurrency_policy, is_active, next_run_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                schedule_id,
                project_id,
                name.strip() or "Scheduled run",
                normalized_type,
                interval_minutes,
                normalized_cron,
                normalized_timezone,
                max_retries,
                retry_delay_minutes,
                normalized_concurrency_policy,
                next_run_at,
                timestamp,
            ),
        )
        record_audit(
            conn,
            "schedule.created",
            "schedule",
            schedule_id,
            {
                "project_id": project_id,
                "schedule_type": normalized_type,
                "interval_minutes": interval_minutes,
                "cron_expression": normalized_cron,
                "timezone": normalized_timezone,
                "max_retries": max_retries,
                "retry_delay_minutes": retry_delay_minutes,
                "concurrency_policy": normalized_concurrency_policy,
            },
            context.workspace_id,
        )
    return redirect_with_notice("Schedule created.")


@app.post("/schedules/{schedule_id}/toggle")
async def toggle_schedule(request: Request, schedule_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        schedule = conn.execute(
            """
            SELECT s.*
            FROM schedules s
            JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.workspace_id = ?
            """,
            (schedule_id, context.workspace_id),
        ).fetchone()
        if schedule is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        require_workspace_project_query_access(conn, context, schedule["project_id"])
        is_active = 0 if schedule["is_active"] else 1
        next_run_at = schedule_next_run_at(schedule, datetime.now(timezone.utc)) if is_active else schedule["next_run_at"]
        conn.execute(
            "UPDATE schedules SET is_active = ?, next_run_at = ? WHERE id = ?",
            (is_active, next_run_at, schedule_id),
        )
        record_audit(conn, "schedule.resumed" if is_active else "schedule.paused", "schedule", schedule_id, {"project_id": schedule["project_id"]}, context.workspace_id)
    return redirect_with_notice("Schedule updated.")


@app.post("/schedules/{schedule_id}/run")
async def run_schedule_now(request: Request, schedule_id: str, background_tasks: BackgroundTasks) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        schedule = conn.execute(
            """
            SELECT s.*
            FROM schedules s
            JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.workspace_id = ?
            """,
            (schedule_id, context.workspace_id),
        ).fetchone()
        if schedule is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        require_workspace_project_query_access(conn, context, schedule["project_id"])
    run_id = await asyncio.to_thread(create_run, schedule["project_id"], "schedule_manual", schedule_id)
    background_tasks.add_task(execute_run, run_id)
    with connect() as conn:
        conn.execute("UPDATE schedules SET last_run_at = ? WHERE id = ?", (now_iso(), schedule_id))
        record_audit(conn, "schedule.manual_run", "schedule", schedule_id, {"run_id": run_id, "project_id": schedule["project_id"]}, context.workspace_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/schedules/{schedule_id}/backfill", response_class=HTMLResponse)
async def view_schedule_backfill(request: Request, schedule_id: str, notice: Optional[str] = None) -> HTMLResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        schedule = conn.execute(
            """
            SELECT s.*, p.name AS project_name, p.workspace_id, p.parameters_json AS project_parameters_json
            FROM schedules s
            JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.workspace_id = ?
            """,
            (schedule_id, context.workspace_id),
        ).fetchone()
        if schedule is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        require_workspace_project_query_access(conn, context, schedule["project_id"])
    try:
        schedule_timezone = ZoneInfo(schedule["timezone"] or "UTC")
    except ZoneInfoNotFoundError:
        schedule_timezone = timezone.utc
    local_now = datetime.now(timezone.utc).astimezone(schedule_timezone)
    return templates.TemplateResponse(
        request,
        "schedule_backfill.html",
        {
            **shell_context(context, "schedules"),
            "notice": notice,
            "schedule": dict(schedule),
            "start_at": (local_now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            "end_at": local_now.strftime("%Y-%m-%dT%H:%M"),
            "backfill_max_runs": BACKFILL_MAX_RUNS,
        },
    )


@app.post("/schedules/{schedule_id}/backfill")
async def backfill_schedule(
    request: Request,
    schedule_id: str,
    start_at: str = Form(...),
    end_at: str = Form(...),
    max_runs: int = Form(25),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        schedule = conn.execute(
            """
            SELECT s.*, p.workspace_id, p.parameters_json AS project_parameters_json
            FROM schedules s
            JOIN projects p ON p.id = s.project_id
            WHERE s.id = ? AND p.workspace_id = ?
            """,
            (schedule_id, context.workspace_id),
        ).fetchone()
        if schedule is None:
            raise HTTPException(status_code=404, detail="Schedule not found")
        require_workspace_project_query_access(conn, context, schedule["project_id"])
        if not schedule["is_active"]:
            return redirect_to_schedule_backfill(schedule_id, "Resume the schedule before queuing a backfill.")
        try:
            range_start = parse_backfill_datetime(start_at, schedule["timezone"])
            range_end = parse_backfill_datetime(end_at, schedule["timezone"])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", end_at.strip()):
                range_end += timedelta(minutes=1) - timedelta(microseconds=1)
            occurrences = schedule_backfill_occurrences(schedule, range_start, range_end, max_runs)
        except ValueError as exc:
            return redirect_to_schedule_backfill(schedule_id, str(exc))
        if not occurrences:
            return redirect_to_schedule_backfill(schedule_id, "No scheduled occurrences fall within this range.")
        run_ids = [
            queue_schedule_run(
                conn,
                schedule,
                "schedule_backfill",
                scheduled_for_at=scheduled_for_at,
                parameter_overrides={BACKFILL_SCHEDULED_FOR_PARAMETER: scheduled_for_at},
                audit_detail={"backfill": True},
            )
            for scheduled_for_at in occurrences
        ]
        record_audit(
            conn,
            "schedule.backfill_queued",
            "schedule",
            schedule_id,
            {
                "project_id": schedule["project_id"],
                "run_ids": run_ids,
                "start_at": range_start.isoformat(),
                "end_at": range_end.isoformat(),
                "run_count": len(run_ids),
            },
            context.workspace_id,
        )
    return redirect_with_notice(f"Queued {len(run_ids)} backfill runs.")


@app.post("/reports")
async def create_report(
    request: Request,
    project_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    visibility: str = Form("workspace"),
) -> RedirectResponse:
    normalized_visibility = visibility.strip().lower()
    if normalized_visibility not in REPORT_VISIBILITIES:
        return redirect_with_notice("Report visibility must be workspace or private.")
    report_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        try:
            ensure_workspace_capacity(conn, context.workspace_id, "reports")
        except ValueError as exc:
            return redirect_with_notice(str(exc))
        require_workspace_project_query_access(conn, context, project_id)
        conn.execute(
            """
            INSERT INTO reports (id, workspace_id, project_id, created_by_user_id, title, description, visibility, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                context.workspace_id,
                project_id,
                context.user_id,
                title.strip() or "Untitled report",
                description.strip(),
                normalized_visibility,
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO report_subscriptions (report_id, user_id, workspace_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (report_id, context.user_id, context.workspace_id, timestamp),
        )
        create_default_report_widgets(conn, report_id, context.workspace_id, context.user_id, timestamp)
        latest_run = get_latest_successful_project_run(conn, project_id)
        if latest_run is not None:
            create_report_snapshot(conn, report_id, context.workspace_id, latest_run)
        record_audit(
            conn,
            "report.created",
            "report",
            report_id,
            {
                "project_id": project_id,
                "title": title.strip() or "Untitled report",
                "visibility": normalized_visibility,
            },
            context.workspace_id,
        )
    return RedirectResponse(f"/reports/{report_id}", status_code=303)


@app.post("/reports/{report_id}/visibility")
async def update_report_visibility(
    request: Request,
    report_id: str,
    visibility: str = Form(...),
) -> RedirectResponse:
    normalized_visibility = visibility.strip().lower()
    if normalized_visibility not in REPORT_VISIBILITIES:
        return redirect_with_notice("Report visibility must be workspace or private.")
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can update visibility")
        require_workspace_project_query_access(conn, context, report["project_id"])
        conn.execute(
            "UPDATE reports SET visibility = ?, updated_at = ? WHERE id = ?",
            (normalized_visibility, now_iso(), report_id),
        )
        removed_subscriptions = prune_private_report_subscriptions(conn, report) if normalized_visibility == "private" else 0
        record_audit(
            conn,
            "report.visibility_updated",
            "report",
            report_id,
            {"visibility": normalized_visibility, "removed_subscriptions": removed_subscriptions},
            context.workspace_id,
        )
    return redirect_with_notice("Report visibility updated.")


@app.post("/reports/{report_id}/widgets")
async def create_report_widget(
    request: Request,
    report_id: str,
    kind: str = Form(...),
    title: str = Form(""),
    aggregate: str = Form("sum"),
    value_column: str = Form(""),
    label_column: str = Form(""),
    x_column: str = Form(""),
    markdown_text: str = Form(""),
    table_limit: int = Form(100),
    table_highlight_column: str = Form(""),
    table_highlight_rule: str = Form("none"),
    table_highlight_threshold: str = Form(""),
    widget_width: str = Form(""),
) -> RedirectResponse:
    try:
        normalized_kind, normalized_title, config = parse_report_widget_config(
            kind,
            title,
            aggregate,
            value_column,
            label_column,
            x_column,
            markdown_text,
            table_limit,
            table_highlight_column,
            table_highlight_rule,
            table_highlight_threshold,
            widget_width,
        )
    except ValueError as exc:
        return redirect_to_report(report_id, str(exc))
    widget_id = uuid.uuid4().hex
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can manage components")
        require_workspace_project_query_access(conn, context, report["project_id"])
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM report_widgets WHERE report_id = ?",
            (report_id,),
        ).fetchone()["position"]
        conn.execute(
            """
            INSERT INTO report_widgets (id, report_id, workspace_id, created_by_user_id, kind, title, config_json, position, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                widget_id,
                report_id,
                context.workspace_id,
                context.user_id,
                normalized_kind,
                normalized_title,
                encode_json(config),
                position,
                now_iso(),
            ),
        )
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        record_audit(
            conn,
            "report.widget_created",
            "report",
            report_id,
            {"widget_id": widget_id, "kind": normalized_kind, "title": normalized_title},
            context.workspace_id,
        )
    return redirect_to_report(report_id, "Report component added.")


@app.post("/reports/{report_id}/widgets/{widget_id}/layout")
async def update_report_widget_layout(
    request: Request,
    report_id: str,
    widget_id: str,
    width: str = Form(...),
    direction: str = Form("stay"),
) -> RedirectResponse:
    normalized_width = width.strip().lower()
    normalized_direction = direction.strip().lower()
    if normalized_width not in REPORT_WIDGET_WIDTHS or normalized_direction not in {"up", "down", "stay"}:
        return redirect_to_report(report_id, "Invalid component layout update.")
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can manage components")
        require_workspace_project_query_access(conn, context, report["project_id"])
        widgets = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? AND workspace_id = ? ORDER BY position, created_at",
            (report_id, context.workspace_id),
        ).fetchall()
        widget_ids = [widget["id"] for widget in widgets]
        if widget_id not in widget_ids:
            raise HTTPException(status_code=404, detail="Report component not found")
        current_index = widget_ids.index(widget_id)
        target_index = current_index
        if normalized_direction == "up":
            target_index = max(current_index - 1, 0)
        elif normalized_direction == "down":
            target_index = min(current_index + 1, len(widget_ids) - 1)
        if target_index != current_index:
            widget_ids.insert(target_index, widget_ids.pop(current_index))
        for position, ordered_widget_id in enumerate(widget_ids):
            conn.execute("UPDATE report_widgets SET position = ? WHERE id = ?", (position, ordered_widget_id))
        selected_widget = next(widget for widget in widgets if widget["id"] == widget_id)
        config = decode_json(selected_widget["config_json"], {})
        if not isinstance(config, dict):
            config = {}
        config["width"] = normalized_width
        conn.execute("UPDATE report_widgets SET config_json = ? WHERE id = ?", (encode_json(config), widget_id))
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        record_audit(
            conn,
            "report.widget_layout_updated",
            "report",
            report_id,
            {
                "widget_id": widget_id,
                "width": normalized_width,
                "direction": normalized_direction,
                "position": target_index,
            },
            context.workspace_id,
        )
    return redirect_to_report(report_id, "Component layout updated.")


@app.post("/reports/{report_id}/widgets/reorder", status_code=204)
async def reorder_report_widgets(
    request: Request,
    report_id: str,
    order_json: str = Form(...),
) -> Response:
    try:
        ordered_widget_ids = json.loads(order_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Component order must be a JSON array") from exc
    if not isinstance(ordered_widget_ids, list) or not all(
        isinstance(widget_id, str) and widget_id for widget_id in ordered_widget_ids
    ):
        raise HTTPException(status_code=400, detail="Component order must contain component IDs")
    if len(set(ordered_widget_ids)) != len(ordered_widget_ids):
        raise HTTPException(status_code=400, detail="Component order cannot contain duplicates")
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can manage components")
        require_workspace_project_query_access(conn, context, report["project_id"])
        current_widget_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM report_widgets WHERE report_id = ? AND workspace_id = ? ORDER BY position, created_at",
                (report_id, context.workspace_id),
            ).fetchall()
        ]
        if len(ordered_widget_ids) != len(current_widget_ids) or set(ordered_widget_ids) != set(current_widget_ids):
            raise HTTPException(status_code=400, detail="Component order must include every report component exactly once")
        for position, widget_id in enumerate(ordered_widget_ids):
            conn.execute("UPDATE report_widgets SET position = ? WHERE id = ?", (position, widget_id))
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        record_audit(
            conn,
            "report.widget_reordered",
            "report",
            report_id,
            {"widget_ids": ordered_widget_ids},
            context.workspace_id,
        )
    return Response(status_code=204)


@app.post("/reports/{report_id}/widgets/{widget_id}/delete")
async def delete_report_widget(request: Request, report_id: str, widget_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can manage components")
        require_workspace_project_query_access(conn, context, report["project_id"])
        widget = conn.execute(
            "SELECT * FROM report_widgets WHERE id = ? AND report_id = ? AND workspace_id = ?",
            (widget_id, report_id, context.workspace_id),
        ).fetchone()
        if widget is None:
            raise HTTPException(status_code=404, detail="Report component not found")
        conn.execute("DELETE FROM report_widgets WHERE id = ?", (widget_id,))
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        record_audit(
            conn,
            "report.widget_deleted",
            "report",
            report_id,
            {"widget_id": widget_id, "kind": widget["kind"], "title": widget["title"]},
            context.workspace_id,
        )
    return redirect_to_report(report_id, "Report component removed.")


@app.post("/reports/{report_id}/filters")
async def create_report_filter(
    request: Request,
    report_id: str,
    name: str = Form(...),
    column_name: str = Form(...),
    filter_type: str = Form("select"),
    default_value: str = Form(""),
) -> RedirectResponse:
    try:
        normalized_name, normalized_column_name, normalized_filter_type, normalized_default_value = parse_report_filter_config(
            name,
            column_name,
            filter_type,
            default_value,
        )
    except ValueError as exc:
        return redirect_to_report(report_id, str(exc))
    filter_id = uuid.uuid4().hex
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can manage filters")
        require_workspace_project_query_access(conn, context, report["project_id"])
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS position FROM report_filters WHERE report_id = ?",
            (report_id,),
        ).fetchone()["position"]
        conn.execute(
            """
            INSERT INTO report_filters (
                id, report_id, workspace_id, created_by_user_id, name, column_name,
                filter_type, default_value, position, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filter_id,
                report_id,
                context.workspace_id,
                context.user_id,
                normalized_name,
                normalized_column_name,
                normalized_filter_type,
                normalized_default_value,
                position,
                now_iso(),
            ),
        )
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        record_audit(
            conn,
            "report.filter_created",
            "report",
            report_id,
            {
                "filter_id": filter_id,
                "name": normalized_name,
                "column_name": normalized_column_name,
                "filter_type": normalized_filter_type,
            },
            context.workspace_id,
        )
    return redirect_to_report(report_id, "Report filter added.")


@app.post("/reports/{report_id}/filters/{filter_id}/delete")
async def delete_report_filter(request: Request, report_id: str, filter_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can manage filters")
        require_workspace_project_query_access(conn, context, report["project_id"])
        report_filter = conn.execute(
            "SELECT * FROM report_filters WHERE id = ? AND report_id = ? AND workspace_id = ?",
            (filter_id, report_id, context.workspace_id),
        ).fetchone()
        if report_filter is None:
            raise HTTPException(status_code=404, detail="Report filter not found")
        conn.execute("DELETE FROM report_filters WHERE id = ?", (filter_id,))
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        record_audit(
            conn,
            "report.filter_deleted",
            "report",
            report_id,
            {"filter_id": filter_id, "name": report_filter["name"], "column_name": report_filter["column_name"]},
            context.workspace_id,
        )
    return redirect_to_report(report_id, "Report filter removed.")


@app.post("/reports/{report_id}/grants")
async def grant_report_access(
    request: Request,
    report_id: str,
    user_id: str = Form(...),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can grant access")
        require_workspace_project_query_access(conn, context, report["project_id"])
        if report["visibility"] != "private":
            return redirect_to_report(report_id, "Set the report to private before granting member access.")
        member = conn.execute(
            """
            SELECT u.id, u.email, m.role
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            WHERE m.workspace_id = ? AND u.id = ?
            """,
            (context.workspace_id, user_id),
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=404, detail="Workspace member not found")
        if member["id"] == report["created_by_user_id"] or member["role"] in {"owner", "admin"}:
            return redirect_to_report(report_id, "That member already has report access.")
        grant = conn.execute(
            """
            INSERT OR IGNORE INTO report_access_grants (
                report_id, user_id, workspace_id, granted_by_user_id, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_id, member["id"], context.workspace_id, context.user_id, now_iso()),
        )
        if grant.rowcount:
            record_audit(
                conn,
                "report.access_granted",
                "report",
                report_id,
                {"user_id": member["id"], "email": member["email"]},
                context.workspace_id,
            )
            notice = "Member access granted."
        else:
            notice = "That member already has report access."
    return redirect_to_report(report_id, notice)


@app.post("/reports/{report_id}/grants/{user_id}/delete")
async def revoke_report_access(request: Request, report_id: str, user_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report = get_workspace_report(conn, context.workspace_id, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if not can_manage_report(context, report):
            raise HTTPException(status_code=403, detail="Only the report creator or an administrator can revoke access")
        require_workspace_project_query_access(conn, context, report["project_id"])
        deleted = conn.execute(
            """
            DELETE FROM report_access_grants
            WHERE report_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (report_id, context.workspace_id, user_id),
        )
        if deleted.rowcount:
            removed_subscription = 0
            if report["visibility"] == "private":
                removed_subscription = conn.execute(
                    "DELETE FROM report_subscriptions WHERE report_id = ? AND workspace_id = ? AND user_id = ?",
                    (report_id, context.workspace_id, user_id),
                ).rowcount
                conn.execute(
                    """
                    DELETE FROM notifications
                    WHERE workspace_id = ?
                      AND resource_type = 'report'
                      AND resource_id = ?
                      AND recipient_user_id = ?
                    """,
                    (context.workspace_id, report_id, user_id),
                )
            record_audit(
                conn,
                "report.access_revoked",
                "report",
                report_id,
                {"user_id": user_id, "subscription_removed": bool(removed_subscription)},
                context.workspace_id,
            )
            notice = "Member access revoked."
        else:
            notice = "That member does not have explicit report access."
    return redirect_to_report(report_id, notice)


@app.post("/reports/{report_id}/subscriptions")
async def subscribe_to_report(
    request: Request,
    report_id: str,
    external_channel_ids: Optional[list[str]] = Form(None),
) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report, _source = require_workspace_report_query_access(conn, context, report_id)
        timestamp = now_iso()
        subscription = conn.execute(
            """
            INSERT OR IGNORE INTO report_subscriptions (report_id, user_id, workspace_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (report_id, context.user_id, context.workspace_id, timestamp),
        )
        try:
            selected_channels = set_subscription_channels(
                conn,
                report_id,
                context.user_id,
                context.workspace_id,
                external_channel_ids or [],
                timestamp,
            )
        except ValueError as exc:
            if subscription.rowcount:
                conn.execute(
                    "DELETE FROM report_subscriptions WHERE report_id = ? AND user_id = ?",
                    (report_id, context.user_id),
                )
            return redirect_to_report(report_id, str(exc))
        if subscription.rowcount:
            record_audit(
                conn,
                "report.subscribed",
                "report",
                report_id,
                {"user_id": context.user_id, "external_channel_ids": selected_channels},
                context.workspace_id,
            )
            notice = "Subscribed to report updates."
        else:
            record_audit(
                conn,
                "report.subscription_channels_updated",
                "report",
                report_id,
                {"user_id": context.user_id, "external_channel_ids": selected_channels},
                context.workspace_id,
            )
            notice = "Report delivery preferences updated."
    return redirect_to_report(report_id, notice)


@app.post("/reports/{report_id}/subscriptions/delete")
async def unsubscribe_from_report(request: Request, report_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report, _source = require_workspace_report_query_access(conn, context, report_id)
        deleted = conn.execute(
            "DELETE FROM report_subscriptions WHERE report_id = ? AND workspace_id = ? AND user_id = ?",
            (report_id, context.workspace_id, context.user_id),
        )
        if deleted.rowcount:
            record_audit(
                conn,
                "report.unsubscribed",
                "report",
                report_id,
                {"user_id": context.user_id},
                context.workspace_id,
            )
            notice = "Unsubscribed from report updates."
        else:
            notice = "You are not subscribed to this report."
    return redirect_to_report(report_id, notice)


@app.post("/notifications/{notification_id}/read")
async def mark_notification_read(request: Request, notification_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        notification = conn.execute(
            """
            SELECT *
            FROM notifications
            WHERE id = ?
              AND workspace_id = ?
              AND (recipient_user_id IS NULL OR recipient_user_id = ?)
            """,
            (notification_id, context.workspace_id, context.user_id),
        ).fetchone()
        if notification is None:
            raise HTTPException(status_code=404, detail="Notification not found")
        if not can_view_source_bound_resource(conn, context, notification["resource_type"], notification["resource_id"]):
            raise HTTPException(status_code=404, detail="Notification not found")
        conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
        record_audit(conn, "notification.read", "notification", notification_id, {}, context.workspace_id)
    return redirect_with_notice("Notification marked read.")


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def view_report(request: Request, report_id: str, notice: Optional[str] = None) -> HTMLResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        report, source = require_workspace_report_query_access(conn, context, report_id)
        latest_snapshot = get_latest_report_snapshot(conn, report_id)
        successful_snapshot = get_latest_report_snapshot(conn, report_id, "succeeded")
        latest_snapshot_run = require_workspace_run_query_access(conn, context, latest_snapshot["run_id"]) if latest_snapshot else None
        latest_run = require_workspace_run_query_access(conn, context, successful_snapshot["run_id"]) if successful_snapshot else None
        successful_snapshot_source = get_workspace_data_source(conn, context.workspace_id, latest_run["data_source_id"]) if latest_run else source
        snapshot_source = get_workspace_data_source(conn, context.workspace_id, latest_snapshot_run["data_source_id"]) if latest_snapshot_run else source
        can_export_snapshot = bool(
            successful_snapshot
            and successful_snapshot_source
            and can_export_data_source(conn, context, successful_snapshot_source)
        )
        can_manage = can_manage_report(context, report)
        report_grantees = get_report_grantees(conn, context.workspace_id, report_id) if can_manage else []
        granted_user_ids = {grantee["user_id"] for grantee in report_grantees}
        grantable_members = [
            member
            for member in get_workspace_members(conn, context.workspace_id)
            if member["user_id"] not in granted_user_ids
            and member["user_id"] != report["created_by_user_id"]
            and member["role"] not in {"owner", "admin"}
        ] if can_manage else []
        is_subscribed = conn.execute(
            """
            SELECT 1
            FROM report_subscriptions
            WHERE report_id = ? AND workspace_id = ? AND user_id = ?
            """,
            (report_id, context.workspace_id, context.user_id),
        ).fetchone() is not None
        subscription_channels = available_subscription_channels(conn, context.workspace_id)
        selected_subscription_channels = selected_subscription_channel_ids(conn, report_id, context.user_id)
        widgets = rows_to_dicts(
            conn.execute(
                """
                SELECT *
                FROM report_widgets
                WHERE report_id = ? AND workspace_id = ?
                ORDER BY position ASC, created_at ASC
                """,
                (report_id, context.workspace_id),
            ).fetchall()
        )
        report_filters = rows_to_dicts(
            conn.execute(
                """
                SELECT *
                FROM report_filters
                WHERE report_id = ? AND workspace_id = ?
                ORDER BY position ASC, created_at ASC
                """,
                (report_id, context.workspace_id),
            ).fetchall()
        )
    result = decode_json(successful_snapshot["result_json"], {}) if successful_snapshot else {}
    filtered_result, rendered_filters = apply_report_filters(result, report_filters, request.query_params)
    rendered_widgets = build_report_widgets(filtered_result, widgets)
    report_payload = dict(report)
    report_payload["can_manage"] = can_manage
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            **shell_context(context, "reports"),
            "notice": notice,
            "report": report_payload,
            "can_refresh": context.role in {"owner", "admin", "analyst"},
            "can_export_snapshot": can_export_snapshot,
            "is_subscribed": is_subscribed,
            "subscription_channels": subscription_channels,
            "selected_subscription_channels": selected_subscription_channels,
            "report_grantees": report_grantees,
            "grantable_members": grantable_members,
            "latest_run": dict(latest_run) if latest_run else None,
            "snapshot_run": dict(latest_snapshot_run) if latest_snapshot_run else None,
            "snapshot_source": dict(snapshot_source) if snapshot_source else None,
            "latest_snapshot": dict(latest_snapshot) if latest_snapshot else None,
            "successful_snapshot": dict(successful_snapshot) if successful_snapshot else None,
            "result": filtered_result,
            "widgets": rendered_widgets,
            "report_filters": rendered_filters,
        },
    )


@app.get("/reports/{report_id}/snapshot.csv")
async def download_report_snapshot_csv(request: Request, report_id: str) -> Response:
    result = load_report_snapshot_for_download(request, report_id, "csv")
    return result_csv_response(result, f"report-{report_id[:8]}-snapshot.csv")


@app.get("/reports/{report_id}/snapshot.json")
async def download_report_snapshot_json(request: Request, report_id: str) -> Response:
    result = load_report_snapshot_for_download(request, report_id, "json")
    return result_json_response(result, f"report-{report_id[:8]}-snapshot.json")


@app.get("/reports/{report_id}/snapshot.xlsx")
async def download_report_snapshot_xlsx(request: Request, report_id: str) -> Response:
    result, metadata = load_report_snapshot_export_payload(request, report_id, "xlsx", record_export=False)
    try:
        content = await asyncio.to_thread(
            render_xlsx,
            result.get("columns", []),
            result.get("rows", []),
            metadata["title"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_report_snapshot_export(report_id, "xlsx", metadata)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="report-{report_id[:8]}-snapshot.xlsx"'},
    )


@app.get("/reports/{report_id}/snapshot.png")
async def download_report_snapshot_png(request: Request, report_id: str) -> Response:
    result, metadata = load_report_snapshot_export_payload(request, report_id, "png", record_export=False)
    content = await asyncio.to_thread(
        render_report_png,
        metadata["title"],
        metadata["description"],
        metadata["snapshot_created_at"],
        result,
    )
    record_report_snapshot_export(report_id, "png", metadata)
    return Response(
        content,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="report-{report_id[:8]}-snapshot.png"'},
    )


@app.get("/reports/{report_id}/snapshot.pdf")
async def download_report_snapshot_pdf(request: Request, report_id: str) -> Response:
    result, metadata = load_report_snapshot_export_payload(request, report_id, "pdf", record_export=False)
    content = await asyncio.to_thread(
        render_report_pdf,
        metadata["title"],
        metadata["description"],
        metadata["snapshot_created_at"],
        result,
    )
    record_report_snapshot_export(report_id, "pdf", metadata)
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{report_id[:8]}-snapshot.pdf"'},
    )


@app.post("/reports/{report_id}/refresh")
async def refresh_report(request: Request, report_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        report, _source = require_workspace_report_query_access(conn, context, report_id)

    run_id = await asyncio.to_thread(create_run, report["project_id"], "report_refresh")
    if not await asyncio.to_thread(claim_run_execution, run_id):
        with connect() as conn:
            run = get_workspace_run(conn, context.workspace_id, run_id)
            if run is not None and run["status"] == "queued":
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'canceled', error = ?, finished_at = ?, duration_ms = 0
                    WHERE id = ? AND status = 'queued'
                    """,
                    ("Report refresh did not start because the workspace has no available execution slot.", now_iso(), run_id),
                )
                record_audit(
                    conn,
                    "run.canceled",
                    "run",
                    run_id,
                    {"phase": "workspace_concurrency", "report_id": report_id},
                    context.workspace_id,
                )
            record_audit(
                conn,
                "report.refresh_deferred",
                "report",
                report_id,
                {"run_id": run_id},
                context.workspace_id,
            )
        return redirect_to_report(report_id, "Report refresh needs an available workspace execution slot.")

    await asyncio.to_thread(execute_run, run_id)
    with connect() as conn:
        run = get_workspace_run(conn, context.workspace_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        create_report_snapshot(conn, report_id, context.workspace_id, run)
        conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), report_id))
        subscriber_notifications = notify_report_subscribers(conn, report, run)
        record_audit(
            conn,
            "report.refreshed",
            "report",
            report_id,
            {"run_id": run_id, "status": run["status"], "subscriber_notifications": subscriber_notifications},
            context.workspace_id,
        )
    return RedirectResponse(f"/reports/{report_id}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
async def search_runs_page(
    request: Request,
    q: str = "",
    status: str = "",
    trigger_type: str = "",
    project_id: str = "",
    started_from: str = "",
    started_to: str = "",
    page: int = 1,
) -> HTMLResponse:
    error = ""
    try:
        filters = RunSearchFilters.parse(q, status, trigger_type, project_id, started_from, started_to)
    except ValueError as exc:
        error = str(exc)
        filters = RunSearchFilters()
    with connect() as conn:
        context = get_request_context(request, conn)
        source_ids = queryable_workspace_source_ids(conn, context)
        projects = rows_to_dicts(
            conn.execute(
                "SELECT id, name, data_source_id FROM projects WHERE workspace_id = ? ORDER BY name",
                (context.workspace_id,),
            ).fetchall()
        )
        projects = [project for project in projects if project["data_source_id"] in source_ids]
        result = search_workspace_runs(conn, context.workspace_id, source_ids, filters, page)
    query_values = {
        "q": q,
        "status": status,
        "trigger_type": trigger_type,
        "project_id": project_id,
        "started_from": started_from,
        "started_to": started_to,
    }

    def page_url(target_page: int) -> str:
        return f"/runs?{urlencode({**query_values, 'page': target_page})}"

    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            **shell_context(context, "runs"),
            "error": error,
            "filters": filters,
            "raw_started_from": started_from,
            "raw_started_to": started_to,
            "projects": projects,
            "result": result,
            "run_statuses": RUN_STATUSES,
            "run_trigger_types": RUN_TRIGGER_TYPES,
            "previous_url": page_url(result["page"] - 1),
            "next_url": page_url(result["page"] + 1),
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def view_run(
    request: Request,
    run_id: str,
    notice: Optional[str] = None,
    result_page: int = 1,
    log_page: int = 1,
) -> HTMLResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        run = require_workspace_run_query_access(conn, context, run_id)
        source = get_workspace_data_source(conn, context.workspace_id, run["data_source_id"])
        can_export_result = bool(source and can_export_data_source(conn, context, source))
    result = decode_json(run["result_json"], None)
    displayed_result, result_pagination = paginate_run_result(result, result_page)
    displayed_logs, log_pagination = paginate_run_logs(run["logs"] or "", log_page)
    parameters = decode_json(run["parameters_json"], {})
    if not isinstance(parameters, dict):
        parameters = {}
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            **shell_context(context, "runs"),
            "notice": notice,
            "run": dict(run),
            "result": displayed_result,
            "result_pagination": result_pagination,
            "displayed_logs": displayed_logs,
            "log_pagination": log_pagination,
            "parameters": parameters,
            "parameters_json": json.dumps(parameters, ensure_ascii=False, indent=2, sort_keys=True),
            "can_cancel": context.role in {"owner", "admin", "analyst"} and run["status"] in {"queued", "running"},
            "can_export_result": bool(displayed_result.get("columns") and can_export_result),
        },
    )


@app.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str) -> RedirectResponse:
    with connect() as conn:
        context = get_request_context(request, conn)
        require_role(context, "analyst")
        run = require_workspace_run_query_access(conn, context, run_id)
        if run["status"] == "queued":
            canceled = conn.execute(
                """
                UPDATE runs
                SET status = 'canceled', error = ?, finished_at = ?, duration_ms = 0
                WHERE id = ? AND status = 'queued'
                """,
                ("Canceled before execution.", now_iso(), run_id),
            )
            if canceled.rowcount == 1:
                record_audit(
                    conn,
                    "run.canceled",
                    "run",
                    run_id,
                    {"requested_by_user_id": context.user_id, "phase": "queued"},
                    context.workspace_id,
                )
                return redirect_to_run(run_id, "Run canceled before execution.")
            run = get_workspace_run(conn, context.workspace_id, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
        if run["status"] == "running":
            canceling = conn.execute(
                "UPDATE runs SET status = 'canceling' WHERE id = ? AND status = 'running'",
                (run_id,),
            )
            if canceling.rowcount == 1:
                record_audit(
                    conn,
                    "run.cancel_requested",
                    "run",
                    run_id,
                    {"requested_by_user_id": context.user_id},
                    context.workspace_id,
                )
            else:
                run = get_workspace_run(conn, context.workspace_id, run_id)
                if run is None:
                    raise HTTPException(status_code=404, detail="Run not found")
                if run["status"] == "canceling":
                    return redirect_to_run(run_id, "Run cancellation is already in progress.")
                return redirect_to_run(run_id, f"Run is already {run['status']}.")
        elif run["status"] == "canceling":
            return redirect_to_run(run_id, "Run cancellation is already in progress.")
        else:
            return redirect_to_run(run_id, f"Run is already {run['status']}.")

    stopped = await asyncio.to_thread(cancel_run_execution, run_id)
    notice = "Run cancellation requested." if stopped else "Run cancellation requested; waiting for the executor to stop."
    return redirect_to_run(run_id, notice)


@app.get("/runs/{run_id}/result.csv")
async def download_run_result_csv(request: Request, run_id: str) -> Response:
    result = load_run_result_for_download(request, run_id, "csv")
    return result_csv_response(result, f"run-{run_id[:8]}-result.csv")


@app.get("/runs/{run_id}/result.json")
async def download_run_result_json(request: Request, run_id: str) -> Response:
    result = load_run_result_for_download(request, run_id, "json")
    return result_json_response(result, f"run-{run_id[:8]}-result.json")


def result_csv_response(result: dict[str, Any], filename: str) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(result.get("columns") or [])
    for row in result.get("rows") or []:
        writer.writerow(row)
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def result_json_response(result: dict[str, Any], filename: str) -> Response:
    return Response(
        json.dumps(result, ensure_ascii=False, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/runs")
async def search_runs_api(
    request: Request,
    q: str = "",
    status: str = "",
    trigger_type: str = "",
    project_id: str = "",
    started_from: str = "",
    started_to: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    try:
        filters = RunSearchFilters.parse(q, status, trigger_type, project_id, started_from, started_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with connect() as conn:
        context = get_request_context(request, conn)
        source_ids = queryable_workspace_source_ids(conn, context)
        return search_workspace_runs(conn, context.workspace_id, source_ids, filters, page, page_size)


@app.get("/api/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    with connect() as conn:
        context = get_request_context(request, conn)
        run = require_workspace_run_query_access(conn, context, run_id)
    payload = dict(run)
    payload["result"] = decode_json(payload.pop("result_json"), None)
    payload["parameters"] = decode_json(payload.pop("parameters_json"), {})
    return payload


@app.get("/api/workspace/quota")
async def get_workspace_quota_api(request: Request) -> dict[str, dict[str, int]]:
    with connect() as conn:
        context = get_request_context(request, conn)
        quota = get_workspace_quota(conn, context.workspace_id)
        usage = get_workspace_usage(conn, context.workspace_id)
    return {
        "limits": {resource: int(quota[f"max_{resource}"]) for resource in QUOTA_RESOURCES},
        "usage": usage,
    }


@app.get("/api/notifications")
async def list_notifications(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    bounded_limit = min(max(limit, 1), 200)
    with connect() as conn:
        context = get_request_context(request, conn)
        return list_visible_notifications(conn, context, bounded_limit)


@app.get("/api/audit-events")
async def list_audit_events(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    bounded_limit = min(max(limit, 1), 200)
    with connect() as conn:
        context = get_request_context(request, conn)
        rows = list_visible_audit_events(conn, context, bounded_limit)
    for row in rows:
        row["detail"] = decode_json(row.pop("detail_json"), {})
    return rows


async def schedule_loop(app_instance: FastAPI) -> None:
    while True:
        await asyncio.sleep(10)
        app_instance.state.scheduler_last_tick = time.time()
        await asyncio.to_thread(claim_due_schedules)
        asyncio.create_task(asyncio.to_thread(dispatch_due_notification_deliveries))
        for run_id in claim_queued_manual_runs():
            asyncio.create_task(asyncio.to_thread(execute_run, run_id))
        for run_id in claim_queued_schedule_runs():
            asyncio.create_task(asyncio.to_thread(execute_run, run_id))
        for run_id in claim_due_retries():
            asyncio.create_task(asyncio.to_thread(execute_run, run_id))


def claim_due_retries() -> list[str]:
    now = now_iso()
    claimed_run_ids: list[str] = []
    with connect() as conn:
        retries = conn.execute(
            """
            SELECT r.id, r.schedule_id, r.attempt, r.retry_of_run_id, r.trigger_type, p.workspace_id
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            JOIN schedules s ON s.id = r.schedule_id
            WHERE r.status = 'queued'
              AND r.trigger_type IN ('schedule_retry', 'schedule_backfill_retry')
              AND r.next_attempt_at <= ?
              AND s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs active_run
                  WHERE active_run.schedule_id = r.schedule_id
                    AND active_run.id != r.id
                    AND active_run.status IN ('running', 'canceling')
              )
            ORDER BY r.next_attempt_at ASC
            LIMIT 10
            """,
            (now,),
        ).fetchall()
    for retry in retries:
        if not claim_run_execution(retry["id"]):
            continue
        with connect() as conn:
            record_audit(
                conn,
                "run.retry_claimed",
                "run",
                retry["id"],
                {
                    "schedule_id": retry["schedule_id"],
                    "trigger_type": retry["trigger_type"],
                    "attempt": retry["attempt"],
                    "retry_of_run_id": retry["retry_of_run_id"],
                },
                retry["workspace_id"],
            )
        claimed_run_ids.append(retry["id"])
    return claimed_run_ids


def queue_schedule_run(
    conn,
    schedule,
    trigger_type: str,
    scheduled_for_at: Optional[str] = None,
    parameter_overrides: Optional[dict[str, Any]] = None,
    audit_detail: Optional[dict[str, Any]] = None,
) -> str:
    version = select_run_version(conn, schedule["project_id"])
    project_parameters_json = schedule["project_parameters_json"] if "project_parameters_json" in schedule.keys() else "{}"
    parameters = decode_json(version["parameters_json"] if version is not None else project_parameters_json, {})
    if not isinstance(parameters, dict):
        parameters = {}
    parameters = {**parameters, **(parameter_overrides or {})}
    parameters_json = encode_json(parameters)
    secret_bindings_json = version["secret_bindings_json"] if version is not None else "[]"
    secret_bindings = decode_json(secret_bindings_json, [])
    run_id = uuid.uuid4().hex
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO runs (
            id, project_id, project_version_id, status, trigger_type, schedule_id,
            scheduled_for_at, attempt, parameters_json, secret_bindings_json, started_at
        )
        VALUES (?, ?, ?, 'queued', ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            run_id,
            schedule["project_id"],
            version["id"] if version else None,
            trigger_type,
            schedule["id"],
            scheduled_for_at,
            parameters_json,
            secret_bindings_json,
            timestamp,
        ),
    )
    details = {
        "project_id": schedule["project_id"],
        "project_version_id": version["id"] if version else None,
        "schedule_id": schedule["id"],
        "trigger_type": trigger_type,
        "attempt": 1,
        "scheduled_for_at": scheduled_for_at,
        "parameter_names": sorted(parameters),
        "secret_binding_count": len(secret_bindings) if isinstance(secret_bindings, list) else 0,
    }
    details.update(audit_detail or {})
    record_audit(conn, "run.queued", "run", run_id, details, schedule["workspace_id"])
    return run_id


def supersede_schedule_runs(conn, schedule, active_runs) -> tuple[list[str], list[str]]:
    timestamp = now_iso()
    canceled_run_ids: list[str] = []
    cancel_requested_run_ids: list[str] = []
    for active_run in active_runs:
        run_id = active_run["id"]
        if active_run["status"] == "queued":
            canceled = conn.execute(
                """
                UPDATE runs
                SET status = 'canceled', error = ?, finished_at = ?, duration_ms = 0
                WHERE id = ? AND status = 'queued'
                """,
                ("Canceled because a newer scheduled run superseded it.", timestamp, run_id),
            )
            if canceled.rowcount == 1:
                canceled_run_ids.append(run_id)
                record_audit(
                    conn,
                    "run.canceled",
                    "run",
                    run_id,
                    {"reason": "cancel_previous", "schedule_id": schedule["id"], "phase": "queued"},
                    schedule["workspace_id"],
                )
        elif active_run["status"] == "running":
            canceling = conn.execute(
                "UPDATE runs SET status = 'canceling' WHERE id = ? AND status = 'running'",
                (run_id,),
            )
            if canceling.rowcount == 1:
                record_audit(
                    conn,
                    "run.cancel_requested",
                    "run",
                    run_id,
                    {"reason": "cancel_previous", "schedule_id": schedule["id"]},
                    schedule["workspace_id"],
                )
                cancel_requested_run_ids.append(run_id)
        elif active_run["status"] == "canceling":
            cancel_requested_run_ids.append(run_id)
    if canceled_run_ids or cancel_requested_run_ids:
        record_audit(
            conn,
            "schedule.run_superseded",
            "schedule",
            schedule["id"],
            {
                "concurrency_policy": "cancel_previous",
                "canceled_run_ids": canceled_run_ids,
                "cancel_requested_run_ids": cancel_requested_run_ids,
            },
            schedule["workspace_id"],
        )
    return canceled_run_ids, cancel_requested_run_ids


def claim_due_schedules() -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    claimed: list[dict[str, str]] = []
    cancel_run_ids: list[str] = []
    with connect() as conn:
        schedules = conn.execute(
            """
            SELECT s.*, p.workspace_id, p.parameters_json AS project_parameters_json
            FROM schedules s
            JOIN projects p ON p.id = s.project_id
            WHERE s.is_active = 1 AND s.next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT 10
            """,
            (now.isoformat(),),
        ).fetchall()
        for schedule in schedules:
            next_run_at = schedule_next_run_at(schedule, now)
            active_runs = conn.execute(
                """
                SELECT id, status
                FROM runs
                WHERE schedule_id = ? AND status IN ('queued', 'running', 'canceling')
                """,
                (schedule["id"],),
            ).fetchall()
            has_queued_run = any(active_run["status"] == "queued" for active_run in active_runs)
            policy = schedule["concurrency_policy"]
            # queue_all intentionally falls through so every due occurrence receives its own queue entry.
            should_skip = (policy == "skip" and bool(active_runs)) or (policy == "queue_one" and has_queued_run)
            if should_skip:
                conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (next_run_at, schedule["id"]))
                record_audit(
                    conn,
                    "schedule.run_skipped",
                    "schedule",
                    schedule["id"],
                    {
                        "concurrency_policy": policy,
                        "active_run_ids": [active_run["id"] for active_run in active_runs],
                        "reason": "active_run" if policy == "skip" else "queued_run",
                    },
                    schedule["workspace_id"],
                )
                continue
            canceled_run_ids: list[str] = []
            if policy == "cancel_previous" and active_runs:
                canceled_run_ids, superseded_run_ids = supersede_schedule_runs(conn, schedule, active_runs)
                cancel_run_ids.extend(superseded_run_ids)
            else:
                superseded_run_ids = []
            conn.execute(
                "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (now.isoformat(), next_run_at, schedule["id"]),
            )
            run_id = queue_schedule_run(
                conn,
                schedule,
                "schedule",
                schedule["next_run_at"],
                audit_detail={
                    "concurrency_policy": policy,
                    "replaced_run_ids": canceled_run_ids + superseded_run_ids,
                },
            )
            claimed.append({"run_id": run_id, "schedule_id": schedule["id"]})
    for run_id in dict.fromkeys(cancel_run_ids):
        try:
            cancel_run_execution(run_id)
        except Exception:  # noqa: BLE001
            pass
    return claimed


def claim_queued_schedule_runs() -> list[str]:
    claimed_run_ids: list[str] = []
    with connect() as conn:
        queued_runs = conn.execute(
            """
            SELECT r.id, r.schedule_id, p.workspace_id
            FROM runs r
            JOIN schedules s ON s.id = r.schedule_id
            JOIN projects p ON p.id = r.project_id
            WHERE r.status = 'queued'
              AND r.trigger_type IN ('schedule', 'schedule_backfill')
              AND s.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM runs active_run
                  WHERE active_run.schedule_id = r.schedule_id
                    AND active_run.id != r.id
                    AND active_run.status IN ('running', 'canceling')
              )
            ORDER BY COALESCE(r.scheduled_for_at, r.started_at) ASC
            LIMIT 10
            """
        ).fetchall()
    for queued_run in queued_runs:
        if not claim_run_execution(queued_run["id"]):
            continue
        with connect() as conn:
            record_audit(
                conn,
                "run.schedule_claimed",
                "run",
                queued_run["id"],
                {"schedule_id": queued_run["schedule_id"]},
                queued_run["workspace_id"],
            )
        claimed_run_ids.append(queued_run["id"])
    return claimed_run_ids


def claim_queued_manual_runs() -> list[str]:
    claimed_run_ids: list[str] = []
    with connect() as conn:
        queued_runs = conn.execute(
            """
            SELECT r.id, r.trigger_type, p.workspace_id
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            WHERE r.status = 'queued'
              AND r.trigger_type IN ('manual', 'schedule_manual')
            ORDER BY r.started_at ASC
            LIMIT 10
            """
        ).fetchall()
    for queued_run in queued_runs:
        if not claim_run_execution(queued_run["id"]):
            continue
        with connect() as conn:
            record_audit(
                conn,
                "run.manual_claimed",
                "run",
                queued_run["id"],
                {"trigger_type": queued_run["trigger_type"]},
                queued_run["workspace_id"],
            )
        claimed_run_ids.append(queued_run["id"])
    return claimed_run_ids


def create_project_version(
    conn,
    project_id: str,
    language: str,
    script: str,
    parameters_json: str,
    runtime_profile: str,
    data_source_id: str,
    created_at: str,
    secret_bindings_json: str = "[]",
) -> str:
    latest = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS version_number FROM project_versions WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    version_number = int(latest["version_number"]) + 1
    version_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO project_versions (
            id, project_id, version_number, language, script, parameters_json,
            runtime_profile, secret_bindings_json, data_source_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            project_id,
            version_number,
            language,
            script,
            parameters_json,
            runtime_profile,
            secret_bindings_json,
            data_source_id,
            created_at,
        ),
    )
    return version_id


def get_workspace_run(conn, workspace_id: str, run_id: str):
    return conn.execute(
        """
        SELECT
            r.*,
            p.name AS project_name,
            COALESCE(pv.data_source_id, p.data_source_id) AS data_source_id,
            d.name AS data_source_name,
            d.source_type AS data_source_type,
            d.classification AS data_source_classification,
            pv.version_number AS version_number
        FROM runs r
        JOIN projects p ON p.id = r.project_id
        LEFT JOIN project_versions pv ON pv.id = r.project_version_id
        LEFT JOIN data_sources d ON d.id = COALESCE(pv.data_source_id, p.data_source_id)
        WHERE r.id = ? AND p.workspace_id = ?
        """,
        (run_id, workspace_id),
    ).fetchone()


def get_workspace_report(conn, workspace_id: str, report_id: str):
    return conn.execute(
        """
        SELECT
            r.*,
            p.name AS project_name,
            p.id AS project_id,
            published.version_number AS published_version_number,
            d.id AS data_source_id,
            d.name AS data_source_name,
            d.source_type AS data_source_type
        FROM reports r
        JOIN projects p ON p.id = r.project_id
        LEFT JOIN project_versions published ON published.id = p.published_version_id
        LEFT JOIN data_sources d ON d.id = COALESCE(published.data_source_id, p.data_source_id)
        WHERE r.id = ? AND r.workspace_id = ?
        """,
        (report_id, workspace_id),
    ).fetchone()


def get_latest_successful_project_run(conn, project_id: str):
    return conn.execute(
        """
        SELECT *
        FROM runs
        WHERE project_id = ? AND status = 'succeeded'
        ORDER BY finished_at DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()


def get_latest_report_snapshot(conn, report_id: str, status: Optional[str] = None):
    if status is None:
        return conn.execute(
            """
            SELECT *
            FROM report_snapshots
            WHERE report_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (report_id,),
        ).fetchone()
    return conn.execute(
        """
        SELECT *
        FROM report_snapshots
        WHERE report_id = ? AND status = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (report_id, status),
    ).fetchone()


def load_run_result_for_download(request: Request, run_id: str, export_format: str) -> dict[str, Any]:
    with connect() as conn:
        context = get_request_context(request, conn)
        run = require_workspace_run_query_access(conn, context, run_id)
        source = get_workspace_data_source(conn, context.workspace_id, run["data_source_id"])
        if source is None:
            raise HTTPException(status_code=404, detail="Data source not found")
        require_data_source_export_access(conn, context, source)
        result = decode_json(run["result_json"], None)
        if not isinstance(result, dict) or not result.get("columns"):
            raise HTTPException(status_code=404, detail="Run result is not available")
        result, masked_columns = apply_export_masking(
            result,
            decode_json(source["column_metadata_json"], {}),
            allow_raw=can_manage_data_source(conn, context, source),
        )
        record_audit(
            conn,
            "run.exported",
            "run",
            run_id,
            {
                "format": export_format,
                "data_source_id": source["id"],
                "classification": source["classification"],
                "masked_columns": masked_columns,
            },
            context.workspace_id,
        )
    return result


def load_report_snapshot_export_payload(
    request: Request,
    report_id: str,
    export_format: str,
    record_export: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with connect() as conn:
        context = get_request_context(request, conn)
        report, _source = require_workspace_report_query_access(conn, context, report_id)
        snapshot = get_latest_report_snapshot(conn, report_id, "succeeded")
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Report snapshot is not available")
        run = require_workspace_run_query_access(conn, context, snapshot["run_id"])
        source = get_workspace_data_source(conn, context.workspace_id, run["data_source_id"])
        if source is None:
            raise HTTPException(status_code=404, detail="Data source not found")
        require_data_source_export_access(conn, context, source)
        result = decode_json(snapshot["result_json"], None)
        if not isinstance(result, dict) or not result.get("columns"):
            raise HTTPException(status_code=404, detail="Report snapshot is not available")
        result, masked_columns = apply_export_masking(
            result,
            decode_json(source["column_metadata_json"], {}),
            allow_raw=can_manage_data_source(conn, context, source),
        )
        metadata = {
            "title": report["title"],
            "description": report["description"],
            "snapshot_created_at": snapshot["created_at"][:19].replace("T", " "),
            "snapshot_id": snapshot["id"],
            "data_source_id": source["id"],
            "classification": source["classification"],
            "workspace_id": context.workspace_id,
            "masked_columns": masked_columns,
        }
        if record_export:
            record_report_snapshot_export(report_id, export_format, metadata, conn=conn)
    return result, metadata


def record_report_snapshot_export(
    report_id: str,
    export_format: str,
    metadata: dict[str, Any],
    conn=None,
) -> None:
    if conn is None:
        with connect() as export_conn:
            record_report_snapshot_export(report_id, export_format, metadata, conn=export_conn)
        return
    record_audit(
        conn,
        "report.exported",
        "report",
        report_id,
        {
            "format": export_format,
            "snapshot_id": metadata["snapshot_id"],
            "data_source_id": metadata["data_source_id"],
            "classification": metadata["classification"],
            "masked_columns": metadata.get("masked_columns", []),
        },
        metadata["workspace_id"],
    )


def load_report_snapshot_for_download(request: Request, report_id: str, export_format: str) -> dict[str, Any]:
    result, _metadata = load_report_snapshot_export_payload(request, report_id, export_format)
    return result


def paginate_items(items: list[Any], requested_page: int, page_size: int) -> tuple[list[Any], dict[str, int]]:
    total = len(items)
    total_pages = max(1, math.ceil(total / page_size))
    page = min(max(requested_page, 1), total_pages)
    start = (page - 1) * page_size
    end = min(start + page_size, total)
    return items[start:end], {
        "page": page,
        "total_pages": total_pages,
        "start": start + 1 if total else 0,
        "end": end,
        "total": total,
    }


def paginate_run_result(result: Any, requested_page: int) -> tuple[dict[str, Any], dict[str, int]]:
    columns, rows = result_columns_and_rows(result if isinstance(result, dict) else {})
    page_rows, pagination = paginate_items(rows, requested_page, RUN_RESULT_PAGE_SIZE)
    displayed_result = dict(result) if isinstance(result, dict) else {}
    displayed_result["columns"] = columns
    displayed_result["rows"] = page_rows
    return displayed_result, pagination


def paginate_run_logs(logs: str, requested_page: int) -> tuple[str, dict[str, int]]:
    page_lines, pagination = paginate_items(logs.splitlines(), requested_page, RUN_LOG_PAGE_SIZE)
    return "\n".join(page_lines), pagination


def result_columns_and_rows(result: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    raw_columns = result.get("columns") if isinstance(result, dict) else []
    raw_rows = result.get("rows") if isinstance(result, dict) else []
    columns = [str(column) for column in raw_columns] if isinstance(raw_columns, list) else []
    rows = [list(row) for row in raw_rows if isinstance(row, (list, tuple))] if isinstance(raw_rows, list) else []
    return columns, rows


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def column_index(columns: list[str], rows: list[list[Any]], requested: str = "", numeric: bool = False) -> Optional[int]:
    if requested and requested in columns:
        index = columns.index(requested)
        if not numeric or any(index < len(row) and as_number(row[index]) is not None for row in rows):
            return index
        return None
    if numeric:
        for index in range(len(columns)):
            if any(index < len(row) and as_number(row[index]) is not None for row in rows):
                return index
        return None
    return 0 if columns else None


def format_metric_value(value: float | int) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def build_metric_widget(result: dict[str, Any], title: str, config: dict[str, Any]) -> dict[str, str]:
    columns, rows = result_columns_and_rows(result)
    aggregate = str(config.get("aggregate") or "row_count")
    if aggregate == "row_count":
        return {"title": title or "Rows", "value": format_metric_value(len(rows)), "detail": "rows"}
    if aggregate == "column_count":
        return {"title": title or "Columns", "value": format_metric_value(len(columns)), "detail": "columns"}

    value_column = str(config.get("value_column") or "")
    index = column_index(columns, rows, value_column, numeric=aggregate != "count")
    if index is None:
        return {"title": title or "Metric", "value": "--", "detail": "No matching values"}
    if aggregate == "count":
        count = sum(1 for row in rows if index < len(row) and row[index] is not None and row[index] != "")
        return {"title": title or f"{columns[index]} count", "value": format_metric_value(count), "detail": columns[index]}

    values = [as_number(row[index]) for row in rows if index < len(row)]
    numeric_values = [value for value in values if value is not None]
    if not numeric_values:
        return {"title": title or "Metric", "value": "--", "detail": "No numeric values"}
    aggregations = {
        "sum": sum(numeric_values),
        "average": sum(numeric_values) / len(numeric_values),
        "minimum": min(numeric_values),
        "maximum": max(numeric_values),
    }
    value = aggregations.get(aggregate, sum(numeric_values))
    return {"title": title or f"{columns[index]} {aggregate}", "value": format_metric_value(value), "detail": columns[index]}


def build_chart_data(result: dict[str, Any], label_column: str = "", value_column: str = "") -> dict[str, Any]:
    columns, rows = result_columns_and_rows(result)
    numeric_index = column_index(columns, rows, value_column, numeric=True)
    if numeric_index is None:
        return {"bars": [], "line_points": [], "polyline_points": "", "value_column": "", "label_column": ""}
    if label_column and label_column in columns:
        label_index = columns.index(label_column)
    else:
        label_index = 0 if numeric_index != 0 else (1 if len(columns) > 1 else 0)

    points: list[dict[str, Any]] = []
    for row in rows:
        if numeric_index >= len(row):
            continue
        value = as_number(row[numeric_index])
        if value is None:
            continue
        label = str(row[label_index])[:24] if label_index < len(row) else ""
        points.append({"label": label, "value": value})
        if len(points) == 12:
            break
    if not points:
        return {"bars": [], "line_points": [], "polyline_points": "", "value_column": columns[numeric_index], "label_column": ""}

    max_absolute = max(abs(point["value"]) for point in points)
    bars = [
        {
            "label": point["label"],
            "value": point["value"],
            "width": 0 if max_absolute == 0 else round((abs(point["value"]) / max_absolute) * 100, 2),
            "negative": point["value"] < 0,
        }
        for point in points
    ]
    minimum = min(point["value"] for point in points)
    maximum = max(point["value"] for point in points)
    value_range = maximum - minimum
    line_points = []
    for index, point in enumerate(points):
        x = 50 if len(points) == 1 else round(index / (len(points) - 1) * 100, 2)
        y = 50 if value_range == 0 else round(100 - ((point["value"] - minimum) / value_range * 100), 2)
        line_points.append({**point, "x": x, "y": y})
    return {
        "bars": bars,
        "line_points": line_points,
        "polyline_points": " ".join(f"{point['x']},{point['y']}" for point in line_points),
        "value_column": columns[numeric_index],
        "label_column": columns[label_index] if label_index < len(columns) else "",
    }


def build_scatter_chart(result: dict[str, Any], x_column: str = "", value_column: str = "") -> dict[str, Any]:
    columns, rows = result_columns_and_rows(result)
    value_index = column_index(columns, rows, value_column, numeric=True)
    if value_index is None:
        return {"points": [], "x_column": "", "value_column": ""}

    x_index = column_index(columns, rows, x_column, numeric=True)
    if x_index is None or x_index == value_index:
        x_index = next(
            (
                index
                for index, _column in enumerate(columns)
                if index != value_index
                and any(index < len(row) and as_number(row[index]) is not None for row in rows)
            ),
            None,
        )
    if x_index is None:
        return {"points": [], "x_column": "", "value_column": columns[value_index]}

    values: list[tuple[float, float]] = []
    for row in rows:
        if x_index >= len(row) or value_index >= len(row):
            continue
        x_value = as_number(row[x_index])
        y_value = as_number(row[value_index])
        if x_value is None or y_value is None:
            continue
        values.append((x_value, y_value))
        if len(values) == 100:
            break
    if not values:
        return {"points": [], "x_column": columns[x_index], "value_column": columns[value_index]}

    minimum_x = min(value[0] for value in values)
    maximum_x = max(value[0] for value in values)
    minimum_y = min(value[1] for value in values)
    maximum_y = max(value[1] for value in values)
    x_range = maximum_x - minimum_x
    y_range = maximum_y - minimum_y
    points = []
    for x_value, y_value in values:
        x = 50 if x_range == 0 else round(6 + ((x_value - minimum_x) / x_range * 88), 2)
        y = 50 if y_range == 0 else round(94 - ((y_value - minimum_y) / y_range * 88), 2)
        points.append(
            {
                "x": x,
                "y": y,
                "tooltip": f"{columns[x_index]}: {format_metric_value(x_value)} | {columns[value_index]}: {format_metric_value(y_value)}",
            }
        )
    return {
        "points": points,
        "x_column": columns[x_index],
        "value_column": columns[value_index],
    }


def build_pie_chart(result: dict[str, Any], label_column: str = "", value_column: str = "") -> dict[str, Any]:
    columns, rows = result_columns_and_rows(result)
    numeric_index = column_index(columns, rows, value_column, numeric=True)
    if numeric_index is None:
        return {"slices": [], "gradient": "", "value_column": "", "label_column": "", "total": 0}
    if label_column and label_column in columns:
        label_index = columns.index(label_column)
    else:
        label_index = 0 if numeric_index != 0 else (1 if len(columns) > 1 else 0)

    grouped: dict[str, float] = {}
    for row in rows:
        if numeric_index >= len(row):
            continue
        value = as_number(row[numeric_index])
        if value is None or value <= 0:
            continue
        label = str(row[label_index])[:48] if label_index < len(row) else ""
        grouped[label] = grouped.get(label, 0) + value
    entries = sorted(grouped.items(), key=lambda entry: entry[1], reverse=True)
    if len(entries) > len(PIE_COLORS):
        other_value = sum(value for _label, value in entries[len(PIE_COLORS) - 1 :])
        entries = [*entries[: len(PIE_COLORS) - 1], ("Other", other_value)]
    total = sum(value for _label, value in entries)
    if total <= 0:
        return {
            "slices": [],
            "gradient": "",
            "value_column": columns[numeric_index],
            "label_column": columns[label_index] if label_index < len(columns) else "",
            "total": 0,
        }

    start = 0.0
    slices = []
    gradient_segments = []
    for index, (label, value) in enumerate(entries):
        end = start + (value / total * 100)
        color = PIE_COLORS[index % len(PIE_COLORS)]
        slices.append({"label": label, "value": value, "percent": round(value / total * 100, 2), "color": color})
        gradient_segments.append(f"{color} {start:.2f}% {end:.2f}%")
        start = end
    return {
        "slices": slices,
        "gradient": f"conic-gradient({', '.join(gradient_segments)})",
        "value_column": columns[numeric_index],
        "label_column": columns[label_index] if label_index < len(columns) else "",
        "total": total,
    }


def apply_report_filters(result: dict[str, Any], stored_filters: list[dict[str, Any]], query_params) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    columns, rows = result_columns_and_rows(result)
    filtered_rows = rows
    rendered_filters: list[dict[str, Any]] = []
    for stored_filter in stored_filters:
        column_name = str(stored_filter["column_name"])
        filter_type = str(stored_filter["filter_type"])
        key = f"filter_{stored_filter['id']}"
        rendered_filter = {
            "id": stored_filter["id"],
            "name": stored_filter["name"],
            "column_name": column_name,
            "filter_type": filter_type,
            "input_name": key,
            "min_input_name": f"{key}_min",
            "max_input_name": f"{key}_max",
            "available": column_name in columns,
            "options": [],
            "value": "",
            "minimum": "",
            "maximum": "",
        }
        if column_name not in columns:
            rendered_filters.append(rendered_filter)
            continue
        index = columns.index(column_name)
        default_value = str(stored_filter["default_value"] or "")
        if filter_type == "select":
            options = sorted(
                {str(row[index]) for row in rows if index < len(row) and row[index] is not None and str(row[index]) != ""},
                key=str.casefold,
            )[:100]
            value = str(query_params.get(key, default_value) or "").strip()
            rendered_filter["options"] = options
            rendered_filter["value"] = value
            if value:
                filtered_rows = [row for row in filtered_rows if index < len(row) and str(row[index]) == value]
        elif filter_type == "contains":
            value = str(query_params.get(key, default_value) or "").strip()
            rendered_filter["value"] = value
            if value:
                lowered = value.casefold()
                filtered_rows = [row for row in filtered_rows if index < len(row) and lowered in str(row[index]).casefold()]
        elif filter_type == "range":
            default_minimum, separator, default_maximum = default_value.partition(",")
            if not separator:
                default_minimum = default_maximum = ""
            minimum = str(query_params.get(rendered_filter["min_input_name"], default_minimum) or "").strip()
            maximum = str(query_params.get(rendered_filter["max_input_name"], default_maximum) or "").strip()
            minimum_number = as_number(minimum) if minimum else None
            maximum_number = as_number(maximum) if maximum else None
            rendered_filter["minimum"] = minimum
            rendered_filter["maximum"] = maximum
            if minimum_number is not None or maximum_number is not None:
                filtered_rows = [
                    row
                    for row in filtered_rows
                    if index < len(row)
                    and (value := as_number(row[index])) is not None
                    and (minimum_number is None or value >= minimum_number)
                    and (maximum_number is None or value <= maximum_number)
                ]
        rendered_filters.append(rendered_filter)
    filtered_result = dict(result)
    filtered_result["columns"] = columns
    filtered_result["rows"] = filtered_rows
    summary = filtered_result.get("summary")
    if isinstance(summary, dict):
        filtered_result["summary"] = {**summary, "rows": len(filtered_rows), "columns": len(columns)}
    return filtered_result, rendered_filters


def build_table_widget(result: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    columns, rows = result_columns_and_rows(result)
    try:
        limit = int(config.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = min(max(limit, 1), 500)
    highlight_rule = str(config.get("highlight_rule") or "none").strip().lower()
    if highlight_rule not in REPORT_TABLE_HIGHLIGHT_RULES:
        highlight_rule = "none"
    highlight_column = str(config.get("highlight_column") or "")
    highlight_index = column_index(columns, rows, highlight_column, numeric=True) if highlight_rule != "none" else None
    highlight_threshold = as_number(config.get("highlight_threshold"))
    if highlight_rule in {"above", "below"} and highlight_threshold is None:
        highlight_rule = "none"

    def cell_class(index: int, value: Any) -> str:
        if index != highlight_index:
            return ""
        numeric_value = as_number(value)
        if numeric_value is None:
            return ""
        if highlight_rule == "positive" and numeric_value > 0:
            return "table-cell-good"
        if highlight_rule == "negative" and numeric_value < 0:
            return "table-cell-bad"
        if highlight_rule == "above" and numeric_value >= highlight_threshold:
            return "table-cell-good"
        if highlight_rule == "below" and numeric_value <= highlight_threshold:
            return "table-cell-bad"
        return ""

    rendered_rows = [
        {"cells": [{"value": value, "class_name": cell_class(index, value)} for index, value in enumerate(row)]}
        for row in rows[:limit]
    ]
    return {
        "columns": columns,
        "rows": rendered_rows,
        "total_rows": len(rows),
        "limit": limit,
        "highlight_column": columns[highlight_index] if highlight_index is not None else "",
        "highlight_rule": highlight_rule,
    }


def render_markdown(content: str) -> Markup:
    def inline(value: str) -> str:
        escaped = html.escape(value)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)

    parts: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            parts.append("<ul>" + "".join(list_items) + "</ul>")
            list_items = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            flush_list()
            continue
        if line.startswith("- "):
            list_items.append(f"<li>{inline(line[2:])}</li>")
            continue
        flush_list()
        if line.startswith("### "):
            parts.append(f"<h5>{inline(line[4:])}</h5>")
        elif line.startswith("## "):
            parts.append(f"<h4>{inline(line[3:])}</h4>")
        elif line.startswith("# "):
            parts.append(f"<h3>{inline(line[2:])}</h3>")
        else:
            parts.append(f"<p>{inline(line)}</p>")
    flush_list()
    return Markup("\n".join(parts))


def build_report_widgets(result: dict[str, Any], widgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered = []
    for stored_widget in widgets:
        config = decode_json(stored_widget.get("config_json"), {})
        if not isinstance(config, dict):
            config = {}
        widget = {
            "id": stored_widget["id"],
            "kind": stored_widget["kind"],
            "title": stored_widget["title"],
            "position": stored_widget.get("position", 0),
            "width": (
                config.get("width")
                if config.get("width") in REPORT_WIDGET_WIDTHS
                else default_report_widget_width(stored_widget["kind"])
            ),
        }
        if widget["kind"] == "metric":
            widget["metric"] = build_metric_widget(result, widget["title"], config)
        elif widget["kind"] in {"bar", "line"}:
            widget["chart"] = build_chart_data(
                result,
                str(config.get("label_column") or ""),
                str(config.get("value_column") or ""),
            )
        elif widget["kind"] == "scatter":
            widget["scatter"] = build_scatter_chart(
                result,
                str(config.get("x_column") or ""),
                str(config.get("value_column") or ""),
            )
        elif widget["kind"] == "pie":
            widget["pie"] = build_pie_chart(
                result,
                str(config.get("label_column") or ""),
                str(config.get("value_column") or ""),
            )
        elif widget["kind"] == "table":
            widget["table"] = build_table_widget(result, config)
        elif widget["kind"] == "markdown":
            widget["markdown_html"] = render_markdown(str(config.get("content") or ""))
        rendered.append(widget)
    return rendered


def redirect_with_notice(message: str) -> RedirectResponse:
    return RedirectResponse(f"/?notice={message}", status_code=303)


def redirect_to_report(report_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/reports/{report_id}?notice={quote(message)}", status_code=303)


def redirect_to_run(run_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/runs/{run_id}?notice={quote(message)}", status_code=303)


def redirect_to_schedule_backfill(schedule_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/schedules/{schedule_id}/backfill?notice={quote(message)}", status_code=303)


def redirect_to_data_source(source_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(f"/data-sources/{source_id}?notice={quote(message)}", status_code=303)
