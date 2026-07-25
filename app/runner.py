from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Row
from typing import Any, Optional

from .clickhouse_tools import (
    parse_clickhouse_connection_url,
    parse_clickhouse_identifier,
    rewrite_clickhouse_parameters,
    validate_clickhouse_read_only_sql,
)
from .db import DATA_DIR, RUN_DIR, connect, decode_json, encode_json, record_audit, record_notification
from .mysql_tools import parse_mysql_connection_url, parse_mysql_identifier, validate_mysql_read_only_sql
from .postgres_tools import (
    parse_postgres_connection_url,
    parse_postgres_identifier,
    validate_postgres_read_only_sql,
)
from .report_subscriptions import notify_report_subscribers
from .runtime_profiles import runtime_profile_for_project
from .s3_tools import parse_s3_bucket, parse_s3_object_key
from .secret_tools import (
    data_source_secret_environment_name,
    parse_secret_bindings,
    redact_result,
    redact_text,
    remove_unbound_secret_sources,
    resolve_secret_reference_value,
    resolve_secret_values,
)
from .sql_tools import rewrite_dollar_parameters


RUNNER_TIMEOUT_SECONDS = int(os.getenv("ANYDATAS_RUN_TIMEOUT_SECONDS", "45"))
DEFAULT_MAX_CONCURRENT_RUNS = 2
REPORT_SNAPSHOT_TRIGGERS = {"schedule", "schedule_manual", "schedule_retry"}
SCHEDULE_RETRY_TRIGGERS = {"schedule", "schedule_retry", "schedule_backfill", "schedule_backfill_retry"}
SCHEDULE_SLOT_TRIGGERS = {"schedule", "schedule_backfill", "schedule_retry", "schedule_backfill_retry"}
RUN_CONTROL_LOCK = threading.Lock()
LOCAL_RUN_PROCESSES: dict[str, subprocess.Popen[str]] = {}
CANCEL_REQUESTED_RUN_IDS: set[str] = set()


class RunCanceled(RuntimeError):
    pass


def request_run_cancellation(run_id: str) -> None:
    with RUN_CONTROL_LOCK:
        CANCEL_REQUESTED_RUN_IDS.add(run_id)


def is_run_cancellation_requested(run_id: str) -> bool:
    with RUN_CONTROL_LOCK:
        return run_id in CANCEL_REQUESTED_RUN_IDS


def clear_run_cancellation(run_id: str) -> None:
    with RUN_CONTROL_LOCK:
        CANCEL_REQUESTED_RUN_IDS.discard(run_id)


def cancel_run_execution(run_id: str) -> bool:
    request_run_cancellation(run_id)
    return get_runner().cancel(run_id)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_max_concurrent_runs() -> int:
    configured = os.getenv("ANYDATAS_DEFAULT_MAX_CONCURRENT_RUNS", str(DEFAULT_MAX_CONCURRENT_RUNS))
    try:
        return max(int(configured), 0)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_RUNS


def claim_run_execution(run_id: str, started_at: Optional[str] = None) -> bool:
    """Atomically move one queued run into a workspace execution slot."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            """
            SELECT r.id, r.status, r.schedule_id, r.trigger_type, p.workspace_id
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None or run["status"] != "queued":
            return False
        if run["schedule_id"] and run["trigger_type"] in SCHEDULE_SLOT_TRIGGERS:
            active_schedule_run = conn.execute(
                """
                SELECT 1
                FROM runs
                WHERE schedule_id = ?
                  AND id != ?
                  AND status IN ('running', 'canceling')
                LIMIT 1
                """,
                (run["schedule_id"], run_id),
            ).fetchone()
            if active_schedule_run is not None:
                return False

        quota = conn.execute(
            "SELECT max_concurrent_runs FROM workspace_quotas WHERE workspace_id = ?",
            (run["workspace_id"],),
        ).fetchone()
        max_concurrent_runs = max(int(quota["max_concurrent_runs"]), 0) if quota is not None else default_max_concurrent_runs()
        active_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            WHERE p.workspace_id = ?
              AND r.status IN ('running', 'canceling')
            """,
            (run["workspace_id"],),
        ).fetchone()["count"]
        if int(active_count) >= max_concurrent_runs:
            return False

        claimed = conn.execute(
            """
            UPDATE runs
            SET status = 'running', started_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (started_at or now_iso(), run_id),
        )
        return claimed.rowcount == 1


def create_report_snapshot(conn, report_id: str, workspace_id: str, run) -> str:
    snapshot_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO report_snapshots (id, workspace_id, report_id, run_id, status, result_json, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            workspace_id,
            report_id,
            run["id"],
            run["status"],
            run["result_json"] if run["status"] == "succeeded" else None,
            run["error"],
            now_iso(),
        ),
    )
    return snapshot_id


