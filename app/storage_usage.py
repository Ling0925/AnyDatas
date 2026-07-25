from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import UPLOAD_DIR, decode_json


MEBIBYTE = 1024 * 1024
DEFAULT_WORKSPACE_STORAGE_BYTES = 10 * 1024 * MEBIBYTE


def managed_file_size(value: str) -> int:
    if not value:
        return 0
    path = Path(value).resolve()
    upload_root = UPLOAD_DIR.resolve()
    if upload_root not in path.parents or not path.is_file():
        return 0
    return path.stat().st_size


def source_storage_paths(source: Any) -> set[str]:
    paths = {str(source["path"])} if source["path"] else set()
    connection = decode_json(source["connection_json"], {})
    if isinstance(connection, dict) and isinstance(connection.get("original_path"), str):
        paths.add(connection["original_path"])
    return {path for path in paths if path}


def source_storage_bytes(source: Any) -> int:
    return sum(managed_file_size(path) for path in source_storage_paths(source))


def workspace_storage_bytes(conn, workspace_id: str, exclude_source_id: str = "") -> int:
    rows = conn.execute(
        "SELECT id, path, connection_json FROM data_sources WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    paths: set[str] = set()
    for source in rows:
        if exclude_source_id and source["id"] == exclude_source_id:
            continue
        paths.update(source_storage_paths(source))
    return sum(managed_file_size(path) for path in paths)


def paths_storage_bytes(paths: list[Path]) -> int:
    unique_paths = {path.resolve() for path in paths}
    return sum(path.stat().st_size for path in unique_paths if path.is_file())


def ensure_workspace_storage_capacity(current_bytes: int, incoming_bytes: int, limit_bytes: int) -> None:
    if current_bytes + incoming_bytes > limit_bytes:
        raise ValueError(
            f"Workspace storage limit exceeded: {current_bytes + incoming_bytes} bytes would exceed {limit_bytes} bytes."
        )
