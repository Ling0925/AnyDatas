#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backup import DATABASE_FILENAME, default_data_dir


PRUNED_LOG_MESSAGE = "Run payload pruned by retention policy."
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "canceled")


def safe_run_directory(data_dir: Path, run_id: str) -> Path:
    run_root = (data_dir / "runs").resolve()
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError(f"Unsafe run directory for retention: {run_id}")
    return run_root / run_id


def retention_candidates(conn: sqlite3.Connection, cutoff: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    runs = conn.execute(
        """
        SELECT r.id, p.workspace_id
        FROM runs r
        JOIN projects p ON p.id = r.project_id
        WHERE r.status IN ('succeeded', 'failed', 'canceled')
          AND datetime(COALESCE(r.finished_at, r.started_at)) < datetime(?)
          AND (r.result_json IS NOT NULL OR r.logs != ? OR r.error IS NOT NULL)
        ORDER BY r.started_at
        """,
        (cutoff, PRUNED_LOG_MESSAGE),
    ).fetchall()
    snapshots = conn.execute(
        """
        SELECT rs.id, rs.workspace_id
        FROM report_snapshots rs
        WHERE datetime(rs.created_at) < datetime(?)
          AND rs.id != (
            SELECT latest.id
            FROM report_snapshots latest
            WHERE latest.report_id = rs.report_id
            ORDER BY latest.created_at DESC, latest.id DESC
            LIMIT 1
          )
          AND rs.id != COALESCE((
            SELECT successful.id
            FROM report_snapshots successful
            WHERE successful.report_id = rs.report_id AND successful.status = 'succeeded'
            ORDER BY successful.created_at DESC, successful.id DESC
            LIMIT 1
          ), '')
        ORDER BY rs.created_at
        """,
        (cutoff,),
    ).fetchall()
    return runs, snapshots


def apply_retention(data_dir: Path, keep_days: int, force: bool = False, now: datetime | None = None) -> dict:
    if keep_days <= 0:
        raise ValueError("Retention keep days must be positive.")
    data_dir = data_dir.expanduser().resolve()
    database_path = data_dir / DATABASE_FILENAME
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database_path}")
    effective_now = now or datetime.now(timezone.utc)
    cutoff = (effective_now - timedelta(days=keep_days)).isoformat()
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        runs, snapshots = retention_candidates(conn, cutoff)
        run_ids = [run["id"] for run in runs]
        snapshot_ids = [snapshot["id"] for snapshot in snapshots]
        existing_run_directories = [
            str(run_directory)
            for run_id in run_ids
            if (run_directory := safe_run_directory(data_dir, run_id)).exists()
        ]
        result = {
            "mode": "applied" if force else "preview",
            "keep_days": keep_days,
            "cutoff": cutoff,
            "run_payloads": len(run_ids),
            "report_snapshots": len(snapshot_ids),
            "run_directories": len(existing_run_directories),
            "failed_run_directories": [],
        }
        if not force:
            return result

        if snapshot_ids:
            conn.executemany("DELETE FROM report_snapshots WHERE id = ?", [(snapshot_id,) for snapshot_id in snapshot_ids])
        if run_ids:
            conn.executemany(
                "UPDATE runs SET result_json = NULL, logs = ?, error = NULL WHERE id = ?",
                [(PRUNED_LOG_MESSAGE, run_id) for run_id in run_ids],
            )
        run_counts = Counter(run["workspace_id"] for run in runs)
        snapshot_counts = Counter(snapshot["workspace_id"] for snapshot in snapshots)
        for workspace_id in sorted(set(run_counts) | set(snapshot_counts)):
            conn.execute(
                """
                INSERT INTO audit_events (id, workspace_id, action, resource_type, resource_id, detail_json, created_at)
                VALUES (?, ?, 'system.retention_applied', 'workspace', ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    workspace_id,
                    workspace_id,
                    json.dumps(
                        {
                            "keep_days": keep_days,
                            "cutoff": cutoff,
                            "run_payloads": run_counts[workspace_id],
                            "report_snapshots": snapshot_counts[workspace_id],
                        },
                        separators=(",", ":"),
                    ),
                    effective_now.isoformat(),
                ),
            )

    for directory_name in existing_run_directories:
        run_directory = Path(directory_name)
        try:
            if run_directory.is_symlink():
                run_directory.unlink()
            else:
                shutil.rmtree(run_directory)
        except OSError:
            result["failed_run_directories"].append(str(run_directory))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or apply AnyDatas single-server runtime retention.")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--keep-days", type=int, default=90)
    parser.add_argument("--force", action="store_true", help="Apply retention instead of previewing it")
    args = parser.parse_args()
    try:
        result = apply_retention(args.data_dir, args.keep_days, force=args.force)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["failed_run_directories"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
