from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .auth import RequestContext


DATA_SOURCE_VISIBILITIES = frozenset({"workspace", "private"})
DATA_SOURCE_PERMISSIONS = frozenset({"view", "query", "manage"})
DATA_SOURCE_PERMISSION_RANK = {"view": 1, "query": 2, "manage": 3}
DATA_SOURCE_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


def record_value(record: Any, key: str, default: Any = None) -> Any:
    try:
        value = record[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def data_source_access_level_for_member(conn, workspace_id: str, user_id: str, role: str, source: Any) -> str | None:
    """Return a member's effective source permission without exposing other workspaces."""
    if record_value(source, "workspace_id") != workspace_id:
        return None
    if role in {"owner", "admin"}:
        return "manage"

    creator_id = record_value(source, "created_by_user_id")
    if creator_id == user_id:
        return "manage"

    visibility = record_value(source, "visibility", "workspace")
    if visibility != "private":
        # Sources created before per-source access control were editable by all analysts.
        if not creator_id and role == "analyst":
            return "manage"
        return "query"

    grant = conn.execute(
        """
        SELECT permission
        FROM data_source_access_grants
        WHERE data_source_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (record_value(source, "id"), workspace_id, user_id),
    ).fetchone()
    permission = record_value(grant, "permission") if grant is not None else None
    return permission if permission in DATA_SOURCE_PERMISSIONS else None


def data_source_access_level(conn, context: RequestContext, source: Any) -> str | None:
    return data_source_access_level_for_member(conn, context.workspace_id, context.user_id, context.role, source)


def can_query_data_source_for_member(conn, workspace_id: str, user_id: str, role: str, source: Any) -> bool:
    level = data_source_access_level_for_member(conn, workspace_id, user_id, role, source)
    return level is not None and DATA_SOURCE_PERMISSION_RANK[level] >= DATA_SOURCE_PERMISSION_RANK["query"]


def has_data_source_access(conn, context: RequestContext, source: Any, minimum_permission: str) -> bool:
    required_rank = DATA_SOURCE_PERMISSION_RANK[minimum_permission]
    level = data_source_access_level(conn, context, source)
    return level is not None and DATA_SOURCE_PERMISSION_RANK[level] >= required_rank


def can_view_data_source(conn, context: RequestContext, source: Any) -> bool:
    return has_data_source_access(conn, context, source, "view")


def can_query_data_source(conn, context: RequestContext, source: Any) -> bool:
    return has_data_source_access(conn, context, source, "query")


def can_manage_data_source(conn, context: RequestContext, source: Any) -> bool:
    return has_data_source_access(conn, context, source, "manage")


def can_export_data_source(conn, context: RequestContext, source: Any) -> bool:
    """Restrict exports from the highest classification without blocking analysis."""
    if not can_query_data_source(conn, context, source):
        return False
    return record_value(source, "classification", "internal") != "restricted" or can_manage_data_source(conn, context, source)


def require_data_source_access(conn, context: RequestContext, source: Any, minimum_permission: str) -> None:
    if minimum_permission not in DATA_SOURCE_PERMISSION_RANK:
        raise ValueError("Unsupported data source permission")
    if has_data_source_access(conn, context, source, minimum_permission):
        return
    if minimum_permission == "manage" and can_view_data_source(conn, context, source):
        raise HTTPException(status_code=403, detail="Data source manage access required")
    raise HTTPException(status_code=404, detail="Data source not found")


def require_data_source_export_access(conn, context: RequestContext, source: Any) -> None:
    require_data_source_access(conn, context, source, "query")
    if not can_export_data_source(conn, context, source):
        raise HTTPException(status_code=403, detail="Restricted data source exports require manage access")