def create_run(project_id: str, trigger_type: str, schedule_id: Optional[str] = None) -> str:
    run_id = uuid.uuid4().hex
    with connect() as conn:
        project = conn.execute(
            "SELECT id, workspace_id, parameters_json, runtime_profile FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            raise ValueError("Project not found")
        version = select_run_version(conn, project_id)
        parameters = decode_json(version["parameters_json"] if version is not None else project["parameters_json"], {})
        if not isinstance(parameters, dict):
            parameters = {}
        parameters_json = encode_json(parameters)
        secret_bindings = parse_secret_bindings(version["secret_bindings_json"] if version is not None else "[]")
        secret_bindings_json = encode_json(secret_bindings)
        conn.execute(
            """
            INSERT INTO runs (
                id, project_id, project_version_id, status, trigger_type, schedule_id,
                scheduled_for_at, parameters_json, secret_bindings_json, started_at
            )
            VALUES (?, ?, ?, 'queued', ?, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                version["id"] if version else None,
                trigger_type,
                schedule_id,
                parameters_json,
                secret_bindings_json,
                now_iso(),
            ),
        )
        record_audit(
            conn,
            "run.queued",
            "run",
            run_id,
            {
                "project_id": project_id,
                "project_version_id": version["id"] if version else None,
                "trigger_type": trigger_type,
                "schedule_id": schedule_id,
                "runtime_profile": (
                    version["runtime_profile"] if version is not None else project["runtime_profile"]
                ),
                "parameter_names": sorted(parameters),
                "secret_binding_count": len(secret_bindings),
            },
            project["workspace_id"],
        )
    return run_id


def select_run_version(conn, project_id: str):
    version = conn.execute(
        """
        SELECT pv.*
        FROM projects p
        JOIN project_versions pv ON pv.id = p.published_version_id
        WHERE p.id = ?
        """,
        (project_id,),
    ).fetchone()
    if version is not None:
        return version
    return conn.execute(
        """
        SELECT *
        FROM project_versions
        WHERE project_id = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()


def run_project(project_id: str, trigger_type: str = "manual", schedule_id: Optional[str] = None) -> str:
    run_id = create_run(project_id, trigger_type, schedule_id)
    execute_run(run_id)
    return run_id


def prepare_runtime_source(conn, workspace_id: str, source) -> tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]:
    runtime_source = dict(source)
    connection = decode_json(source["connection_json"], {})
    if not isinstance(connection, dict):
        raise ValueError("Data source connection metadata is invalid.")
    source_type = source["source_type"]
    if source_type == "s3":
        if connection.get("driver") != "s3":
            raise ValueError("S3 data source connection metadata is invalid.")
        runtime_format = connection.get("runtime_format")
        if runtime_format not in {"csv", "parquet"}:
            raise ValueError("S3 data source runtime format is invalid.")
        bucket = connection.get("bucket")
        object_key = connection.get("object_key")
        if not isinstance(bucket, str) or not isinstance(object_key, str):
            raise ValueError("S3 data source object metadata is missing.")
        runtime_source["connection_json"] = encode_json(
            {
                "driver": "s3",
                "runtime_format": runtime_format,
                "bucket": parse_s3_bucket(bucket),
                "object_key": parse_s3_object_key(object_key),
                "etag": connection.get("etag", ""),
                "version_id": connection.get("version_id", ""),
            }
        )
        return runtime_source, {}, []
    if source_type not in {"postgres", "mysql", "clickhouse"}:
        return runtime_source, {}, []

    secret_id = connection.get("secret_id")
    table = connection.get("table")
    expected_environment_name = data_source_secret_environment_name(source["id"])
    if not isinstance(secret_id, str) or not secret_id:
        raise ValueError(f"{source_type.title()} data source is missing its connection reference.")
    if connection.get("url_environment") != expected_environment_name:
        raise ValueError(f"{source_type.title()} data source has invalid runtime connection metadata.")
    if not isinstance(table, str):
        raise ValueError(f"{source_type.title()} data source is missing table metadata.")
    if source_type == "postgres":
        schema = connection.get("schema")
        if not isinstance(schema, str):
            raise ValueError("PostgreSQL data source is missing schema metadata.")
        namespace = parse_postgres_identifier(schema, "schema")
        table = parse_postgres_identifier(table, "table")
        parse_connection = parse_postgres_connection_url
        namespace_key = "schema"
    elif source_type == "mysql":
        database = connection.get("database")
        if not isinstance(database, str):
            raise ValueError("MySQL data source is missing database metadata.")
        namespace = parse_mysql_identifier(database, "database")
        table = parse_mysql_identifier(table, "table")
        parse_connection = parse_mysql_connection_url
        namespace_key = "database"
    else:
        database = connection.get("database")
        if not isinstance(database, str):
            raise ValueError("ClickHouse data source is missing database metadata.")
        namespace = parse_clickhouse_identifier(database, "database")
        table = parse_clickhouse_identifier(table, "table")
        parse_connection = parse_clickhouse_connection_url
        namespace_key = "database"
    value, reference = resolve_secret_reference_value(conn, workspace_id, secret_id)
    parse_connection(value)
    runtime_source["connection_json"] = encode_json(
        {
            "driver": source_type,
            namespace_key: namespace,
            "table": table,
            "url_environment": expected_environment_name,
        }
    )
    return (
        runtime_source,
        {expected_environment_name: value},
        [
            {
                "secret_id": reference["secret_id"],
                "secret_name": reference["secret_name"],
                "environment_name": expected_environment_name,
            }
        ],
    )


def execute_run(run_id: str) -> None:
    started = time.monotonic()
    canceled_before_execution = False
    failure_before_execution: Optional[str] = None
    secret_values: dict[str, str] = {}
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return
    if run["status"] == "queued" and not claim_run_execution(run_id):
        return

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return
        if run["status"] == "canceled":
            clear_run_cancellation(run_id)
            return
        if run["status"] == "canceling":
            canceled_before_execution = True
        elif run["status"] != "running":
            return
        else:
            project = conn.execute("SELECT * FROM projects WHERE id = ?", (run["project_id"],)).fetchone()
            if project is None:
                failure_before_execution = "Project no longer exists"
            else:
                if run["project_version_id"]:
                    version = conn.execute("SELECT * FROM project_versions WHERE id = ?", (run["project_version_id"],)).fetchone()
                else:
                    version = None
                runnable = version if version is not None else project
                source = conn.execute("SELECT * FROM data_sources WHERE id = ?", (runnable["data_source_id"],)).fetchone()
                if source is None:
                    failure_before_execution = "Data source no longer exists"
                else:
                    try:
                        secret_bindings = parse_secret_bindings(run["secret_bindings_json"])
                        project_secret_values, resolved_secrets = resolve_secret_values(conn, project["workspace_id"], secret_bindings)
                        runtime_source, source_secret_values, resolved_source_secrets = prepare_runtime_source(
                            conn,
                            project["workspace_id"],
                            source,
                        )
                        secret_values = {**project_secret_values, **source_secret_values}
                        resolved_secrets.extend(resolved_source_secrets)
                    except (RuntimeError, ValueError) as exc:
                        failure_before_execution = str(exc)
                    else:
                        if resolved_secrets:
                            record_audit(
                                conn,
                                "run.secrets_resolved",
                                "run",
                                run_id,
                                {
                                    "secret_names": [secret["secret_name"] for secret in resolved_secrets],
                                    "environment_names": [secret["environment_name"] for secret in resolved_secrets],
                                },
                                project["workspace_id"],
                            )

    if canceled_before_execution:
        _finish_run(run_id, "canceled", "", None, "Canceled before execution.", started)
        return
    if failure_before_execution:
        _finish_run(run_id, "failed", "", None, redact_text(failure_before_execution, secret_values.values()), started)
        return

    try:
        if is_run_cancellation_requested(run_id):
            raise RunCanceled("Run canceled before execution.")
        parameters = decode_json(run["parameters_json"], {})
        if not isinstance(parameters, dict):
            parameters = {}
        result, logs = get_runner().run(runnable, runtime_source, run_id, parameters, secret_values)
        if is_run_cancellation_requested(run_id):
            raise RunCanceled("Run canceled during execution.")
        _finish_run(
            run_id,
            "succeeded",
            redact_text(logs, secret_values.values()),
            redact_result(result, secret_values.values()),
            None,
            started,
        )
    except RunCanceled as exc:
        _finish_run(run_id, "canceled", "", None, str(exc), started)
    except subprocess.TimeoutExpired as exc:
        logs = (exc.stdout or "") + (exc.stderr or "")
        _finish_run(
            run_id,
            "failed",
            redact_text(logs, secret_values.values()),
            None,
            f"Run exceeded {RUNNER_TIMEOUT_SECONDS}s timeout",
            started,
        )
    except Exception as exc:  # noqa: BLE001
        _finish_run(run_id, "failed", "", None, redact_text(str(exc), secret_values.values()), started)


def _finish_run(
    run_id: str,
    status: str,
    logs: str,
    result: Optional[dict[str, Any]],
    error: Optional[str],
    started: float,
) -> None:
    duration_ms = int((time.monotonic() - started) * 1000)
    with connect() as conn:
        run = conn.execute(
            """
            SELECT
                r.id,
                r.project_id,
                r.project_version_id,
                r.trigger_type,
                r.schedule_id,
                r.scheduled_for_at,
                r.attempt,
                r.parameters_json,
                r.secret_bindings_json,
                p.workspace_id,
                p.name AS project_name,
                s.is_active AS schedule_is_active,
                s.max_retries,
                s.retry_delay_minutes
            FROM runs r
            JOIN projects p ON p.id = r.project_id
            LEFT JOIN schedules s ON s.id = r.schedule_id
            WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        finished_at = now_iso()
        result_json = encode_json(result) if result is not None else None
        cancellation_error = error or "Canceled by user."
        conn.execute(
            """
            UPDATE runs
            SET
                status = CASE WHEN status IN ('canceling', 'canceled') THEN 'canceled' ELSE ? END,
                logs = ?,
                result_json = CASE WHEN status IN ('canceling', 'canceled') THEN NULL ELSE ? END,
                error = CASE WHEN status IN ('canceling', 'canceled') THEN ? ELSE ? END,
                finished_at = ?,
                duration_ms = ?
            WHERE id = ?
            """,
            (status, logs, result_json, cancellation_error, error, finished_at, duration_ms, run_id),
        )
        completed_run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        final_status = completed_run["status"] if completed_run is not None else status
        final_error = completed_run["error"] if completed_run is not None else error
        record_audit(
            conn,
            f"run.{final_status}",
            "run",
            run_id,
            {"duration_ms": duration_ms, "error": final_error},
            run["workspace_id"] if run else "demo-workspace",
        )
        retry_queued = False
        if (
            final_status == "failed"
            and run is not None
            and run["schedule_id"]
            and run["schedule_is_active"]
            and run["trigger_type"] in SCHEDULE_RETRY_TRIGGERS
            and int(run["attempt"] or 1) <= int(run["max_retries"] or 0)
        ):
            retry_id = uuid.uuid4().hex
            retry_attempt = int(run["attempt"] or 1) + 1
            retry_delay_minutes = min(
                int(run["retry_delay_minutes"] or 1) * (2 ** (int(run["attempt"] or 1) - 1)),
                1440,
            )
            retry_at = (datetime.fromisoformat(finished_at) + timedelta(minutes=retry_delay_minutes)).isoformat()
            conn.execute(
                """
                INSERT INTO runs (
                    id, project_id, project_version_id, status, trigger_type, schedule_id,
                    scheduled_for_at, attempt, retry_of_run_id, next_attempt_at,
                    parameters_json, secret_bindings_json, started_at
                )
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_id,
                    run["project_id"],
                    run["project_version_id"],
                    "schedule_backfill_retry" if run["trigger_type"] in {"schedule_backfill", "schedule_backfill_retry"} else "schedule_retry",
                    run["schedule_id"],
                    run["scheduled_for_at"],
                    retry_attempt,
                    run_id,
                    retry_at,
                    run["parameters_json"],
                    run["secret_bindings_json"],
                    finished_at,
                ),
            )
            record_audit(
                conn,
                "run.retry_queued",
                "run",
                retry_id,
                {
                    "schedule_id": run["schedule_id"],
                    "attempt": retry_attempt,
                    "retry_of_run_id": run_id,
                    "retry_delay_minutes": retry_delay_minutes,
                    "next_attempt_at": retry_at,
                },
                run["workspace_id"],
            )
            retry_queued = True
        if (
            run is not None
            and run["trigger_type"] in REPORT_SNAPSHOT_TRIGGERS
            and final_status != "canceled"
            and not (final_status == "failed" and retry_queued)
        ):
            reports = conn.execute(
                "SELECT id, workspace_id, project_id, title FROM reports WHERE project_id = ? AND workspace_id = ?",
                (run["project_id"], run["workspace_id"]),
            ).fetchall()
            for report in reports:
                snapshot_id = create_report_snapshot(conn, report["id"], run["workspace_id"], completed_run)
                conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (finished_at, report["id"]))
                subscriber_notifications = notify_report_subscribers(conn, report, completed_run)
                record_audit(
                    conn,
                    "report.snapshot_updated",
                    "report",
                    report["id"],
                    {
                        "run_id": run_id,
                        "snapshot_id": snapshot_id,
                        "status": final_status,
                        "trigger_type": run["trigger_type"],
                        "schedule_id": run["schedule_id"],
                        "subscriber_notifications": subscriber_notifications,
                    },
                    run["workspace_id"],
                )
        if final_status == "failed" and run is not None and not retry_queued:
            record_notification(
                conn,
                run["workspace_id"],
                "run.failed",
                f"Run failed: {run['project_name']}",
                final_error or logs or "Run failed without an error message.",
                "error",
                "run",
                run_id,
            )
    clear_run_cancellation(run_id)


