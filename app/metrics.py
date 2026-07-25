from __future__ import annotations

from typing import Any


RUN_STATUSES = ("queued", "running", "succeeded", "failed", "canceling", "canceled")
DELIVERY_STATUSES = ("queued", "sending", "sent", "failed", "canceled")
RUN_USAGE_PERIODS = (
    ("24h", "Last 24 Hours", "-1 day"),
    ("7d", "Last 7 Days", "-7 days"),
    ("retained", "Retained History", None),
)


def prometheus_label_value(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metric_line(name: str, value: int | float, labels: dict[str, Any] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    rendered_labels = ",".join(
        f'{label_name}="{prometheus_label_value(label_value)}"'
        for label_name, label_value in labels.items()
    )
    return f"{name}{{{rendered_labels}}} {value}"


def add_metric_header(lines: list[str], name: str, description: str) -> None:
    lines.append(f"# HELP {name} {description}")
    lines.append(f"# TYPE {name} gauge")


def grouped_counts(conn, query: str) -> dict[str, int]:
    return {str(row["name"]): int(row["count"]) for row in conn.execute(query).fetchall()}


def scalar_count(conn, query: str) -> int:
    row = conn.execute(query).fetchone()
    return int(row["count"] if row else 0)


def workspace_run_usage(conn, workspace_id: str, hourly_cost_cny: float = 0) -> list[dict[str, Any]]:
    usage = []
    for key, label, interval in RUN_USAGE_PERIODS:
        time_filter = "" if interval is None else "AND datetime(r.started_at) >= datetime('now', ?)"
        parameters: tuple[Any, ...] = (workspace_id,) if interval is None else (workspace_id, interval)
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_runs,
                SUM(CASE WHEN r.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded_runs,
                SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END) AS failed_runs,
                SUM(CASE WHEN r.status = 'canceled' THEN 1 ELSE 0 END) AS canceled_runs,
                SUM(CASE WHEN r.status IN ('queued', 'running', 'canceling') THEN 1 ELSE 0 END) AS active_runs,
                COALESCE(SUM(r.duration_ms), 0) AS total_duration_ms,
                COALESCE(AVG(r.duration_ms), 0) AS average_duration_ms
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            WHERE p.workspace_id = ?
              {time_filter}
            """,
            parameters,
        ).fetchone()
        completed_runs = int(row["succeeded_runs"] or 0) + int(row["failed_runs"] or 0) + int(row["canceled_runs"] or 0)
        duration_hours = float(row["total_duration_ms"] or 0) / 3_600_000
        usage.append(
            {
                "key": key,
                "label": label,
                "total_runs": int(row["total_runs"] or 0),
                "succeeded_runs": int(row["succeeded_runs"] or 0),
                "failed_runs": int(row["failed_runs"] or 0),
                "canceled_runs": int(row["canceled_runs"] or 0),
                "active_runs": int(row["active_runs"] or 0),
                "success_rate": round((int(row["succeeded_runs"] or 0) / completed_runs * 100) if completed_runs else 0, 1),
                "duration_hours": round(duration_hours, 3),
                "average_duration_seconds": round(float(row["average_duration_ms"] or 0) / 1000, 2),
                "estimated_cost_cny": round(duration_hours * max(hourly_cost_cny, 0), 2),
            }
        )
    return usage


def render_prometheus_metrics(
    conn,
    runner: str,
    scheduler_enabled: bool,
    scheduler_running: bool,
    scheduler_last_tick: float | None,
) -> str:
    lines: list[str] = []
    add_metric_header(lines, "anydatas_info", "Static AnyDatas process information.")
    lines.append(
        metric_line(
            "anydatas_info",
            1,
            {
                "runner": runner,
                "scheduler_enabled": str(scheduler_enabled).lower(),
            },
        )
    )
    add_metric_header(lines, "anydatas_up", "Whether the AnyDatas control plane can serve metrics.")
    lines.append(metric_line("anydatas_up", 1))
    add_metric_header(lines, "anydatas_database_up", "Whether the metadata database responded to this scrape.")
    lines.append(metric_line("anydatas_database_up", 1))
    add_metric_header(lines, "anydatas_scheduler_up", "Whether the in-process scheduler task is active.")
    lines.append(metric_line("anydatas_scheduler_up", int(scheduler_enabled and scheduler_running)))
    if scheduler_last_tick is not None:
        add_metric_header(lines, "anydatas_scheduler_last_tick_timestamp_seconds", "Unix timestamp of the latest scheduler loop tick.")
        lines.append(metric_line("anydatas_scheduler_last_tick_timestamp_seconds", scheduler_last_tick))

    add_metric_header(lines, "anydatas_workspaces", "Number of workspaces in the metadata database.")
    lines.append(metric_line("anydatas_workspaces", scalar_count(conn, "SELECT COUNT(*) AS count FROM workspaces")))
    add_metric_header(lines, "anydatas_workspace_members", "Number of workspace memberships in the metadata database.")
    lines.append(metric_line("anydatas_workspace_members", scalar_count(conn, "SELECT COUNT(*) AS count FROM memberships")))
    add_metric_header(lines, "anydatas_projects", "Number of analysis projects in the metadata database.")
    lines.append(metric_line("anydatas_projects", scalar_count(conn, "SELECT COUNT(*) AS count FROM projects")))
    add_metric_header(lines, "anydatas_reports", "Number of reports in the metadata database.")
    lines.append(metric_line("anydatas_reports", scalar_count(conn, "SELECT COUNT(*) AS count FROM reports")))
    add_metric_header(lines, "anydatas_audit_events", "Number of retained audit events.")
    lines.append(metric_line("anydatas_audit_events", scalar_count(conn, "SELECT COUNT(*) AS count FROM audit_events")))

    add_metric_header(lines, "anydatas_data_sources", "Number of data sources grouped by source type.")
    source_counts = grouped_counts(conn, "SELECT source_type AS name, COUNT(*) AS count FROM data_sources GROUP BY source_type")
    for source_type, count in sorted(source_counts.items()):
        lines.append(metric_line("anydatas_data_sources", count, {"source_type": source_type or "unknown"}))

    add_metric_header(lines, "anydatas_runs", "Number of retained runs grouped by current status.")
    run_counts = grouped_counts(conn, "SELECT status AS name, COUNT(*) AS count FROM runs GROUP BY status")
    for status in RUN_STATUSES:
        lines.append(metric_line("anydatas_runs", run_counts.get(status, 0), {"status": status}))
    duration_row = conn.execute(
        """
        SELECT COUNT(duration_ms) AS count, COALESCE(SUM(duration_ms), 0) AS total_duration_ms
        FROM runs
        WHERE duration_ms IS NOT NULL
        """
    ).fetchone()
    add_metric_header(lines, "anydatas_retained_run_duration_seconds", "Aggregate duration of retained completed runs.")
    lines.append(metric_line("anydatas_retained_run_duration_seconds", float(duration_row["total_duration_ms"] or 0) / 1000))
    add_metric_header(lines, "anydatas_retained_run_duration_samples", "Number of retained completed runs with a duration.")
    lines.append(metric_line("anydatas_retained_run_duration_samples", int(duration_row["count"] or 0)))

    add_metric_header(lines, "anydatas_schedules", "Number of schedules grouped by active state.")
    schedule_counts = grouped_counts(
        conn,
        "SELECT CASE WHEN is_active = 1 THEN 'active' ELSE 'paused' END AS name, COUNT(*) AS count FROM schedules GROUP BY is_active",
    )
    for state in ("active", "paused"):
        lines.append(metric_line("anydatas_schedules", schedule_counts.get(state, 0), {"state": state}))

    add_metric_header(lines, "anydatas_notifications", "Number of in-app notifications grouped by read state.")
    notification_counts = grouped_counts(
        conn,
        "SELECT CASE WHEN is_read = 1 THEN 'read' ELSE 'unread' END AS name, COUNT(*) AS count FROM notifications GROUP BY is_read",
    )
    for state in ("unread", "read"):
        lines.append(metric_line("anydatas_notifications", notification_counts.get(state, 0), {"state": state}))

    add_metric_header(lines, "anydatas_notification_deliveries", "Number of notification deliveries grouped by current status.")
    delivery_counts = grouped_counts(
        conn,
        "SELECT status AS name, COUNT(*) AS count FROM notification_deliveries GROUP BY status",
    )
    for status in DELIVERY_STATUSES:
        lines.append(metric_line("anydatas_notification_deliveries", delivery_counts.get(status, 0), {"status": status}))
    return "\n".join(lines) + "\n"
