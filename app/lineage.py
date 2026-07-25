from __future__ import annotations

from typing import Any


def data_source_impact(conn, workspace_id: str, source_id: str) -> dict[str, Any]:
    projects = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                project.id,
                project.name,
                project.language,
                project.data_source_id = ? AS draft_uses_source,
                published.data_source_id = ? AS published_uses_source,
                published.version_number AS published_version_number,
                (
                    SELECT COUNT(*)
                    FROM project_versions version
                    WHERE version.project_id = project.id AND version.data_source_id = ?
                ) AS version_count,
                CASE WHEN published.data_source_id = ? THEN (
                    SELECT COUNT(*) FROM schedules schedule
                    WHERE schedule.project_id = project.id AND schedule.is_active = 1
                ) ELSE 0 END AS active_schedule_count,
                CASE WHEN published.data_source_id = ? THEN (
                    SELECT COUNT(*) FROM reports report
                    WHERE report.project_id = project.id AND report.workspace_id = ?
                ) ELSE 0 END AS report_count
            FROM projects project
            LEFT JOIN project_versions published ON published.id = project.published_version_id
            WHERE project.workspace_id = ?
              AND (
                project.data_source_id = ?
                OR published.data_source_id = ?
                OR EXISTS (
                    SELECT 1 FROM project_versions version
                    WHERE version.project_id = project.id AND version.data_source_id = ?
                )
              )
            ORDER BY project.name, project.id
            """,
            (
                source_id,
                source_id,
                source_id,
                source_id,
                source_id,
                workspace_id,
                workspace_id,
                source_id,
                source_id,
                source_id,
            ),
        ).fetchall()
    ]
    schedules = [
        dict(row)
        for row in conn.execute(
            """
            SELECT schedule.id, schedule.name, schedule.schedule_type, schedule.is_active,
                   schedule.next_run_at, project.id AS project_id, project.name AS project_name
            FROM schedules schedule
            JOIN projects project ON project.id = schedule.project_id
            JOIN project_versions published ON published.id = project.published_version_id
            WHERE project.workspace_id = ? AND published.data_source_id = ?
            ORDER BY schedule.is_active DESC, schedule.name
            """,
            (workspace_id, source_id),
        ).fetchall()
    ]
    reports = [
        dict(row)
        for row in conn.execute(
            """
            SELECT report.*,
                   project.id AS project_id, project.name AS project_name
            FROM reports report
            JOIN projects project ON project.id = report.project_id
            JOIN project_versions published ON published.id = project.published_version_id
            WHERE report.workspace_id = ? AND published.data_source_id = ?
            ORDER BY report.title, report.id
            """,
            (workspace_id, source_id),
        ).fetchall()
    ]
    run_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM runs run
        JOIN projects project ON project.id = run.project_id
        LEFT JOIN project_versions version ON version.id = run.project_version_id
        WHERE project.workspace_id = ?
          AND COALESCE(version.data_source_id, project.data_source_id) = ?
        """,
        (workspace_id, source_id),
    ).fetchone()[0]
    return {
        "projects": projects,
        "schedules": schedules,
        "reports": reports,
        "run_count": int(run_count),
        "active_schedule_count": sum(1 for schedule in schedules if int(schedule["is_active"])),
    }