class LocalSubprocessRunner:
    """Development runner. Production can replace this with a Docker-backed runner."""

    def run(
        self,
        project: Row,
        source: Row,
        run_id: str,
        parameters: dict[str, Any],
        secret_values: Optional[dict[str, str]] = None,
    ) -> tuple[dict[str, Any], str]:
        runtime_profile = runtime_profile_for_project(project)
        if runtime_profile["id"] != "standard":
            raise RuntimeError(f"Runtime profile {runtime_profile['id']} requires ANYDATAS_RUNNER=docker.")
        run_path, result_path, wrapper_path = prepare_run_files(
            project,
            run_id,
            source["source_type"],
            parameters,
        )

        env = remove_unbound_secret_sources(dict(os.environ))
        env.update(
            {
                "ANYDATAS_LANGUAGE": project["language"],
                "ANYDATAS_SOURCE_TYPE": source["source_type"],
                "ANYDATAS_SCRIPT": str(run_path / ("main.sql" if project["language"] == "sql" else "main.py")),
                "ANYDATAS_DATASET": source["path"],
                "ANYDATAS_CONNECTION": source["connection_json"],
                "ANYDATAS_PARAMETERS_JSON": encode_json(parameters),
                "ANYDATAS_OUTPUT": str(result_path),
                "ANYDATAS_POSTGRES_STATEMENT_TIMEOUT_MS": str(RUNNER_TIMEOUT_SECONDS * 1000),
                "ANYDATAS_MYSQL_STATEMENT_TIMEOUT_MS": str(RUNNER_TIMEOUT_SECONDS * 1000),
            }
        )
        env.update(secret_values or {})

        with RUN_CONTROL_LOCK:
            if run_id in CANCEL_REQUESTED_RUN_IDS:
                raise RunCanceled("Run canceled before local process started.")
            proc = subprocess.Popen(
                [sys.executable, str(wrapper_path)],
                cwd=str(run_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            LOCAL_RUN_PROCESSES[run_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=RUNNER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(proc.args, RUNNER_TIMEOUT_SECONDS, output=stdout, stderr=stderr) from None
        finally:
            with RUN_CONTROL_LOCK:
                LOCAL_RUN_PROCESSES.pop(run_id, None)
        if is_run_cancellation_requested(run_id):
            raise RunCanceled("Run canceled during local execution.")
        return read_runner_result(subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr), result_path)

    def cancel(self, run_id: str) -> bool:
        with RUN_CONTROL_LOCK:
            proc = LOCAL_RUN_PROCESSES.get(run_id)
        if proc is None:
            return False
        try:
            proc.terminate()
            return True
        except ProcessLookupError:
            return False


class DockerRunner:
    """Single-server production runner using Docker Engine."""

    def run(
        self,
        project: Row,
        source: Row,
        run_id: str,
        parameters: dict[str, Any],
        secret_values: Optional[dict[str, str]] = None,
    ) -> tuple[dict[str, Any], str]:
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("ANYDATAS_RUNNER=docker was requested, but docker is not installed")
        if is_run_cancellation_requested(run_id):
            raise RunCanceled("Run canceled before Docker execution started.")

        run_path, result_path, _wrapper_path = prepare_run_files(
            project,
            run_id,
            source["source_type"],
            parameters,
        )
        run_path.chmod(0o777)
        external_database_source = source["source_type"] in {"postgres", "mysql", "clickhouse"}
        source_path = Path(source["path"]).resolve() if not external_database_source else None
        host_data_dir = self.host_data_dir(docker)
        docker_run_path = docker_path(run_path, host_data_dir)
        docker_source_path = docker_path(source_path, host_data_dir) if source_path is not None else None
        source_mount = (
            ["--mount", f"type=bind,src={docker_source_path.parent},dst=/data,readonly"]
            if docker_source_path is not None
            else []
        )
        dataset_path = "" if source_path is None else f"/data/{source_path.name}"
        runtime_profile = runtime_profile_for_project(project)
        image = runtime_profile["image"]
        memory = os.getenv("ANYDATAS_DOCKER_MEMORY", "2g")
        cpus = os.getenv("ANYDATAS_DOCKER_CPUS", "1")
        tmpfs_size = os.getenv("ANYDATAS_DOCKER_TMPFS", "64m")
        runtime_user = os.getenv("ANYDATAS_DOCKER_USER", "65532:65532")
        script_name = "main.sql" if project["language"] == "sql" else "main.py"
        container_name = docker_container_name(run_id)
        database_network = os.getenv("ANYDATAS_DOCKER_DATABASE_NETWORK", "").strip() if external_database_source else "none"
        if external_database_source and not database_network:
            source_label = {
                "postgres": "PostgreSQL",
                "mysql": "MySQL",
                "clickhouse": "ClickHouse",
            }[source["source_type"]]
            raise RuntimeError(f"{source_label} data sources require ANYDATAS_DOCKER_DATABASE_NETWORK when using Docker Runner.")
        command = [
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"com.anydatas.run_id={run_id}",
            "--network",
            database_network,
            "--cpus",
            cpus,
            "--memory",
            memory,
            "--pids-limit",
            "128",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={tmpfs_size}",
            "--user",
            runtime_user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--mount",
            f"type=bind,src={docker_run_path},dst=/work",
            *source_mount,
            "-w",
            "/work",
            "-e",
            f"ANYDATAS_LANGUAGE={project['language']}",
            "-e",
            f"ANYDATAS_SOURCE_TYPE={source['source_type']}",
            "-e",
            f"ANYDATAS_SCRIPT=/work/{script_name}",
            "-e",
            f"ANYDATAS_DATASET={dataset_path}",
            "-e",
            f"ANYDATAS_CONNECTION={source['connection_json']}",
            "-e",
            f"ANYDATAS_PARAMETERS_JSON={encode_json(parameters)}",
            "-e",
            "ANYDATAS_OUTPUT=/work/result.json",
            "-e",
            f"ANYDATAS_POSTGRES_STATEMENT_TIMEOUT_MS={RUNNER_TIMEOUT_SECONDS * 1000}",
            "-e",
            f"ANYDATAS_MYSQL_STATEMENT_TIMEOUT_MS={RUNNER_TIMEOUT_SECONDS * 1000}",
            "-e",
            f"ANYDATAS_CLICKHOUSE_QUERY_TIMEOUT_SECONDS={RUNNER_TIMEOUT_SECONDS}",
            "-e",
            "HOME=/tmp",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        for environment_name, value in sorted((secret_values or {}).items()):
            command.extend(["-e", f"{environment_name}={value}"])
        command.extend([image, "python", "/work/wrapper.py"])
        try:
            if is_run_cancellation_requested(run_id):
                raise RunCanceled("Run canceled before Docker execution started.")
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=RUNNER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Killing the Docker CLI does not guarantee that its container has stopped.
            try:
                subprocess.run(
                    [docker, "rm", "--force", container_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise
        if is_run_cancellation_requested(run_id):
            raise RunCanceled("Run canceled during Docker execution.")
        return read_runner_result(proc, result_path)

    def cancel(self, run_id: str) -> bool:
        docker = shutil.which("docker")
        if docker is None:
            return False
        try:
            proc = subprocess.run(
                [docker, "rm", "--force", docker_container_name(run_id)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    @staticmethod
    def host_data_dir(docker: str) -> Optional[Path]:
        configured = os.getenv("ANYDATAS_DOCKER_HOST_DATA_DIR", "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                raise RuntimeError("ANYDATAS_DOCKER_HOST_DATA_DIR must be an absolute host path")
            return candidate

        if not is_dockerized():
            return None
        container_id = os.getenv("HOSTNAME", "").strip()
        if not container_id:
            return None
        try:
            inspect = subprocess.run(
                [
                    docker,
                    "inspect",
                    "--format",
                    f"{{{{range .Mounts}}}}{{{{if eq .Destination \"{DATA_DIR}\"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}",
                    container_id,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if inspect.returncode != 0 or not inspect.stdout.strip():
            return None
        host_path = Path(inspect.stdout.strip())
        return host_path if host_path.is_absolute() else None


def docker_path(path: Path, host_data_dir: Optional[Path]) -> Path:
    """Translate files below the app data directory to the Docker host mount."""
    if host_data_dir is None:
        return path
    try:
        return host_data_dir / path.resolve().relative_to(DATA_DIR)
    except ValueError:
        return path


def is_dockerized() -> bool:
    return Path("/.dockerenv").exists()


def docker_container_name(run_id: str) -> str:
    normalized = "".join(character if character.isalnum() or character in "_.-" else "-" for character in run_id)
    return f"anydatas-run-{normalized}"[:63]


def get_runner():
    if os.getenv("ANYDATAS_RUNNER", "local").lower() == "docker":
        return DockerRunner()
    return LocalSubprocessRunner()


def prepare_run_files(
    project: Row,
    run_id: str,
    source_type: str = "",
    parameters: Optional[dict[str, Any]] = None,
) -> tuple[Path, Path, Path]:
    run_path = RUN_DIR / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    script_path = run_path / ("main.sql" if project["language"] == "sql" else "main.py")
    result_path = run_path / "result.json"
    wrapper_path = run_path / "wrapper.py"
    script = project["script"]
    if project["language"] == "sql":
        if source_type == "postgres":
            validate_postgres_read_only_sql(script)
            script = rewrite_dollar_parameters(script)
        elif source_type == "mysql":
            validate_mysql_read_only_sql(script)
            script = rewrite_dollar_parameters(script, hash_line_comments=True)
        elif source_type == "clickhouse":
            validate_clickhouse_read_only_sql(script)
            script, _bound_parameters = rewrite_clickhouse_parameters(script, parameters or {})
    script_path.write_text(script, encoding="utf-8")
    wrapper_path.write_text(wrapper_code(), encoding="utf-8")
    return run_path, result_path, wrapper_path


def read_runner_result(proc: subprocess.CompletedProcess, result_path: Path) -> tuple[dict[str, Any], str]:
    logs = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(logs.strip() or f"Runner exited with code {proc.returncode}")
    if result_path.is_symlink():
        raise RuntimeError("Runner result must be a regular file")
    if not result_path.exists():
        return {"columns": [], "rows": [], "summary": {"rows": 0, "columns": 0}}, logs
    if not result_path.is_file():
        raise RuntimeError("Runner result must be a regular file")
    result = decode_json(result_path.read_text(encoding="utf-8"), {})
    return result, logs


def wrapper_code() -> str:
    return textwrap.dedent(
        r'''
            import csv
            import json
            import os
            import re
            from pathlib import Path
            from urllib.parse import unquote, urlparse

            language = os.environ["ANYDATAS_LANGUAGE"]
            script_path = Path(os.environ["ANYDATAS_SCRIPT"])
            dataset_path = os.environ["ANYDATAS_DATASET"]
            source_type = os.environ.get("ANYDATAS_SOURCE_TYPE", "file")
            connection = json.loads(os.environ.get("ANYDATAS_CONNECTION", "{}") or "{}")
            params = json.loads(os.environ.get("ANYDATAS_PARAMETERS_JSON", "{}") or "{}")
            output_path = Path(os.environ["ANYDATAS_OUTPUT"])

            if not isinstance(params, dict):
                raise ValueError("Run parameters must be a JSON object")

            def quote_identifier(identifier):
                return '"' + identifier.replace('"', '""') + '"'

            def sqlite_uri(path):
                return Path(path).absolute().as_uri() + "?mode=ro"

            def sqlite_table_name():
                table = connection.get("table")
                if not table:
                    raise ValueError("SQLite data source is missing table metadata")
                return table

            def duckdb_path(path):
                return path.replace("'", "''")

            def postgres_connection_url():
                environment_name = connection.get("url_environment")
                if not isinstance(environment_name, str) or not environment_name.startswith("ANYDATAS_USER_SECRET_SOURCE_"):
                    raise ValueError("PostgreSQL data source is missing runtime connection metadata")
                connection_url = os.environ.get(environment_name)
                if not connection_url:
                    raise ValueError("PostgreSQL data source connection is not configured at runtime")
                return connection_url

            def postgres_table_query():
                schema = connection.get("schema")
                table = connection.get("table")
                if not isinstance(schema, str) or not isinstance(table, str):
                    raise ValueError("PostgreSQL data source is missing schema or table metadata")
                from psycopg import sql as psycopg_sql
                return psycopg_sql.SQL("SELECT * FROM {}.{}").format(
                    psycopg_sql.Identifier(schema),
                    psycopg_sql.Identifier(table),
                )

            def configure_postgres_cursor(cursor):
                try:
                    statement_timeout_ms = int(os.environ.get("ANYDATAS_POSTGRES_STATEMENT_TIMEOUT_MS", "45000"))
                except ValueError as exc:
                    raise ValueError("PostgreSQL statement timeout must be an integer") from exc
                if statement_timeout_ms < 1 or statement_timeout_ms > 3600000:
                    raise ValueError("PostgreSQL statement timeout must be between 1 and 3600000 milliseconds")
                cursor.execute(f"SET LOCAL statement_timeout TO {statement_timeout_ms}")

            def mysql_connection_options():
                environment_name = connection.get("url_environment")
                database = connection.get("database")
                if not isinstance(environment_name, str) or not environment_name.startswith("ANYDATAS_USER_SECRET_SOURCE_"):
                    raise ValueError("MySQL data source is missing runtime connection metadata")
                if not isinstance(database, str) or not database:
                    raise ValueError("MySQL data source is missing database metadata")
                connection_url = os.environ.get(environment_name)
                if not connection_url:
                    raise ValueError("MySQL data source connection is not configured at runtime")
                parsed = urlparse(connection_url)
                if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname or not parsed.username:
                    raise ValueError("MySQL data source connection URL is invalid")
                try:
                    port = parsed.port or 3306
                except ValueError as exc:
                    raise ValueError("MySQL data source connection URL has an invalid port") from exc
                return {
                    "host": parsed.hostname,
                    "port": port,
                    "user": unquote(parsed.username),
                    "password": unquote(parsed.password or ""),
                    "database": database,
                    "charset": "utf8mb4",
                    "connect_timeout": 5,
                    "read_timeout": 5,
                    "write_timeout": 5,
                    "autocommit": False,
                }

            def quote_mysql_identifier(identifier):
                return "`" + identifier.replace("`", "``") + "`"

            def mysql_table_query():
                database = connection.get("database")
                table = connection.get("table")
                if not isinstance(database, str) or not isinstance(table, str):
                    raise ValueError("MySQL data source is missing database or table metadata")
                return f"SELECT * FROM {quote_mysql_identifier(database)}.{quote_mysql_identifier(table)}"

            def configure_mysql_cursor(cursor):
                try:
                    statement_timeout_ms = int(os.environ.get("ANYDATAS_MYSQL_STATEMENT_TIMEOUT_MS", "45000"))
                except ValueError as exc:
                    raise ValueError("MySQL statement timeout must be an integer") from exc
                if statement_timeout_ms < 1 or statement_timeout_ms > 3600000:
                    raise ValueError("MySQL statement timeout must be between 1 and 3600000 milliseconds")
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (statement_timeout_ms,))

            def begin_mysql_read_only_transaction(cursor):
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION READ ONLY")

            def clickhouse_connection_options():
                environment_name = connection.get("url_environment")
                database = connection.get("database")
                if not isinstance(environment_name, str) or not environment_name.startswith("ANYDATAS_USER_SECRET_SOURCE_"):
                    raise ValueError("ClickHouse data source is missing runtime connection metadata")
                if not isinstance(database, str) or not database:
                    raise ValueError("ClickHouse data source is missing database metadata")
                connection_url = os.environ.get(environment_name)
                if not connection_url:
                    raise ValueError("ClickHouse data source connection is not configured at runtime")
                parsed = urlparse(connection_url)
                if parsed.scheme not in {"clickhouse", "clickhouses"} or not parsed.hostname or not parsed.username:
                    raise ValueError("ClickHouse data source connection URL is invalid")
                try:
                    secure = parsed.scheme == "clickhouses"
                    port = parsed.port or (8443 if secure else 8123)
                except ValueError as exc:
                    raise ValueError("ClickHouse data source connection URL has an invalid port") from exc
                return {
                    "host": parsed.hostname,
                    "port": port,
                    "username": unquote(parsed.username),
                    "password": unquote(parsed.password or ""),
                    "database": database,
                    "secure": secure,
                    "connect_timeout": 5,
                    "send_receive_timeout": 5,
                }

            def clickhouse_query_settings():
                try:
                    timeout_seconds = int(os.environ.get("ANYDATAS_CLICKHOUSE_QUERY_TIMEOUT_SECONDS", "45"))
                except ValueError as exc:
                    raise ValueError("ClickHouse query timeout must be an integer") from exc
                if timeout_seconds < 1 or timeout_seconds > 3600:
                    raise ValueError("ClickHouse query timeout must be between 1 and 3600 seconds")
                return {
                    "readonly": 1,
                    "max_execution_time": timeout_seconds,
                    "max_result_rows": 500,
                    "result_overflow_mode": "break",
                }

            def clickhouse_table_query():
                database = connection.get("database")
                table = connection.get("table")
                if not isinstance(database, str) or not isinstance(table, str):
                    raise ValueError("ClickHouse data source is missing database or table metadata")
                return f"SELECT * FROM {quote_mysql_identifier(database)}.{quote_mysql_identifier(table)}"

            def dbapi_named_params(sql):
                names = set(re.findall(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", sql))
                return {name: params[name] for name in names if name in params}

            def sql_parameter_names(sql):
                names = set()
                index = 0
                while index < len(sql):
                    char = sql[index]
                    if char == "'":
                        index += 1
                        while index < len(sql):
                            if sql[index] == "'":
                                index += 1
                                if index < len(sql) and sql[index] == "'":
                                    index += 1
                                    continue
                                break
                            index += 1
                        continue
                    if char == '"':
                        index += 1
                        while index < len(sql):
                            if sql[index] == '"':
                                index += 1
                                if index < len(sql) and sql[index] == '"':
                                    index += 1
                                    continue
                                break
                            index += 1
                        continue
                    if sql.startswith("--", index):
                        newline = sql.find("\n", index + 2)
                        index = len(sql) if newline < 0 else newline + 1
                        continue
                    if sql.startswith("/*", index):
                        end = sql.find("*/", index + 2)
                        index = len(sql) if end < 0 else end + 2
                        continue
                    if char == "$":
                        start = index + 1
                        if start < len(sql) and (sql[start].isalpha() or sql[start] == "_"):
                            end = start + 1
                            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                                end += 1
                            if end < len(sql) and sql[end] == "$":
                                delimiter = sql[index : end + 1]
                                close = sql.find(delimiter, end + 1)
                                index = len(sql) if close < 0 else close + len(delimiter)
                                continue
                            names.add(sql[start:end])
                            index = end
                            continue
                        if start < len(sql) and sql[start] == "$":
                            close = sql.find("$$", start + 1)
                            index = len(sql) if close < 0 else close + 2
                            continue
                    index += 1
                return names

            def sql_params(sql):
                return {name: params[name] for name in sql_parameter_names(sql) if name in params}

            def normalize_table(value):
                if value is None:
                    return {"columns": [], "rows": [], "summary": {"rows": 0, "columns": 0}}
                if isinstance(value, dict) and "columns" in value and "rows" in value:
                    return value
                if isinstance(value, list):
                    if not value:
                        return {"columns": [], "rows": [], "summary": {"rows": 0, "columns": 0}}
                    if isinstance(value[0], dict):
                        columns = list(value[0].keys())
                        rows = [[item.get(column) for column in columns] for item in value]
                        return {"columns": columns, "rows": rows, "summary": {"rows": len(rows), "columns": len(columns)}}
                    return {"columns": ["value"], "rows": [[item] for item in value], "summary": {"rows": len(value), "columns": 1}}
                if isinstance(value, dict):
                    columns = list(value.keys())
                    return {"columns": columns, "rows": [[value[column] for column in columns]], "summary": {"rows": 1, "columns": len(columns)}}
                return {"columns": ["value"], "rows": [[value]], "summary": {"rows": 1, "columns": 1}}

            def load_data():
                if source_type == "sqlite":
                    import sqlite3

                    table = sqlite_table_name()
                    con = sqlite3.connect(sqlite_uri(dataset_path), uri=True)
                    con.row_factory = sqlite3.Row
                    rows = con.execute(f"SELECT * FROM {quote_identifier(table)}").fetchall()
                    return [dict(row) for row in rows]
                if source_type == "postgres":
                    import psycopg

                    with psycopg.connect(postgres_connection_url()) as con:
                        with con.cursor() as cursor:
                            cursor.execute("SET TRANSACTION READ ONLY")
                            configure_postgres_cursor(cursor)
                            cursor.execute(postgres_table_query())
                            columns = [item.name for item in (cursor.description or [])]
                            return [dict(zip(columns, row)) for row in cursor.fetchall()]
                if source_type == "mysql":
                    import pymysql

                    with pymysql.connect(**mysql_connection_options()) as con:
                        with con.cursor() as cursor:
                            begin_mysql_read_only_transaction(cursor)
                            configure_mysql_cursor(cursor)
                            cursor.execute(mysql_table_query())
                            columns = [item[0] for item in (cursor.description or [])]
                            return [dict(zip(columns, row)) for row in cursor.fetchall()]
                if source_type == "clickhouse":
                    import clickhouse_connect

                    client = clickhouse_connect.get_client(**clickhouse_connection_options())
                    try:
                        query_result = client.query(clickhouse_table_query(), settings=clickhouse_query_settings())
                        columns = [str(column) for column in query_result.column_names]
                        return [dict(zip(columns, row)) for row in query_result.result_rows]
                    finally:
                        client.close()
                if source_type == "parquet" or (
                    source_type == "s3" and connection.get("runtime_format") == "parquet"
                ):
                    import duckdb

                    con = duckdb.connect(database=":memory:")
                    safe_dataset = duckdb_path(dataset_path)
                    relation = con.execute(f"SELECT * FROM read_parquet('{safe_dataset}')")
                    columns = [item[0] for item in (relation.description or [])]
                    return [dict(zip(columns, row)) for row in relation.fetchall()]
                with open(dataset_path, "r", encoding="utf-8-sig", newline="") as handle:
                    return list(csv.DictReader(handle))

            def load_csv():
                return load_data()

            def load_parquet():
                return load_data()

            def load_xlsx():
                return load_data()

            if language == "sql":
                sql = script_path.read_text(encoding="utf-8")
                bound_params = sql_params(sql)
                if source_type == "sqlite":
                    import sqlite3

                    table = sqlite_table_name()
                    con = sqlite3.connect(sqlite_uri(dataset_path), uri=True)
                    con.row_factory = sqlite3.Row
                    con.execute(f"CREATE TEMP VIEW data AS SELECT * FROM {quote_identifier(table)}")
                    cursor = con.execute(sql, bound_params)
                    columns = [item[0] for item in (cursor.description or [])]
                    rows = [list(row) for row in cursor.fetchmany(500)] if columns else []
                elif source_type == "postgres":
                    import psycopg

                    with psycopg.connect(postgres_connection_url()) as con:
                        with con.cursor() as cursor:
                            cursor.execute("SET TRANSACTION READ ONLY")
                            configure_postgres_cursor(cursor)
                            cursor.execute(sql, dbapi_named_params(sql))
                            columns = [item.name for item in (cursor.description or [])]
                            rows = [list(row) for row in cursor.fetchmany(500)] if columns else []
                elif source_type == "mysql":
                    import pymysql

                    with pymysql.connect(**mysql_connection_options()) as con:
                        with con.cursor() as cursor:
                            begin_mysql_read_only_transaction(cursor)
                            configure_mysql_cursor(cursor)
                            cursor.execute(sql, dbapi_named_params(sql))
                            columns = [item[0] for item in (cursor.description or [])]
                            rows = [list(row) for row in cursor.fetchmany(500)] if columns else []
                elif source_type == "clickhouse":
                    import clickhouse_connect

                    client = clickhouse_connect.get_client(**clickhouse_connection_options())
                    try:
                        query_result = client.query(
                            sql,
                            parameters=params,
                            settings=clickhouse_query_settings(),
                        )
                        columns = [str(column) for column in query_result.column_names]
                        rows = [list(row) for row in query_result.result_rows[:500]] if columns else []
                    finally:
                        client.close()
                elif source_type == "parquet" or (
                    source_type == "s3" and connection.get("runtime_format") == "parquet"
                ):
                    import duckdb

                    con = duckdb.connect(database=":memory:")
                    safe_dataset = duckdb_path(dataset_path)
                    con.execute(f"CREATE VIEW data AS SELECT * FROM read_parquet('{safe_dataset}')")
                    relation = con.execute(sql, bound_params)
                    columns = [item[0] for item in (relation.description or [])]
                    rows = relation.fetchmany(500) if columns else []
                else:
                    import duckdb

                    con = duckdb.connect(database=":memory:")
                    safe_dataset = duckdb_path(dataset_path)
                    con.execute(f"CREATE VIEW data AS SELECT * FROM read_csv_auto('{safe_dataset}')")
                    relation = con.execute(sql, bound_params)
                    columns = [item[0] for item in (relation.description or [])]
                    rows = relation.fetchmany(500) if columns else []
                result = {"columns": columns, "rows": rows, "summary": {"rows": len(rows), "columns": len(columns)}}
            else:
                namespace = {
                    "csv": csv,
                    "json": json,
                    "dataset_path": dataset_path,
                    "output_path": str(output_path),
                    "load_data": load_data,
                    "load_csv": load_csv,
                    "load_parquet": load_parquet,
                    "load_xlsx": load_xlsx,
                    "params": params,
                }
                code = compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec")
                exec(code, namespace)
                if "result" in namespace:
                    result = normalize_table(namespace["result"])
                elif output_path.exists():
                    result = normalize_table(json.loads(output_path.read_text(encoding="utf-8")))
                else:
                    result = normalize_table(None)

            output_path.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
            '''
    )
