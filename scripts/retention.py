#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backup import DATABASE_FILENAME, artifact_filename, default_data_dir


TERMINAL_JOB_STATUSES = ("succeeded", "failed", "canceled")


def retention_candidates(connection: sqlite3.Connection, cutoff: str) -> list[sqlite3.Row]:
    """选出过期且仍持有完整结果的后台任务，保留任务本身作为审计记录。"""
    return connection.execute(
        """
        SELECT id, result_artifact_key, result_size_bytes
        FROM jobs
        WHERE status IN ('succeeded', 'failed', 'canceled')
          AND result_artifact_key IS NOT NULL
          AND datetime(COALESCE(result_expires_at, finished_at, updated_at)) < datetime(?)
        ORDER BY COALESCE(finished_at, updated_at), id
        """,
        (cutoff,),
    ).fetchall()


def apply_retention(
    data_dir: Path,
    keep_days: int,
    force: bool = False,
    now: datetime | None = None,
) -> dict:
    """预览或清理旧任务结果；默认只读，显式 `--force` 后才修改数据库和产物文件。"""
    if keep_days <= 0:
        raise ValueError("Retention keep days must be positive.")
    data_dir = data_dir.expanduser().resolve()
    database_path = data_dir / DATABASE_FILENAME
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database_path}")
    effective_now = now or datetime.now(timezone.utc)
    cutoff = (effective_now - timedelta(days=keep_days)).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        candidates = retention_candidates(connection, cutoff)
        artifacts = [
            data_dir / "job-results" / artifact_filename(row["result_artifact_key"], "job result")
            for row in candidates
        ]
        result = {
            "mode": "applied" if force else "preview",
            "keep_days": keep_days,
            "cutoff": cutoff,
            "job_results": len(candidates),
            "existing_artifacts": sum(path.is_file() for path in artifacts),
            "bytes_reclaimable": sum(
                path.stat().st_size for path in artifacts if path.is_file()
            ),
            "failed_artifacts": [],
        }
        if not force:
            return result

        updated_at = effective_now.isoformat()
        for row, artifact in zip(candidates, artifacts):
            try:
                artifact.unlink(missing_ok=True)
            except OSError:
                result["failed_artifacts"].append(str(artifact))
                continue
            connection.execute(
                """
                UPDATE jobs
                SET result_json = NULL, result_artifact_key = NULL,
                    result_artifact_format = NULL, result_size_bytes = NULL,
                    result_expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (updated_at, row["id"]),
            )
        connection.commit()
    return result


def main() -> None:
    """执行命令行保留策略，并在文件删除失败时返回非零状态供运维发现。"""
    parser = argparse.ArgumentParser(description="Preview or apply AnyDatas job-result retention.")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--keep-days", type=int, default=30)
    parser.add_argument("--force", action="store_true", help="Apply retention instead of previewing it")
    args = parser.parse_args()
    try:
        result = apply_retention(args.data_dir, args.keep_days, force=args.force)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["failed_artifacts"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
