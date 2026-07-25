from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


RUN_STATUSES = ("queued", "running", "canceling", "succeeded", "failed", "canceled")
RUN_TRIGGER_TYPES = (
    "manual",
    "schedule",
    "schedule_manual",
    "schedule_retry",
    "schedule_backfill",
    "schedule_backfill_retry",
    "report_refresh",
)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class RunSearchFilters:
    query: str = ""
    status: str = ""
    trigger_type: str = ""
    project_id: str = ""
    started_from: str = ""
    started_to: str = ""

    @classmethod
    def parse(
        cls,
        query: str = "",
        status: str = "",
        trigger_type: str = "",
        project_id: str = "",
        started_from: str = "",
        started_to: str = "",
    ) -> "RunSearchFilters":
        normalized_status = status.strip().lower()
        normalized_trigger = trigger_type.strip().lower()
        if normalized_status and normalized_status not in RUN_STATUSES:
            raise ValueError("Unknown run status filter.")
        if normalized_trigger and normalized_trigger not in RUN_TRIGGER_TYPES:
            raise ValueError("Unknown run trigger filter.")
        return cls(
            query=query.strip()[:200],
            status=normalized_status,
            trigger_type=normalized_trigger,
            project_id=project_id.strip(),
            started_from=normalize_timestamp(started_from, end_of_day=False),
            started_to=normalize_timestamp(started_to, end_of_day=True),
        )


def normalize_timestamp(value: str, end_of_day: bool) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Run date filters must use an ISO date or timestamp.") from exc
    if len(normalized) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59) if end_of_day else parsed
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def log_excerpt(logs: str, error: str, query: str) -> str:
    candidates = [line.strip() for line in f"{error}\n{logs}".splitlines() if line.strip()]
    if not candidates:
        return ""
    if query:
        lowered = query.casefold()
        for line in candidates:
            if lowered in line.casefold():
                return line[:300]
    return candidates[-1][:300]


def search_workspace_runs(
    conn,
    workspace_id: str,
    source_ids: Iterable[str],
    filters: RunSearchFilters,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    visible_source_ids = sorted(set(source_ids))
    bounded_page = max(int(page), 1)
    bounded_page_size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
    if not visible_source_ids:
        return {"items": [], "page": 1, "page_size": bounded_page_size, "total": 0, "pages": 1}

    source_placeholders = ", ".join("?" for _ in visible_source_ids)
    conditions = ["p.workspace_id = ?", f"COALESCE(pv.data_source_id, p.data_source_id) IN ({source_placeholders})"]
    parameters: list[Any] = [workspace_id, *visible_source_ids]
    if filters.status:
        conditions.append("r.status = ?")
        parameters.append(filters.status)
    if filters.trigger_type:
        conditions.append("r.trigger_type = ?")
        parameters.append(filters.trigger_type)
    if filters.project_id:
        conditions.append("r.project_id = ?")
        parameters.append(filters.project_id)
    if filters.started_from:
        conditions.append("r.started_at >= ?")
        parameters.append(filters.started_from)
    if filters.started_to:
        conditions.append("r.started_at <= ?")
        parameters.append(filters.started_to)
    if filters.query:
        conditions.append("(r.id LIKE ? OR p.name LIKE ? OR r.logs LIKE ? OR r.error LIKE ?)")
        pattern = f"%{filters.query}%"
        parameters.extend([pattern, pattern, pattern, pattern])

    where_clause = " AND ".join(conditions)
    joins = """
        FROM runs r
        JOIN projects p ON p.id = r.project_id
        LEFT JOIN project_versions pv ON pv.id = r.project_version_id
    """
    total = int(
        conn.execute(f"SELECT COUNT(*) AS count {joins} WHERE {where_clause}", parameters).fetchone()["count"]
    )
    pages = max((total + bounded_page_size - 1) // bounded_page_size, 1)
    bounded_page = min(bounded_page, pages)
    rows = conn.execute(
        f"""
        SELECT
            r.id,
            r.project_id,
            r.project_version_id,
            r.schedule_id,
            r.scheduled_for_at,
            r.status,
            r.trigger_type,
            r.attempt,
            r.retry_of_run_id,
            r.next_attempt_at,
            r.started_at,
            r.finished_at,
            r.duration_ms,
            r.error,
            r.logs,
            p.name AS project_name,
            COALESCE(pv.data_source_id, p.data_source_id) AS data_source_id
        {joins}
        WHERE {where_clause}
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT ? OFFSET ?
        """,
        (*parameters, bounded_page_size, (bounded_page - 1) * bounded_page_size),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["log_excerpt"] = log_excerpt(item.get("logs", ""), item.get("error", ""), filters.query)
        item.pop("logs", None)
        items.append(item)
    return {
        "items": items,
        "page": bounded_page,
        "page_size": bounded_page_size,
        "total": total,
        "pages": pages,
    }
