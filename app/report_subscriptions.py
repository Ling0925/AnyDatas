from __future__ import annotations

from typing import Any

from .data_source_access import can_query_data_source_for_member
from .db import decode_json, record_notification


REPORT_NOTIFICATION_EVENT_TYPES = {"report.refresh_succeeded", "report.refresh_failed"}


def available_subscription_channels(conn, workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, channel_type, event_types_json
        FROM notification_channels
        WHERE workspace_id = ? AND is_active = 1
        ORDER BY name ASC
        """,
        (workspace_id,),
    ).fetchall()
    channels = []
    for row in rows:
        event_types = decode_json(row["event_types_json"], [])
        if isinstance(event_types, list) and REPORT_NOTIFICATION_EVENT_TYPES.intersection(event_types):
            channels.append(dict(row))
    return channels


def selected_subscription_channel_ids(conn, report_id: str, user_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT channel_id FROM report_subscription_channels WHERE report_id = ? AND user_id = ?",
        (report_id, user_id),
    ).fetchall()
    return {row["channel_id"] for row in rows}


def set_subscription_channels(
    conn,
    report_id: str,
    user_id: str,
    workspace_id: str,
    channel_ids: list[str],
    created_at: str,
) -> list[str]:
    available_ids = {channel["id"] for channel in available_subscription_channels(conn, workspace_id)}
    selected_ids = sorted(set(channel_ids))
    if any(channel_id not in available_ids for channel_id in selected_ids):
        raise ValueError("Select active report notification channels from this workspace.")
    conn.execute(
        "DELETE FROM report_subscription_channels WHERE report_id = ? AND user_id = ?",
        (report_id, user_id),
    )
    conn.executemany(
        """
        INSERT INTO report_subscription_channels (report_id, user_id, workspace_id, channel_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, user_id, workspace_id, channel_id, created_at) for channel_id in selected_ids],
    )
    return selected_ids


def notify_report_subscribers(conn, report: Any, run: Any) -> int:
    status = str(run["status"])
    if status == "succeeded":
        event_type = "report.refresh_succeeded"
        title = f"Report refreshed: {report['title']}"
        message = f"Run {run['id'][:8]} refreshed the report successfully."
        severity = "success"
    elif status == "failed":
        event_type = "report.refresh_failed"
        title = f"Report refresh failed: {report['title']}"
        message = run["error"] or "Report refresh failed without an error message."
        severity = "error"
    else:
        return 0

    source = conn.execute(
        """
        SELECT data_source.*
        FROM runs run
        JOIN projects project ON project.id = run.project_id
        LEFT JOIN project_versions project_version ON project_version.id = run.project_version_id
        JOIN data_sources data_source ON data_source.id = COALESCE(project_version.data_source_id, project.data_source_id)
        WHERE run.id = ? AND project.workspace_id = ?
        """,
        (run["id"], report["workspace_id"]),
    ).fetchone()
    if source is None:
        return 0

    subscribers = conn.execute(
        """
        SELECT subscription.user_id, member.role
        FROM report_subscriptions subscription
        JOIN memberships member
          ON member.user_id = subscription.user_id
          AND member.workspace_id = subscription.workspace_id
        WHERE subscription.report_id = ? AND subscription.workspace_id = ?
        ORDER BY subscription.created_at ASC
        """,
        (report["id"], report["workspace_id"]),
    ).fetchall()
    delivered = 0
    for subscriber in subscribers:
        if not can_query_data_source_for_member(
            conn,
            report["workspace_id"],
            subscriber["user_id"],
            subscriber["role"],
            source,
        ):
            continue
        record_notification(
            conn,
            report["workspace_id"],
            event_type,
            title,
            message,
            severity,
            "report",
            report["id"],
            subscriber["user_id"],
            f"{event_type}:report:{report['id']}:run:{run['id']}",
        )
        delivered += 1
    return delivered
