from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest

from app import db as db_module
from app import main as main_module
from app import s3_snapshots as s3_snapshots_module
from app.db import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID, UPLOAD_DIR, connect, decode_json
from app.data_masking import REDACTED_FIELD_VALUE, mask_value
from app.lineage import data_source_impact
from app.main import (
    apply_report_filters,
    build_chart_data,
    build_metric_widget,
    build_pie_chart,
    build_scatter_chart,
    build_table_widget,
    claim_due_retries,
    claim_due_schedules,
    claim_queued_manual_runs,
    claim_queued_schedule_runs,
    next_cron_run,
    schedule_backfill_occurrences,
)
from app import runner as runner_module
from app.runner import DockerRunner, claim_run_execution, create_run, execute_run, now_iso
from app.secret_tools import REDACTED_VALUE
from app.xlsx_tools import read_xlsx_rows


def upload_source(client, name: str, file_bytes: bytes, filename: str = "sales.csv", content_type: str = "text/csv"):
    response = client.post(
        "/data-sources",
        data={"name": name},
        files={"file": (filename, file_bytes, content_type)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM data_sources WHERE name = ?", (name,)).fetchone()


def create_project(client, name: str, source_id: str, script: str, language: str = "sql", parameters_json: str = "{}"):
    response = client.post(
        "/projects",
        data={"name": name, "language": language, "data_source_id": source_id, "script": script, "parameters_json": parameters_json},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def update_workspace_quotas(
    client,
    data_sources: int,
    projects: int,
    schedules: int,
    reports: int,
    concurrent_runs: int = 2,
    storage_mb: int | None = None,
):
    data = {
        "max_data_sources": data_sources,
        "max_projects": projects,
        "max_schedules": schedules,
        "max_reports": reports,
        "max_concurrent_runs": concurrent_runs,
    }
    if storage_mb is not None:
        data["max_storage_mb"] = storage_mb
    response = client.post(
        "/workspace/quotas",
        data=data,
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def create_sample_sqlite(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sales (region TEXT NOT NULL, revenue INTEGER NOT NULL)")
        conn.executemany(
            "INSERT INTO sales (region, revenue) VALUES (?, ?)",
            [("East", 120), ("West", 180), ("East", 90)],
        )


def connect_sqlite_source(client, name: str, database_path: Path, table_name: str = "sales"):
    response = client.post(
        "/data-sources/sqlite",
        data={"name": name, "database_path": str(database_path), "table_name": table_name},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM data_sources WHERE name = ?", (name,)).fetchone()


def create_postgres_secret_reference(client, name: str, environment_variable: str):
    response = client.post(
        "/secrets",
        data={"name": name, "environment_variable": environment_variable, "description": "PostgreSQL connection URL"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM secret_references WHERE name = ?", (name,)).fetchone()


def wait_for_run_status(run_id: str, expected_status: str, timeout_seconds: float = 3) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with connect() as conn:
            run = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is not None and run["status"] == expected_status:
            return
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not reach {expected_status}")


def test_report_widgets_ignore_non_finite_values():
    result = {
        "columns": ["label", "value"],
        "rows": [["valid", 2], ["not-a-number", float("nan")], ["infinite", float("inf")]],
    }

    chart = build_chart_data(result, "label", "value")
    metric = build_metric_widget(result, "Finite total", {"aggregate": "sum", "value_column": "value"})

    assert [bar["label"] for bar in chart["bars"]] == ["valid"]
    assert metric["value"] == "2"


def test_health_and_readiness_endpoints_check_the_database(client):
    health_response = client.get("/healthz")
    readiness_response = client.get("/readyz")

    assert health_response.status_code == 200
    assert readiness_response.status_code == 200
    assert health_response.json() == {
        "status": "ok",
        "database": "ok",
        "scheduler": "disabled",
        "runner": "local",
    }
    assert readiness_response.json() == health_response.json()


def test_sql_project_run_succeeds(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(
        client,
        "regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs/")
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"] == [["East", 210], ["West", 180]]


def test_queued_run_can_be_canceled(client, sample_csv_bytes):
    source = upload_source(client, "queued cancellation sales", sample_csv_bytes)
    project = create_project(client, "queued cancellation project", source["id"], "SELECT * FROM data LIMIT 1;")
    run_id = create_run(project["id"], "manual")

    response = client.post(f"/runs/{run_id}/cancel", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/runs/{run_id}")
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        event = conn.execute("SELECT * FROM audit_events WHERE action = 'run.canceled' AND resource_id = ?", (run_id,)).fetchone()
    assert run["status"] == "canceled"
    assert run["error"] == "Canceled before execution."
    assert event is not None


def test_run_finish_cannot_overwrite_a_canceling_status(client, sample_csv_bytes):
    source = upload_source(client, "finish cancellation sales", sample_csv_bytes)
    project = create_project(client, "finish cancellation project", source["id"], "SELECT * FROM data LIMIT 1;")
    run_id = create_run(project["id"], "manual")
    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'canceling' WHERE id = ?", (run_id,))

    runner_module._finish_run(
        run_id,
        "succeeded",
        "completed after cancellation request",
        {"columns": ["value"], "rows": [[1]]},
        None,
        time.monotonic(),
    )

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "canceled"
    assert run["result_json"] is None
    assert "canceled" in run["error"].lower()


def test_running_local_run_can_be_canceled(client, sample_csv_bytes):
    source = upload_source(client, "active cancellation sales", sample_csv_bytes)
    project = create_project(
        client,
        "active cancellation project",
        source["id"],
        "import time\ntime.sleep(10)\nresult = {'columns': ['value'], 'rows': [[1]]}",
        language="python",
    )
    run_id = create_run(project["id"], "manual")
    worker = threading.Thread(target=execute_run, args=(run_id,), daemon=True)
    worker.start()
    wait_for_run_status(run_id, "running")

    response = client.post(f"/runs/{run_id}/cancel", follow_redirects=False)

    assert response.status_code == 303
    worker.join(timeout=5)
    assert not worker.is_alive()
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        events = conn.execute(
            "SELECT action FROM audit_events WHERE resource_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    assert run["status"] == "canceled"
    assert run["result_json"] is None
    assert "canceled" in run["error"].lower()
    assert [event["action"] for event in events] == ["run.queued", "run.cancel_requested", "run.canceled"]


def test_canceled_scheduled_run_does_not_retry_notify_or_refresh_report(client, sample_csv_bytes):
    source = upload_source(client, "scheduled cancellation sales", sample_csv_bytes)
    project = create_project(
        client,
        "scheduled cancellation project",
        source["id"],
        "import time\ntime.sleep(10)\nresult = {'columns': ['value'], 'rows': [[1]]}",
        language="python",
    )
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "scheduled cancellation report", "description": "No canceled snapshot"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    schedule_response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "scheduled cancellation", "interval_minutes": 60, "max_retries": 2},
        follow_redirects=False,
    )
    assert schedule_response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
    run_id = create_run(project["id"], "schedule", schedule["id"])
    worker = threading.Thread(target=execute_run, args=(run_id,), daemon=True)
    worker.start()
    wait_for_run_status(run_id, "running")

    assert client.post(f"/runs/{run_id}/cancel", follow_redirects=False).status_code == 303
    worker.join(timeout=5)
    assert not worker.is_alive()

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        retry_count = conn.execute(
            "SELECT COUNT(*) AS count FROM runs WHERE retry_of_run_id = ?",
            (run_id,),
        ).fetchone()["count"]
        snapshot_count = conn.execute(
            "SELECT COUNT(*) AS count FROM report_snapshots WHERE report_id = ?",
            (report_id,),
        ).fetchone()["count"]
        notification_count = conn.execute(
            "SELECT COUNT(*) AS count FROM notifications WHERE resource_type = 'run' AND resource_id = ?",
            (run_id,),
        ).fetchone()["count"]
    assert run["status"] == "canceled"
    assert retry_count == 0
    assert snapshot_count == 0
    assert notification_count == 0


def test_workspace_resource_quotas_block_new_resources_and_expose_usage(client, sample_csv_bytes):
    response = update_workspace_quotas(client, data_sources=1, projects=1, schedules=1, reports=1)
    assert "Workspace%20limits%20updated" in response.headers["location"]

    response = client.get("/api/workspace/quota")
    assert response.status_code == 200
    assert response.json() == {
        "limits": {
            "data_sources": 1,
            "projects": 1,
            "schedules": 1,
            "reports": 1,
            "concurrent_runs": 2,
            "storage_bytes": 10 * 1024 * 1024 * 1024,
        },
        "usage": {
            "data_sources": 0,
            "projects": 0,
            "schedules": 0,
            "reports": 0,
            "concurrent_runs": 0,
            "storage_bytes": 0,
        },
    }
    source = upload_source(client, "quota sales", sample_csv_bytes)
    response = client.post(
        "/data-sources",
        data={"name": "over data source quota"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Workspace has reached the data source limit of 1." in unquote(response.headers["location"])

    project = create_project(client, "quota project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/projects",
        data={"name": "over project quota", "language": "sql", "data_source_id": source["id"], "script": "SELECT 1;"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Workspace has reached the project limit of 1." in unquote(response.headers["location"])

    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "quota schedule", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "over schedule quota", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Workspace has reached the schedule limit of 1." in unquote(response.headers["location"])

    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Quota Report", "description": "First report"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Over quota report", "description": "Second report"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Workspace has reached the report limit of 1." in unquote(response.headers["location"])

    response = client.get("/api/workspace/quota")
    assert response.status_code == 200
    assert response.json()["usage"] == {
        "data_sources": 1,
        "projects": 1,
        "schedules": 1,
        "reports": 1,
        "concurrent_runs": 0,
        "storage_bytes": len(sample_csv_bytes),
    }
    with connect() as conn:
        quota_event = conn.execute("SELECT * FROM audit_events WHERE action = 'workspace.quota_updated'").fetchone()
    assert quota_event is not None


def test_workspace_storage_quota_blocks_upload_and_cleans_files(client, sample_csv_bytes):
    update_workspace_quotas(client, 10, 10, 10, 10, storage_mb=0)

    response = client.post(
        "/data-sources",
        data={"name": "storage blocked sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Workspace storage limit exceeded" in unquote(response.headers["location"])
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'storage blocked sales'").fetchone()
    assert source is None
    assert list(UPLOAD_DIR.iterdir()) == []


def test_workspace_run_concurrency_queues_and_drains_manual_and_schedule_runs(client, sample_csv_bytes):
    update_workspace_quotas(client, data_sources=2, projects=2, schedules=2, reports=2, concurrent_runs=1)
    source = upload_source(client, "concurrency sales", sample_csv_bytes)
    project = create_project(client, "concurrency project", source["id"], "SELECT * FROM data LIMIT 1;")
    active_run_id = "workspace-concurrency-active"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, parameters_json, started_at)
            VALUES (?, ?, 'running', 'manual', '{}', ?)
            """,
            (active_run_id, project["id"], now_iso()),
        )

    queued_manual_id = create_run(project["id"], "manual")
    execute_run(queued_manual_id)

    with connect() as conn:
        queued_manual = conn.execute("SELECT status FROM runs WHERE id = ?", (queued_manual_id,)).fetchone()
    assert queued_manual["status"] == "queued"
    assert client.get("/api/workspace/quota").json()["usage"]["concurrent_runs"] == 1

    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded', finished_at = ? WHERE id = ?", (now_iso(), active_run_id))
    assert claim_queued_manual_runs() == [queued_manual_id]
    execute_run(queued_manual_id)

    with connect() as conn:
        completed_manual = conn.execute("SELECT status FROM runs WHERE id = ?", (queued_manual_id,)).fetchone()
        manual_claim_audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'run.manual_claimed' AND resource_id = ?",
            (queued_manual_id,),
        ).fetchone()
    assert completed_manual["status"] == "succeeded"
    assert manual_claim_audit is not None

    for name in ("concurrency schedule one", "concurrency schedule two"):
        response = client.post(
            "/schedules",
            data={"project_id": project["id"], "name": name, "interval_minutes": 60},
            follow_redirects=False,
        )
        assert response.status_code == 303
    with connect() as conn:
        schedule_ids = [row["id"] for row in conn.execute("SELECT id FROM schedules WHERE project_id = ?", (project["id"],)).fetchall()]
        for schedule_id in schedule_ids:
            conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule_id))

    due_runs = claim_due_schedules()
    assert len(due_runs) == 2
    first_claimed = claim_queued_schedule_runs()
    assert len(first_claimed) == 1
    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded', finished_at = ? WHERE id = ?", (now_iso(), first_claimed[0]))
    second_claimed = claim_queued_schedule_runs()
    assert len(second_claimed) == 1
    assert second_claimed[0] != first_claimed[0]


def test_run_search_filters_logs_and_omits_sensitive_payloads(client, sample_csv_bytes):
    source = upload_source(client, "searchable run sales", sample_csv_bytes)
    project = create_project(client, "searchable run project", source["id"], "SELECT 1 AS value;")
    matching_run_id = create_run(project["id"], "manual")
    other_run_id = create_run(project["id"], "schedule")
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = 'failed', logs = ?, error = ?, result_json = ?, secret_bindings_json = ?
            WHERE id = ?
            """,
            (
                "loading source\nwarehouse timeout request=abc-123\ncleanup",
                "query failed",
                '{"secret_result": true}',
                '[{"secret_id": "hidden-reference"}]',
                matching_run_id,
            ),
        )
        conn.execute(
            "UPDATE runs SET status = 'succeeded', logs = 'completed normally' WHERE id = ?",
            (other_run_id,),
        )

    response = client.get("/api/runs", params={"q": "abc-123", "status": "failed", "page_size": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page_size"] == 1
    assert payload["items"][0]["id"] == matching_run_id
    assert payload["items"][0]["log_excerpt"] == "warehouse timeout request=abc-123"
    assert "logs" not in payload["items"][0]
    assert "parameters_json" not in payload["items"][0]
    assert "result_json" not in payload["items"][0]
    assert "secret_bindings_json" not in payload["items"][0]
    assert client.get("/api/runs", params={"trigger_type": "unknown"}).status_code == 422


def test_workspace_run_concurrency_claim_is_atomic(client, sample_csv_bytes):
    update_workspace_quotas(client, data_sources=2, projects=2, schedules=2, reports=2, concurrent_runs=1)
    source = upload_source(client, "atomic concurrency sales", sample_csv_bytes)
    project = create_project(client, "atomic concurrency project", source["id"], "SELECT * FROM data LIMIT 1;")
    run_ids = [create_run(project["id"], "manual"), create_run(project["id"], "manual")]
    claims: list[bool] = []

    workers = [threading.Thread(target=lambda run_id=run_id: claims.append(claim_run_execution(run_id))) for run_id in run_ids]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert claims.count(True) == 1
    with connect() as conn:
        statuses = [row["status"] for row in conn.execute("SELECT status FROM runs WHERE id IN (?, ?)", run_ids).fetchall()]
    assert sorted(statuses) == ["queued", "running"]


def test_report_refresh_requires_an_available_workspace_execution_slot(client, sample_csv_bytes):
    update_workspace_quotas(client, data_sources=2, projects=2, schedules=2, reports=2, concurrent_runs=1)
    source = upload_source(client, "refresh concurrency sales", sample_csv_bytes)
    project = create_project(client, "refresh concurrency project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Refresh concurrency report", "description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_id = response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, parameters_json, started_at)
            VALUES ('refresh-concurrency-active', ?, 'running', 'manual', '{}', ?)
            """,
            (project["id"], now_iso()),
        )

    response = client.post(f"/reports/{report_id}/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert "available%20workspace%20execution%20slot" in response.headers["location"]
    with connect() as conn:
        refresh_run = conn.execute(
            "SELECT * FROM runs WHERE project_id = ? AND trigger_type = 'report_refresh'",
            (project["id"],),
        ).fetchone()
        deferred_event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'report.refresh_deferred' AND resource_id = ?",
            (report_id,),
        ).fetchone()
        snapshot_count = conn.execute("SELECT COUNT(*) AS count FROM report_snapshots WHERE report_id = ?", (report_id,)).fetchone()["count"]
    assert refresh_run["status"] == "canceled"
    assert deferred_event is not None
    assert snapshot_count == 0


def test_workspace_quota_uses_configured_defaults(client, monkeypatch):
    monkeypatch.setenv("ANYDATAS_DEFAULT_MAX_DATA_SOURCES", "2")
    monkeypatch.setenv("ANYDATAS_DEFAULT_MAX_PROJECTS", "3")
    monkeypatch.setenv("ANYDATAS_DEFAULT_MAX_SCHEDULES", "4")
    monkeypatch.setenv("ANYDATAS_DEFAULT_MAX_REPORTS", "5")
    monkeypatch.setenv("ANYDATAS_DEFAULT_MAX_CONCURRENT_RUNS", "6")
    monkeypatch.setenv("ANYDATAS_DEFAULT_MAX_STORAGE_BYTES", "7340032")

    response = client.get("/api/workspace/quota")

    assert response.status_code == 200
    assert response.json()["limits"] == {
        "data_sources": 2,
        "projects": 3,
        "schedules": 4,
        "reports": 5,
        "concurrent_runs": 6,
        "storage_bytes": 7340032,
    }


def test_csv_data_source_quality_summary_is_stored(client):
    source = upload_source(
        client,
        "messy quality sales",
        b"region,revenue\nEast,120\nWest,\nEast,120\n",
    )

    quality = decode_json(source["quality_json"], {})

    assert quality["row_count"] == 3
    assert quality["column_count"] == 2
    assert quality["empty_cells"] == 1
    assert quality["duplicate_rows"] == 1
    assert quality["completeness"] == 83.33


def test_data_source_schema_metadata_is_inferred_rendered_and_editable(client, sample_csv_bytes):
    source = upload_source(client, "schema sales", sample_csv_bytes)
    initial_metadata = decode_json(source["column_metadata_json"], {})
    assert initial_metadata["date"]["type"] == "date"
    assert initial_metadata["revenue"]["type"] == "integer"
    assert initial_metadata["region"]["type"] == "text"

    detail_response = client.get(f"/data-sources/{source['id']}")
    assert detail_response.status_code == 200
    assert "Schema" in detail_response.text
    assert 'name="field_types"' in detail_response.text
    assert 'name="descriptions"' in detail_response.text
    assert 'name="field_classifications"' in detail_response.text
    assert 'name="masking_policies"' in detail_response.text
    assert "Preview" in detail_response.text

    form_data = {
        "field_names": ["date", "revenue", "region"],
        "field_types": ["date", "number", "text"],
        "descriptions": ["Transaction day", "Revenue in USD", "Sales region"],
        "field_classifications": ["none", "financial", "customer"],
        "masking_policies": ["none", "redact", "hash"],
    }
    update_response = client.post(f"/data-sources/{source['id']}/schema", data=form_data, follow_redirects=False)
    assert update_response.status_code == 303
    with connect() as conn:
        updated_source = conn.execute("SELECT * FROM data_sources WHERE id = ?", (source["id"],)).fetchone()
        audit_event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.schema_updated' AND resource_id = ?",
            (source["id"],),
        ).fetchone()
    metadata = decode_json(updated_source["column_metadata_json"], {})
    assert metadata["date"] == {
        "type": "date",
        "description": "Transaction day",
        "classification": "none",
        "masking": "none",
    }
    assert metadata["revenue"] == {
        "type": "number",
        "description": "Revenue in USD",
        "classification": "financial",
        "masking": "redact",
    }
    assert metadata["region"] == {
        "type": "text",
        "description": "Sales region",
        "classification": "customer",
        "masking": "hash",
    }
    assert audit_event is not None

    invalid_response = client.post(
        f"/data-sources/{source['id']}/schema",
        data={
            "field_names": ["date", "revenue", "region"],
            "field_types": ["date", "currency", "text"],
            "descriptions": ["Transaction day", "Revenue in USD", "Sales region"],
        },
        follow_redirects=False,
    )
    assert invalid_response.status_code == 303
    assert "Unsupported%20field%20type" in invalid_response.headers["location"]


def test_run_detail_and_result_downloads_are_available(client, sample_csv_bytes):
    source = upload_source(client, "download sales", sample_csv_bytes)
    project = create_project(
        client,
        "downloadable regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    detail_response = client.get(response.headers["location"])
    assert detail_response.status_code == 200
    assert "Run Details" in detail_response.text
    assert "Download CSV" in detail_response.text
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()

    csv_response = client.get(f"/runs/{run['id']}/result.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-disposition"] == f'attachment; filename="run-{run["id"][:8]}-result.csv"'
    assert "region,revenue" in csv_response.text
    assert "East,210" in csv_response.text

    json_response = client.get(f"/runs/{run['id']}/result.json")
    assert json_response.status_code == 200
    assert json_response.json()["rows"] == [["East", 210], ["West", 180]]


def test_run_detail_paginates_large_results_and_logs(client, sample_csv_bytes):
    source = upload_source(client, "paginated run sales", sample_csv_bytes)
    project = create_project(client, "paginated run project", source["id"], "SELECT * FROM data LIMIT 1;")
    run_id = create_run(project["id"], "manual")
    result = {
        "columns": ["value"],
        "rows": [[f"result-row-{index:03d}"] for index in range(205)],
        "summary": {"rows": 205, "columns": 1},
    }
    logs = "\n".join(f"log-line-{index:04d}" for index in range(405))
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded', result_json = ?, logs = ?, finished_at = ? WHERE id = ?",
            (json.dumps(result), logs, now_iso(), run_id),
        )

    first_page = client.get(f"/runs/{run_id}")
    assert first_page.status_code == 200
    assert "1-100 of 205 rows" in first_page.text
    assert "result-row-000" in first_page.text
    assert "result-row-099" in first_page.text
    assert "result-row-100" not in first_page.text
    assert "1-200 of 405 log lines" in first_page.text
    assert "log-line-0000" in first_page.text
    assert "log-line-0199" in first_page.text
    assert "log-line-0200" not in first_page.text
    assert first_page.text.count("Page 1 of 3") == 2

    final_page = client.get(f"/runs/{run_id}?result_page=3&log_page=3")
    assert final_page.status_code == 200
    assert "201-205 of 205 rows" in final_page.text
    assert "result-row-200" in final_page.text
    assert "result-row-199" not in final_page.text
    assert "401-405 of 405 log lines" in final_page.text
    assert "log-line-0400" in final_page.text
    assert "log-line-0399" not in final_page.text
    assert final_page.text.count("Page 3 of 3") == 2

    clamped_page = client.get(f"/runs/{run_id}?result_page=999&log_page=999")
    assert "201-205 of 205 rows" in clamped_page.text
    assert "401-405 of 405 log lines" in clamped_page.text


def test_run_detail_and_downloads_are_workspace_scoped(client, sample_csv_bytes):
    source = upload_source(client, "private sales", sample_csv_bytes)
    project = create_project(client, "private run", source["id"], "SELECT * FROM data LIMIT 1;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()

    client.post("/login", data={"email": "outsider@example.com", "name": "Outsider"}, follow_redirects=False)

    detail_response = client.get(f"/runs/{run['id']}")
    csv_response = client.get(f"/runs/{run['id']}/result.csv")
    json_response = client.get(f"/runs/{run['id']}/result.json")
    assert detail_response.status_code == 404
    assert csv_response.status_code == 404
    assert json_response.status_code == 404


def test_report_snapshot_downloads_are_available_and_audited(client, sample_csv_bytes, tmp_path):
    source = upload_source(client, "report download sales", sample_csv_bytes)
    project = create_project(
        client,
        "report download project",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Downloadable report", "description": "Snapshot"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_id = response.headers["location"].rsplit("/", 1)[-1]

    csv_response = client.get(f"/reports/{report_id}/snapshot.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-disposition"] == f'attachment; filename="report-{report_id[:8]}-snapshot.csv"'
    assert "region,revenue" in csv_response.text
    assert "East,210" in csv_response.text

    json_response = client.get(f"/reports/{report_id}/snapshot.json")
    assert json_response.status_code == 200
    assert json_response.headers["content-disposition"] == f'attachment; filename="report-{report_id[:8]}-snapshot.json"'
    assert json_response.json()["rows"] == [["East", 210], ["West", 180]]

    xlsx_response = client.get(f"/reports/{report_id}/snapshot.xlsx")
    assert xlsx_response.status_code == 200
    assert xlsx_response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert xlsx_response.headers["content-disposition"] == f'attachment; filename="report-{report_id[:8]}-snapshot.xlsx"'
    workbook_path = tmp_path / "report.xlsx"
    workbook_path.write_bytes(xlsx_response.content)
    workbook_columns, workbook_rows, workbook_sheet = read_xlsx_rows(workbook_path)
    assert workbook_columns == ["region", "revenue"]
    assert workbook_rows == [{"region": "East", "revenue": 210}, {"region": "West", "revenue": 180}]
    assert workbook_sheet == "Downloadable report"

    png_response = client.get(f"/reports/{report_id}/snapshot.png")
    assert png_response.status_code == 200
    assert png_response.headers["content-type"] == "image/png"
    assert png_response.headers["content-disposition"] == f'attachment; filename="report-{report_id[:8]}-snapshot.png"'
    assert png_response.content.startswith(b"\x89PNG\r\n\x1a\n")

    pdf_response = client.get(f"/reports/{report_id}/snapshot.pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.headers["content-disposition"] == f'attachment; filename="report-{report_id[:8]}-snapshot.pdf"'
    assert pdf_response.content.startswith(b"%PDF-")
    with connect() as conn:
        exports = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'report.exported' AND resource_id = ? ORDER BY created_at",
            (report_id,),
        ).fetchall()
    assert [decode_json(event["detail_json"], {})["format"] for event in exports] == [
        "csv",
        "json",
        "xlsx",
        "png",
        "pdf",
    ]


def test_field_masking_applies_to_non_manager_run_and_report_exports(client, sample_csv_bytes):
    source = upload_source(client, "governed export sales", sample_csv_bytes)
    schema_response = client.post(
        f"/data-sources/{source['id']}/schema",
        data={
            "field_names": ["date", "revenue", "region"],
            "field_types": ["date", "integer", "text"],
            "descriptions": ["", "Booked revenue", "Sales territory"],
            "field_classifications": ["none", "financial", "customer"],
            "masking_policies": ["none", "redact", "hash"],
        },
        follow_redirects=False,
    )
    assert schema_response.status_code == 303
    project = create_project(
        client,
        "governed export project",
        source["id"],
        "SELECT region, revenue FROM data ORDER BY region;",
    )
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Governed report", "description": ""},
        follow_redirects=False,
    )
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC", (project["id"],)).fetchone()

    assert client.post(
        "/workspace/members",
        data={"email": "exporter@example.com", "name": "Exporter", "role": "analyst"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        "/login",
        data={"email": "exporter@example.com", "name": "Exporter"},
        follow_redirects=False,
    ).status_code == 303

    run_export = client.get(f"/runs/{run['id']}/result.json")
    report_export = client.get(f"/reports/{report_id}/snapshot.json")

    expected_rows = [
        [mask_value("East", "hash"), REDACTED_FIELD_VALUE],
        [mask_value("East", "hash"), REDACTED_FIELD_VALUE],
        [mask_value("West", "hash"), REDACTED_FIELD_VALUE],
    ]
    assert run_export.status_code == 200
    assert report_export.status_code == 200
    assert run_export.json()["rows"] == expected_rows
    assert report_export.json()["rows"] == expected_rows
    with connect() as conn:
        export_events = conn.execute(
            "SELECT detail_json FROM audit_events WHERE action IN ('run.exported', 'report.exported') ORDER BY created_at"
        ).fetchall()
    assert [decode_json(event["detail_json"], {})["masked_columns"] for event in export_events[-2:]] == [
        ["region", "revenue"],
        ["region", "revenue"],
    ]


def test_data_source_impact_tracks_published_dependencies_after_the_draft_moves(client, sample_csv_bytes):
    production_source = upload_source(client, "impact production sales", sample_csv_bytes)
    draft_source = upload_source(client, "impact draft sales", sample_csv_bytes)
    project = create_project(
        client,
        "impact revenue project",
        production_source["id"],
        "SELECT * FROM data;",
    )
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "impact daily", "interval_minutes": 60},
        follow_redirects=False,
    ).status_code == 303
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Impact report", "description": ""},
        follow_redirects=False,
    )
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    assert client.post(
        f"/projects/{project['id']}",
        data={
            "name": "impact revenue project",
            "language": "sql",
            "data_source_id": draft_source["id"],
            "script": "SELECT * FROM data LIMIT 1;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    ).status_code == 303

    with connect() as conn:
        impact = data_source_impact(conn, DEFAULT_WORKSPACE_ID, production_source["id"])

    assert len(impact["projects"]) == 1
    affected_project = impact["projects"][0]
    assert affected_project["draft_uses_source"] == 0
    assert affected_project["published_uses_source"] == 1
    assert affected_project["version_count"] == 1
    assert affected_project["active_schedule_count"] == 1
    assert affected_project["report_count"] == 1
    assert impact["run_count"] == 1
    assert impact["active_schedule_count"] == 1
    assert impact["schedules"][0]["name"] == "impact daily"
    assert impact["reports"][0]["id"] == report_id


def test_report_filters_apply_select_contains_and_range_to_snapshot_rows():
    result = {
        "columns": ["region", "revenue", "owner"],
        "rows": [["East", 120, "Ada"], ["East", 90, "Grace"], ["West", 180, "Ada"]],
    }
    filters = [
        {"id": "region", "name": "Region", "column_name": "region", "filter_type": "select", "default_value": ""},
        {"id": "owner", "name": "Owner", "column_name": "owner", "filter_type": "contains", "default_value": ""},
        {"id": "revenue", "name": "Revenue", "column_name": "revenue", "filter_type": "range", "default_value": ""},
    ]

    filtered_result, rendered_filters = apply_report_filters(
        result,
        filters,
        {
            "filter_region": "East",
            "filter_owner": "ada",
            "filter_revenue_min": "100",
            "filter_revenue_max": "150",
        },
    )

    assert filtered_result["rows"] == [["East", 120, "Ada"]]
    assert rendered_filters[0]["options"] == ["East", "West"]
    assert rendered_filters[1]["value"] == "ada"
    assert rendered_filters[2]["minimum"] == "100"
    assert rendered_filters[2]["maximum"] == "150"


def test_pie_chart_aggregates_categories_and_limits_legend_entries():
    result = {
        "columns": ["region", "revenue"],
        "rows": [[f"Region {index}", index + 1] for index in range(10)] + [["Region 9", 2]],
    }

    chart = build_pie_chart(result, "region", "revenue")

    assert len(chart["slices"]) == 8
    assert chart["slices"][-1]["label"] == "Other"
    assert chart["total"] == sum(index + 1 for index in range(10)) + 2
    assert chart["gradient"].startswith("conic-gradient(")


def test_scatter_chart_and_table_highlights_use_configured_numeric_columns():
    result = {
        "columns": ["day", "revenue", "region"],
        "rows": [[1, 120, "East"], [2, 80, "West"], [3, 180, "North"]],
    }

    scatter = build_scatter_chart(result, "day", "revenue")
    highlighted_table = build_table_widget(
        result,
        {"limit": 10, "highlight_column": "revenue", "highlight_rule": "below", "highlight_threshold": 100},
    )

    assert scatter["x_column"] == "day"
    assert scatter["value_column"] == "revenue"
    assert len(scatter["points"]) == 3
    assert {point["x"] for point in scatter["points"]} == {6.0, 50.0, 94.0}
    assert highlighted_table["highlight_column"] == "revenue"
    assert highlighted_table["rows"][0]["cells"][1]["class_name"] == ""
    assert highlighted_table["rows"][1]["cells"][1]["class_name"] == "table-cell-bad"
    assert highlighted_table["rows"][2]["cells"][1]["class_name"] == ""


def test_report_scatter_and_table_highlight_components_are_persisted_and_rendered(client):
    source = upload_source(
        client,
        "scatter source",
        b"day,revenue\n1,120\n2,80\n3,180\n",
    )
    project = create_project(client, "scatter project", source["id"], "SELECT day, revenue FROM data ORDER BY day;")
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Scatter report", "description": ""},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]

    scatter_response = client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "scatter", "title": "Revenue distribution", "x_column": "day", "value_column": "revenue"},
        follow_redirects=False,
    )
    table_response = client.post(
        f"/reports/{report_id}/widgets",
        data={
            "kind": "table",
            "title": "Revenue threshold",
            "table_limit": 10,
            "table_highlight_column": "revenue",
            "table_highlight_rule": "below",
            "table_highlight_threshold": 100,
        },
        follow_redirects=False,
    )
    invalid_response = client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "table", "table_highlight_column": "revenue", "table_highlight_rule": "above"},
        follow_redirects=False,
    )

    assert scatter_response.status_code == 303
    assert table_response.status_code == 303
    assert invalid_response.status_code == 303
    assert "Table threshold rules require a finite numeric threshold." in unquote(invalid_response.headers["location"])
    with connect() as conn:
        scatter_widget = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? AND title = 'Revenue distribution'",
            (report_id,),
        ).fetchone()
        table_widget = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? AND title = 'Revenue threshold'",
            (report_id,),
        ).fetchone()
    assert decode_json(scatter_widget["config_json"], {}) == {
        "x_column": "day",
        "value_column": "revenue",
        "width": "half",
    }
    assert decode_json(table_widget["config_json"], {}) == {
        "highlight_column": "revenue",
        "highlight_rule": "below",
        "highlight_threshold": 100.0,
        "limit": 10,
        "width": "full",
    }

    report_page = client.get(f"/reports/{report_id}")

    assert report_page.status_code == 200
    assert "Revenue distribution" in report_page.text
    assert 'class="scatter-chart"' in report_page.text
    assert "Revenue threshold" in report_page.text
    assert 'class="table-cell-bad"' in report_page.text


def test_report_widgets_are_created_rendered_and_audited(client, sample_csv_bytes):
    source = upload_source(client, "widget sales", sample_csv_bytes)
    project = create_project(
        client,
        "widget project",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Widget report", "description": "Configured components"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        defaults = conn.execute(
            "SELECT kind, title FROM report_widgets WHERE report_id = ? ORDER BY position",
            (report_id,),
        ).fetchall()
    assert [(widget["kind"], widget["title"]) for widget in defaults] == [
        ("metric", "Rows"),
        ("metric", "Columns"),
        ("bar", "Comparison"),
        ("table", "Result Table"),
    ]

    metric_response = client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "metric", "title": "Revenue total", "aggregate": "sum", "value_column": "revenue"},
        follow_redirects=False,
    )
    line_response = client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "line", "title": "Revenue trend", "value_column": "revenue", "label_column": "region"},
        follow_redirects=False,
    )
    markdown_response = client.post(
        f"/reports/{report_id}/widgets",
        data={
            "kind": "markdown",
            "title": "Analyst notes",
            "markdown_text": "# Context\n- **Verified**\n<script>alert(1)</script>",
        },
        follow_redirects=False,
    )
    pie_response = client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "pie", "title": "Revenue share", "value_column": "revenue", "label_column": "region"},
        follow_redirects=False,
    )
    filter_response = client.post(
        f"/reports/{report_id}/filters",
        data={"name": "Region", "column_name": "region", "filter_type": "select"},
        follow_redirects=False,
    )
    assert metric_response.status_code == 303
    assert line_response.status_code == 303
    assert markdown_response.status_code == 303
    assert pie_response.status_code == 303
    assert filter_response.status_code == 303

    with connect() as conn:
        report_filter = conn.execute(
            "SELECT * FROM report_filters WHERE report_id = ? AND name = 'Region'",
            (report_id,),
        ).fetchone()

    report_page = client.get(f"/reports/{report_id}")
    assert report_page.status_code == 200
    assert "Revenue total" in report_page.text
    assert "390" in report_page.text
    assert "Revenue trend" in report_page.text
    assert 'class="line-chart"' in report_page.text
    assert "Revenue share" in report_page.text
    assert 'class="pie-chart"' in report_page.text
    assert "Analyst notes" in report_page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report_page.text
    assert "<script>alert(1)</script>" not in report_page.text

    filtered_report_page = client.get(f"/reports/{report_id}?filter_{report_filter['id']}=East")
    assert filtered_report_page.status_code == 200
    assert "1 matching rows" in filtered_report_page.text
    assert "210" in filtered_report_page.text
    assert ">180<" not in filtered_report_page.text
    assert "100.00%" in filtered_report_page.text

    with connect() as conn:
        line_widget = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? AND title = 'Revenue trend'",
            (report_id,),
        ).fetchone()
    delete_response = client.post(
        f"/reports/{report_id}/widgets/{line_widget['id']}/delete",
        follow_redirects=False,
    )
    assert delete_response.status_code == 303
    with connect() as conn:
        deleted = conn.execute("SELECT * FROM report_widgets WHERE id = ?", (line_widget["id"],)).fetchone()
        events = conn.execute(
            "SELECT action FROM audit_events WHERE resource_id = ? AND action LIKE 'report.widget_%' ORDER BY created_at",
            (report_id,),
        ).fetchall()
        filter_events = conn.execute(
            "SELECT action FROM audit_events WHERE resource_id = ? AND action LIKE 'report.filter_%' ORDER BY created_at",
            (report_id,),
        ).fetchall()
    assert deleted is None
    assert [event["action"] for event in events] == [
        "report.widget_created",
        "report.widget_created",
        "report.widget_created",
        "report.widget_created",
        "report.widget_deleted",
    ]
    assert [event["action"] for event in filter_events] == ["report.filter_created"]


def test_report_widget_layout_updates_width_position_render_order_and_audit(client, sample_csv_bytes):
    source = upload_source(client, "layout sales", sample_csv_bytes)
    project = create_project(client, "layout project", source["id"], "SELECT * FROM data;")
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Layout report", "description": ""},
        follow_redirects=False,
    )
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        widgets = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? ORDER BY position",
            (report_id,),
        ).fetchall()
    table_widget = next(widget for widget in widgets if widget["kind"] == "table")
    bar_widget = next(widget for widget in widgets if widget["kind"] == "bar")

    response = client.post(
        f"/reports/{report_id}/widgets/{table_widget['id']}/layout",
        data={"width": "half", "direction": "up"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        ordered_widgets = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? ORDER BY position",
            (report_id,),
        ).fetchall()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'report.widget_layout_updated' AND resource_id = ?",
            (report_id,),
        ).fetchone()
    ordered_ids = [widget["id"] for widget in ordered_widgets]
    assert ordered_ids.index(table_widget["id"]) < ordered_ids.index(bar_widget["id"])
    updated_table = next(widget for widget in ordered_widgets if widget["id"] == table_widget["id"])
    assert decode_json(updated_table["config_json"], {})["width"] == "half"
    assert decode_json(audit["detail_json"], {}) == {
        "widget_id": table_widget["id"],
        "width": "half",
        "direction": "up",
        "position": 2,
    }
    report_html = client.get(f"/reports/{report_id}").text
    assert f'class="report-widget report-layout-item width-half" data-widget-id="{table_widget["id"]}"' in report_html
    assert report_html.index(f'data-widget-id="{table_widget["id"]}"') < report_html.index(
        f'data-widget-id="{bar_widget["id"]}"'
    )


def test_report_widget_drag_order_requires_complete_unique_component_set(client, sample_csv_bytes):
    source = upload_source(client, "drag layout sales", sample_csv_bytes)
    project = create_project(client, "drag layout project", source["id"], "SELECT * FROM data;")
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Drag layout report", "description": ""},
        follow_redirects=False,
    )
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        original_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM report_widgets WHERE report_id = ? ORDER BY position",
                (report_id,),
            ).fetchall()
        ]
    reordered_ids = list(reversed(original_ids))

    response = client.post(
        f"/reports/{report_id}/widgets/reorder",
        data={"order_json": json.dumps(reordered_ids)},
        follow_redirects=False,
    )

    assert response.status_code == 204
    with connect() as conn:
        stored_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM report_widgets WHERE report_id = ? ORDER BY position",
                (report_id,),
            ).fetchall()
        ]
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'report.widget_reordered' AND resource_id = ?",
            (report_id,),
        ).fetchone()
    assert stored_ids == reordered_ids
    assert decode_json(audit["detail_json"], {}) == {"widget_ids": reordered_ids}

    missing = client.post(
        f"/reports/{report_id}/widgets/reorder",
        data={"order_json": json.dumps(reordered_ids[:-1])},
        follow_redirects=False,
    )
    duplicate = client.post(
        f"/reports/{report_id}/widgets/reorder",
        data={"order_json": json.dumps([reordered_ids[0], *reordered_ids])},
        follow_redirects=False,
    )
    assert missing.status_code == 400
    assert duplicate.status_code == 400
    with connect() as conn:
        unchanged_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM report_widgets WHERE report_id = ? ORDER BY position",
                (report_id,),
            ).fetchall()
        ]
    assert unchanged_ids == reordered_ids


def test_empty_report_widget_configuration_survives_database_reinitialization(client, sample_csv_bytes):
    source = upload_source(client, "empty widgets sales", sample_csv_bytes)
    project = create_project(client, "empty widgets project", source["id"], "SELECT * FROM data LIMIT 1;")
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Empty widgets report", "description": ""},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        widgets = conn.execute("SELECT id FROM report_widgets WHERE report_id = ?", (report_id,)).fetchall()
    for widget in widgets:
        assert client.post(
            f"/reports/{report_id}/widgets/{widget['id']}/delete",
            follow_redirects=False,
        ).status_code == 303

    db_module.init_db()

    with connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS count FROM report_widgets WHERE report_id = ?", (report_id,)).fetchone()
        report = conn.execute("SELECT widgets_initialized FROM reports WHERE id = ?", (report_id,)).fetchone()
    assert remaining["count"] == 0
    assert report["widgets_initialized"] == 1


def test_report_member_grants_require_private_visibility(client, sample_csv_bytes):
    source = upload_source(client, "grant visibility sales", sample_csv_bytes)
    project = create_project(client, "grant visibility project", source["id"], "SELECT * FROM data LIMIT 1;")
    member_response = client.post(
        "/workspace/members",
        data={"email": "grantee@example.com", "name": "Grantee", "role": "viewer"},
        follow_redirects=False,
    )
    assert member_response.status_code == 303
    with connect() as conn:
        grantee = conn.execute("SELECT * FROM users WHERE email = 'grantee@example.com'").fetchone()

    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Workspace report", "description": "Shared"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]

    response = client.post(
        f"/reports/{report_id}/grants",
        data={"user_id": grantee["id"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Set%20the%20report%20to%20private" in response.headers["location"]
    with connect() as conn:
        grant = conn.execute(
            "SELECT * FROM report_access_grants WHERE report_id = ? AND user_id = ?",
            (report_id, grantee["id"]),
        ).fetchone()
    assert grant is None


def test_sqlite_data_source_sql_project_run_succeeds(client, tmp_path):
    database_path = tmp_path / "warehouse.sqlite3"
    create_sample_sqlite(database_path)
    source = connect_sqlite_source(client, "warehouse sales", database_path)
    project = create_project(
        client,
        "sqlite regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert source["source_type"] == "sqlite"
    quality = decode_json(source["quality_json"], {})
    assert quality["row_count"] == 3
    assert quality["empty_cells"] == 0
    assert quality["duplicate_rows"] == 0
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"] == [["East", 210], ["West", 180]]


def test_sqlite_data_source_sql_project_binds_parameters(client, tmp_path):
    database_path = tmp_path / "parameterized-warehouse.sqlite3"
    create_sample_sqlite(database_path)
    source = connect_sqlite_source(client, "parameterized warehouse sales", database_path)
    project = create_project(
        client,
        "sqlite parameterized revenue",
        source["id"],
        "SELECT region, revenue FROM data WHERE revenue >= $minimum_revenue ORDER BY revenue DESC;",
        parameters_json='{"minimum_revenue": 150}',
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    assert decode_json(run["result_json"], {})["rows"] == [["West", 180]]


def test_postgres_data_source_uses_external_secret_reference_without_persisting_url(client, monkeypatch):
    connection_url = "postgresql://readonly:very-secret-password@db.example.com:5432/warehouse?sslmode=require"
    monkeypatch.setenv("ANYDATAS_SECRET_WAREHOUSE_POSTGRES_URL", connection_url)
    reference = create_postgres_secret_reference(client, "warehouse-postgres-url", "ANYDATAS_SECRET_WAREHOUSE_POSTGRES_URL")
    inspected: dict[str, str] = {}

    def fake_inspect(url, schema, table):
        inspected.update({"url": url, "schema": schema, "table": table})
        return (
            ["region", "revenue"],
            [{"region": "East", "revenue": 120}],
            1,
            {"row_count": 1, "sampled_rows": 1, "column_count": 2, "completeness": 100},
        )

    monkeypatch.setattr(main_module, "inspect_postgres_table", fake_inspect)
    response = client.post(
        "/data-sources/postgres",
        data={
            "name": "warehouse postgres sales",
            "secret_id": reference["id"],
            "schema_name": "analytics",
            "table_name": "daily_sales",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert inspected == {"url": connection_url, "schema": "analytics", "table": "daily_sales"}
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'warehouse postgres sales'").fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.created' AND resource_id = ?",
            (source["id"],),
        ).fetchone()
    connection = decode_json(source["connection_json"], {})
    assert source["source_type"] == "postgres"
    assert source["path"] == ""
    assert connection == {
        "driver": "postgres",
        "secret_id": reference["id"],
        "schema": "analytics",
        "table": "daily_sales",
        "url_environment": f"ANYDATAS_USER_SECRET_SOURCE_{source['id'].upper()}",
    }
    assert connection_url not in source["connection_json"]
    assert connection_url not in audit["detail_json"]
    blocked_delete = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert blocked_delete.status_code == 303
    assert "still%20used%20by%20a%20data%20source" in blocked_delete.headers["location"]


def test_postgres_run_resolves_connection_reference_and_redacts_it(client, monkeypatch):
    connection_url = "postgresql://readonly:run-secret-password@db.example.com/warehouse"
    monkeypatch.setenv("ANYDATAS_SECRET_POSTGRES_RUN_URL", connection_url)
    reference = create_postgres_secret_reference(client, "postgres-run-url", "ANYDATAS_SECRET_POSTGRES_RUN_URL")
    monkeypatch.setattr(
        main_module,
        "inspect_postgres_table",
        lambda *_args: (
            ["region", "revenue"],
            [{"region": "East", "revenue": 120}],
            1,
            {"row_count": 1, "sampled_rows": 1, "column_count": 2, "completeness": 100},
        ),
    )
    assert client.post(
        "/data-sources/postgres",
        data={"name": "postgres run sales", "secret_id": reference["id"], "schema_name": "analytics", "table_name": "daily_sales"},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'postgres run sales'").fetchone()
    project = create_project(
        client,
        "postgres parameterized run",
        source["id"],
        "SELECT region, revenue FROM data WHERE revenue >= $minimum_revenue;",
        parameters_json='{"minimum_revenue": 100}',
    )
    captured: dict[str, object] = {}

    class FakeRunner:
        def run(self, _project, runtime_source, _run_id, parameters, secret_values):
            captured["source"] = decode_json(runtime_source["connection_json"], {})
            captured["parameters"] = parameters
            captured["secret_values"] = secret_values
            return {"columns": ["connection"], "rows": [[connection_url]], "summary": {"rows": 1, "columns": 1}}, connection_url

    monkeypatch.setattr(runner_module, "get_runner", lambda: FakeRunner())
    queued_run_id = create_run(project["id"], "manual")
    execute_run(queued_run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'run.secrets_resolved' AND resource_id = ?",
            (queued_run_id,),
        ).fetchone()
    expected_environment = f"ANYDATAS_USER_SECRET_SOURCE_{source['id'].upper()}"
    assert captured == {
        "source": {
            "driver": "postgres",
            "schema": "analytics",
            "table": "daily_sales",
            "url_environment": expected_environment,
        },
        "parameters": {"minimum_revenue": 100},
        "secret_values": {expected_environment: connection_url},
    }
    assert run["status"] == "succeeded"
    assert connection_url not in run["logs"]
    assert connection_url not in run["result_json"]
    assert REDACTED_VALUE in run["logs"]
    assert REDACTED_VALUE in run["result_json"]
    assert connection_url not in audit["detail_json"]


def test_postgres_run_rejects_multi_statement_sql_before_starting_user_code(client, monkeypatch):
    connection_url = "postgresql://readonly:blocked-query-password@db.example.com/warehouse"
    monkeypatch.setenv("ANYDATAS_SECRET_POSTGRES_BLOCKED_QUERY_URL", connection_url)
    reference = create_postgres_secret_reference(client, "postgres-blocked-query-url", "ANYDATAS_SECRET_POSTGRES_BLOCKED_QUERY_URL")
    source_id = "a" * 32
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO data_sources (
                id, workspace_id, source_type, name, filename, path, columns_json,
                column_metadata_json, preview_json, row_count, quality_json, connection_json, created_at
            )
            VALUES (?, ?, 'postgres', 'blocked postgres sales', 'analytics.daily_sales', '', ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                source_id,
                DEFAULT_WORKSPACE_ID,
                "[]",
                "{}",
                "[]",
                "{}",
                json.dumps(
                    {
                        "driver": "postgres",
                        "secret_id": reference["id"],
                        "schema": "analytics",
                        "table": "daily_sales",
                        "url_environment": f"ANYDATAS_USER_SECRET_SOURCE_{source_id.upper()}",
                    }
                ),
                now_iso(),
            ),
        )
    project = create_project(
        client,
        "blocked postgres mutation",
        source_id,
        "SELECT * FROM data; DELETE FROM daily_sales;",
    )

    queued_run_id = create_run(project["id"], "manual")
    execute_run(queued_run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()
    assert run["status"] == "failed"
    assert "exactly one read-only query" in run["error"]
    assert connection_url not in run["error"]


def test_mysql_data_source_uses_external_secret_reference_without_persisting_url(client, monkeypatch):
    connection_url = "mysql://readonly:very-secret-password@db.example.com:3306/warehouse"
    monkeypatch.setenv("ANYDATAS_SECRET_WAREHOUSE_MYSQL_URL", connection_url)
    reference = create_postgres_secret_reference(client, "warehouse-mysql-url", "ANYDATAS_SECRET_WAREHOUSE_MYSQL_URL")
    inspected: dict[str, str] = {}

    def fake_inspect(url, database, table):
        inspected.update({"url": url, "database": database, "table": table})
        return (
            ["region", "revenue"],
            [{"region": "East", "revenue": 120}],
            1,
            {"row_count": 1, "sampled_rows": 1, "column_count": 2, "completeness": 100},
        )

    monkeypatch.setattr(main_module, "inspect_mysql_table", fake_inspect)
    response = client.post(
        "/data-sources/mysql",
        data={
            "name": "warehouse mysql sales",
            "secret_id": reference["id"],
            "database_name": "analytics",
            "table_name": "daily_sales",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert inspected == {"url": connection_url, "database": "analytics", "table": "daily_sales"}
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'warehouse mysql sales'").fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.created' AND resource_id = ?",
            (source["id"],),
        ).fetchone()
    connection = decode_json(source["connection_json"], {})
    assert source["source_type"] == "mysql"
    assert source["path"] == ""
    assert connection == {
        "driver": "mysql",
        "secret_id": reference["id"],
        "database": "analytics",
        "table": "daily_sales",
        "url_environment": f"ANYDATAS_USER_SECRET_SOURCE_{source['id'].upper()}",
    }
    assert connection_url not in source["connection_json"]
    assert connection_url not in audit["detail_json"]
    blocked_delete = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert blocked_delete.status_code == 303
    assert "still%20used%20by%20a%20data%20source" in blocked_delete.headers["location"]


def test_mysql_run_resolves_connection_reference_and_redacts_it(client, monkeypatch):
    connection_url = "mysql+pymysql://readonly:run-secret-password@db.example.com/warehouse"
    monkeypatch.setenv("ANYDATAS_SECRET_MYSQL_RUN_URL", connection_url)
    reference = create_postgres_secret_reference(client, "mysql-run-url", "ANYDATAS_SECRET_MYSQL_RUN_URL")
    monkeypatch.setattr(
        main_module,
        "inspect_mysql_table",
        lambda *_args: (
            ["region", "revenue"],
            [{"region": "East", "revenue": 120}],
            1,
            {"row_count": 1, "sampled_rows": 1, "column_count": 2, "completeness": 100},
        ),
    )
    assert client.post(
        "/data-sources/mysql",
        data={"name": "mysql run sales", "secret_id": reference["id"], "database_name": "analytics", "table_name": "daily_sales"},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'mysql run sales'").fetchone()
    project = create_project(
        client,
        "mysql parameterized run",
        source["id"],
        "SELECT region, revenue FROM data WHERE revenue >= $minimum_revenue;",
        parameters_json='{"minimum_revenue": 100}',
    )
    captured: dict[str, object] = {}

    class FakeRunner:
        def run(self, _project, runtime_source, _run_id, parameters, secret_values):
            captured["source"] = decode_json(runtime_source["connection_json"], {})
            captured["parameters"] = parameters
            captured["secret_values"] = secret_values
            return {"columns": ["connection"], "rows": [[connection_url]], "summary": {"rows": 1, "columns": 1}}, connection_url

    monkeypatch.setattr(runner_module, "get_runner", lambda: FakeRunner())
    queued_run_id = create_run(project["id"], "manual")
    execute_run(queued_run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'run.secrets_resolved' AND resource_id = ?",
            (queued_run_id,),
        ).fetchone()
    expected_environment = f"ANYDATAS_USER_SECRET_SOURCE_{source['id'].upper()}"
    assert captured == {
        "source": {
            "driver": "mysql",
            "database": "analytics",
            "table": "daily_sales",
            "url_environment": expected_environment,
        },
        "parameters": {"minimum_revenue": 100},
        "secret_values": {expected_environment: connection_url},
    }
    assert run["status"] == "succeeded"
    assert connection_url not in run["logs"]
    assert connection_url not in run["result_json"]
    assert REDACTED_VALUE in run["logs"]
    assert REDACTED_VALUE in run["result_json"]
    assert connection_url not in audit["detail_json"]


def test_clickhouse_data_source_and_run_use_external_secret_without_persisting_url(client, monkeypatch):
    connection_url = "clickhouses://readonly:run-secret-password@ch.example.com:8443/warehouse"
    monkeypatch.setenv("ANYDATAS_SECRET_CLICKHOUSE_RUN_URL", connection_url)
    reference = create_postgres_secret_reference(client, "clickhouse-run-url", "ANYDATAS_SECRET_CLICKHOUSE_RUN_URL")
    inspected = {}

    def fake_inspect(url, database, table):
        inspected.update({"url": url, "database": database, "table": table})
        return (
            ["region", "revenue"],
            [{"region": "East", "revenue": 120}],
            1,
            {"row_count": 1, "sampled_rows": 1, "column_count": 2, "completeness": 100},
        )

    monkeypatch.setattr(main_module, "inspect_clickhouse_table", fake_inspect)
    response = client.post(
        "/data-sources/clickhouse",
        data={
            "name": "clickhouse run sales",
            "secret_id": reference["id"],
            "database_name": "analytics",
            "table_name": "daily_sales",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert inspected == {"url": connection_url, "database": "analytics", "table": "daily_sales"}
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'clickhouse run sales'").fetchone()
        source_audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.created' AND resource_id = ?",
            (source["id"],),
        ).fetchone()
    stored_connection = decode_json(source["connection_json"], {})
    assert source["source_type"] == "clickhouse"
    assert stored_connection == {
        "driver": "clickhouse",
        "secret_id": reference["id"],
        "database": "analytics",
        "table": "daily_sales",
        "url_environment": f"ANYDATAS_USER_SECRET_SOURCE_{source['id'].upper()}",
    }
    assert connection_url not in source["connection_json"]
    assert connection_url not in source_audit["detail_json"]

    project = create_project(
        client,
        "clickhouse parameterized run",
        source["id"],
        "SELECT region, revenue FROM data WHERE revenue >= $minimum_revenue;",
        parameters_json='{"minimum_revenue": 100}',
    )
    captured = {}

    class FakeRunner:
        def run(self, _project, runtime_source, _run_id, parameters, secret_values):
            captured["source"] = decode_json(runtime_source["connection_json"], {})
            captured["parameters"] = parameters
            captured["secret_values"] = secret_values
            return {"columns": ["connection"], "rows": [[connection_url]]}, connection_url

    monkeypatch.setattr(runner_module, "get_runner", lambda: FakeRunner())
    run_id = create_run(project["id"], "manual")
    execute_run(run_id)
    expected_environment = f"ANYDATAS_USER_SECRET_SOURCE_{source['id'].upper()}"
    assert captured == {
        "source": {
            "driver": "clickhouse",
            "database": "analytics",
            "table": "daily_sales",
            "url_environment": expected_environment,
        },
        "parameters": {"minimum_revenue": 100},
        "secret_values": {expected_environment: connection_url},
    }
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "succeeded"
    assert connection_url not in run["logs"]
    assert connection_url not in run["result_json"]
    assert REDACTED_VALUE in run["logs"]


def test_prepare_run_files_types_clickhouse_parameters_and_rejects_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)
    project = {
        "language": "sql",
        "script": "SELECT '$minimum' AS literal FROM data WHERE value >= $minimum AND active = $active;",
    }

    run_path, _result_path, _wrapper_path = runner_module.prepare_run_files(
        project,
        "clickhouse-rewrite",
        "clickhouse",
        {"minimum": 10, "active": True},
    )

    assert (run_path / "main.sql").read_text(encoding="utf-8") == (
        "SELECT '$minimum' AS literal FROM data WHERE value >= {minimum:Int64} AND active = {active:Bool};"
    )
    with pytest.raises(ValueError, match="cannot contain"):
        runner_module.prepare_run_files(
            {"language": "sql", "script": "SELECT * FROM data INTO OUTFILE 'result.csv'"},
            "clickhouse-rejected",
            "clickhouse",
            {},
        )


def test_s3_snapshot_data_source_runs_without_exposing_or_reusing_credentials(client, monkeypatch):
    secret_key = "s3-private-secret"
    secret_value = json.dumps(
        {
            "endpoint_url": "http://minio:9000",
            "access_key_id": "readonly-key",
            "secret_access_key": secret_key,
            "region": "us-east-1",
        }
    )
    monkeypatch.setenv("ANYDATAS_SECRET_S3_SALES", secret_value)
    reference = create_postgres_secret_reference(client, "s3-sales", "ANYDATAS_SECRET_S3_SALES")
    downloaded = {}

    def fake_download(value, bucket, key, destination, max_bytes):
        downloaded.update(
            {"value": value, "bucket": bucket, "key": key, "destination": destination, "max_bytes": max_bytes}
        )
        destination.write_text("region,revenue\nEast,120\nWest,180\n", encoding="utf-8")
        return {
            "size_bytes": destination.stat().st_size,
            "etag": "etag-123",
            "version_id": "version-7",
            "last_modified": "2026-07-11T00:00:00+00:00",
        }

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", fake_download)
    response = client.post(
        "/data-sources/s3",
        data={
            "name": "S3 monthly sales",
            "secret_id": reference["id"],
            "bucket_name": "analytics-data",
            "object_key": "exports/monthly-sales.csv",
            "classification": "confidential",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert downloaded["value"] == secret_value
    assert downloaded["bucket"] == "analytics-data"
    assert downloaded["key"] == "exports/monthly-sales.csv"
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'S3 monthly sales'").fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.created' AND resource_id = ?",
            (source["id"],),
        ).fetchone()
    connection = decode_json(source["connection_json"], {})
    assert source["source_type"] == "s3"
    assert source["classification"] == "confidential"
    assert source["row_count"] == 2
    assert Path(source["path"]).read_text(encoding="utf-8").startswith("region,revenue")
    assert connection == {
        "driver": "s3",
        "secret_id": reference["id"],
        "bucket": "analytics-data",
        "object_key": "exports/monthly-sales.csv",
        "runtime_format": "csv",
        "size_bytes": Path(source["path"]).stat().st_size,
        "etag": "etag-123",
        "version_id": "version-7",
        "last_modified": "2026-07-11T00:00:00+00:00",
    }
    assert secret_value not in source["connection_json"]
    assert secret_key not in source["connection_json"]
    assert secret_key not in audit["detail_json"]
    blocked_delete = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert blocked_delete.status_code == 303
    assert "still%20used%20by%20a%20data%20source" in blocked_delete.headers["location"]

    project = create_project(
        client,
        "S3 revenue total",
        source["id"],
        "SELECT SUM(revenue) AS total FROM data;",
    )
    run_id = create_run(project["id"], "manual")
    execute_run(run_id)
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "succeeded"
    assert decode_json(run["result_json"], {})["rows"] == [[300]]
    assert secret_value not in run["logs"]
    assert secret_key not in run["result_json"]

    with connect() as conn:
        metadata = decode_json(source["column_metadata_json"], {})
        metadata["region"]["description"] = "Sales territory"
        conn.execute(
            "UPDATE data_sources SET column_metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), source["id"]),
        )

    def refreshed_download(_value, _bucket, _key, destination, _max_bytes):
        destination.write_text("region,revenue\nEast,140\nWest,190\nNorth,80\n", encoding="utf-8")
        return {
            "size_bytes": destination.stat().st_size,
            "etag": "etag-456",
            "version_id": "version-8",
            "last_modified": "2026-07-12T00:00:00+00:00",
        }

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", refreshed_download)
    refresh_response = client.post(f"/data-sources/{source['id']}/refresh-s3", follow_redirects=False)
    assert refresh_response.status_code == 303
    assert "S3%20snapshot%20refreshed" in refresh_response.headers["location"]
    with connect() as conn:
        refreshed_source = conn.execute("SELECT * FROM data_sources WHERE id = ?", (source["id"],)).fetchone()
        refresh_audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'data_source.s3_refreshed' AND resource_id = ?",
            (source["id"],),
        ).fetchone()
    refreshed_connection = decode_json(refreshed_source["connection_json"], {})
    refreshed_metadata = decode_json(refreshed_source["column_metadata_json"], {})
    assert refreshed_source["row_count"] == 3
    assert Path(refreshed_source["path"]).read_text(encoding="utf-8").endswith("North,80\n")
    assert refreshed_connection["etag"] == "etag-456"
    assert refreshed_connection["version_id"] == "version-8"
    assert refreshed_connection["refreshed_at"]
    assert refreshed_metadata["region"]["description"] == "Sales territory"
    assert decode_json(refresh_audit["detail_json"], {})["previous_etag"] == "etag-123"
    detail = client.get(f"/data-sources/{source['id']}")
    assert detail.status_code == 200
    assert "S3 Snapshot" in detail.text
    assert "Refresh Snapshot" in detail.text
    assert "exports/monthly-sales.csv" in detail.text

    stable_snapshot = Path(refreshed_source["path"]).read_bytes()

    def failed_refresh(_value, _bucket, _key, destination, _max_bytes):
        destination.write_text("region,revenue\nCorrupt", encoding="utf-8")
        raise ValueError(f"refresh denied for {secret_key}")

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", failed_refresh)
    failed_response = client.post(f"/data-sources/{source['id']}/refresh-s3", follow_redirects=False)
    assert failed_response.status_code == 303
    assert secret_key not in failed_response.headers["location"]
    assert REDACTED_VALUE in unquote(failed_response.headers["location"])
    assert Path(refreshed_source["path"]).read_bytes() == stable_snapshot


def test_s3_import_errors_are_redacted_and_partial_files_are_removed(client, monkeypatch):
    secret_key = "never-show-this-s3-secret"
    secret_value = json.dumps(
        {"access_key_id": "reader", "secret_access_key": secret_key, "region": "us-east-1"}
    )
    monkeypatch.setenv("ANYDATAS_SECRET_S3_BROKEN", secret_value)
    reference = create_postgres_secret_reference(client, "s3-broken", "ANYDATAS_SECRET_S3_BROKEN")
    attempted_paths = []

    def failed_download(_value, _bucket, _key, destination, _max_bytes):
        attempted_paths.append(destination)
        destination.write_text("partial", encoding="utf-8")
        raise ValueError(f"authentication failed for {secret_key}")

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", failed_download)
    response = client.post(
        "/data-sources/s3",
        data={
            "name": "broken import",
            "secret_id": reference["id"],
            "bucket_name": "analytics-data",
            "object_key": "exports/broken.csv",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert secret_key not in response.headers["location"]
    assert REDACTED_VALUE in unquote(response.headers["location"])
    assert attempted_paths and not attempted_paths[0].exists()
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_sources WHERE name = 'broken import'").fetchone()[0] == 0


def test_s3_snapshot_import_obeys_workspace_storage_quota(client, monkeypatch):
    secret_value = json.dumps(
        {"access_key_id": "reader", "secret_access_key": "quota-secret", "region": "us-east-1"}
    )
    monkeypatch.setenv("ANYDATAS_SECRET_S3_QUOTA", secret_value)
    reference = create_postgres_secret_reference(client, "s3-quota", "ANYDATAS_SECRET_S3_QUOTA")
    created_paths = []

    def fake_download(_value, _bucket, _key, destination, _max_bytes):
        created_paths.append(destination)
        destination.write_text("region,revenue\nEast,120\n", encoding="utf-8")
        return {"size_bytes": destination.stat().st_size, "etag": "quota-etag"}

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", fake_download)
    update_workspace_quotas(client, 10, 10, 10, 10, storage_mb=0)

    response = client.post(
        "/data-sources/s3",
        data={
            "name": "S3 storage blocked sales",
            "secret_id": reference["id"],
            "bucket_name": "analytics-data",
            "object_key": "exports/sales.csv",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Workspace storage limit exceeded" in unquote(response.headers["location"])
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'S3 storage blocked sales'").fetchone()
    assert source is None
    assert created_paths
    assert all(not path.exists() for path in created_paths)


def test_s3_parquet_snapshot_uses_parquet_runner_path(client, monkeypatch, sample_parquet_bytes):
    secret_value = json.dumps(
        {"access_key_id": "reader", "secret_access_key": "parquet-secret", "region": "us-east-1"}
    )
    monkeypatch.setenv("ANYDATAS_SECRET_S3_PARQUET", secret_value)
    reference = create_postgres_secret_reference(client, "s3-parquet", "ANYDATAS_SECRET_S3_PARQUET")

    def fake_download(_value, _bucket, _key, destination, _max_bytes):
        destination.write_bytes(sample_parquet_bytes)
        return {
            "size_bytes": len(sample_parquet_bytes),
            "etag": "parquet-etag",
            "version_id": "",
            "last_modified": "",
        }

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", fake_download)
    assert client.post(
        "/data-sources/s3",
        data={
            "name": "S3 parquet sales",
            "secret_id": reference["id"],
            "bucket_name": "analytics-data",
            "object_key": "exports/sales.parquet",
        },
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'S3 parquet sales'").fetchone()
    assert decode_json(source["connection_json"], {})["runtime_format"] == "parquet"
    project = create_project(
        client,
        "S3 parquet revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS total FROM data GROUP BY region ORDER BY region;",
    )

    run_id = create_run(project["id"], "manual")
    execute_run(run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "succeeded"
    assert decode_json(run["result_json"], {})["rows"] == [["East", 210], ["West", 180]]


def test_s3_xlsx_snapshot_is_converted_to_an_isolated_csv_runtime_file(client, monkeypatch, sample_xlsx_bytes):
    secret_value = json.dumps(
        {"access_key_id": "reader", "secret_access_key": "xlsx-secret", "region": "us-east-1"}
    )
    monkeypatch.setenv("ANYDATAS_SECRET_S3_XLSX", secret_value)
    reference = create_postgres_secret_reference(client, "s3-xlsx", "ANYDATAS_SECRET_S3_XLSX")

    def fake_download(_value, _bucket, _key, destination, _max_bytes):
        destination.write_bytes(sample_xlsx_bytes)
        return {
            "size_bytes": len(sample_xlsx_bytes),
            "etag": "xlsx-etag",
            "version_id": "",
            "last_modified": "",
        }

    monkeypatch.setattr(s3_snapshots_module, "download_s3_object", fake_download)
    assert client.post(
        "/data-sources/s3",
        data={
            "name": "S3 workbook sales",
            "secret_id": reference["id"],
            "bucket_name": "analytics-data",
            "object_key": "exports/sales.xlsx",
        },
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'S3 workbook sales'").fetchone()
    connection = decode_json(source["connection_json"], {})
    assert connection["runtime_format"] == "csv"
    assert connection["sheet"] == "Sales"
    assert Path(connection["original_path"]).suffix == ".xlsx"
    assert Path(connection["original_path"]).exists()
    assert Path(source["path"]).suffix == ".csv"
    assert Path(source["path"]).exists()
    assert Path(source["path"]) != Path(connection["original_path"])

    project = create_project(client, "S3 workbook revenue", source["id"], "SELECT COUNT(*) FROM data;")
    run_id = create_run(project["id"], "manual")
    execute_run(run_id)
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "succeeded"
    assert decode_json(run["result_json"], {})["rows"] == [[3]]


def test_prepare_runtime_source_sanitizes_s3_snapshot_metadata(tmp_path):
    source = {
        "id": "s3-runtime-source",
        "source_type": "s3",
        "path": str(tmp_path / "sales.csv"),
        "connection_json": json.dumps(
            {
                "driver": "s3",
                "secret_id": "secret-reference-id",
                "bucket": "analytics-data",
                "object_key": "exports/sales.csv",
                "runtime_format": "csv",
                "etag": "etag-123",
                "version_id": "version-7",
                "original_path": "/private/control-plane/source.xlsx",
            }
        ),
    }

    runtime_source, secret_values, resolved = runner_module.prepare_runtime_source(
        None,
        DEFAULT_WORKSPACE_ID,
        source,
    )

    assert decode_json(runtime_source["connection_json"], {}) == {
        "driver": "s3",
        "runtime_format": "csv",
        "bucket": "analytics-data",
        "object_key": "exports/sales.csv",
        "etag": "etag-123",
        "version_id": "version-7",
    }
    assert secret_values == {}
    assert resolved == []


def test_docker_runner_keeps_s3_snapshots_off_network(monkeypatch, tmp_path):
    captured = {}
    source_path = tmp_path / "uploads" / "sales.csv"
    source_path.parent.mkdir()
    source_path.write_text("region,revenue\nEast,120\n", encoding="utf-8")
    source = {
        "id": "s3-docker-source",
        "source_type": "s3",
        "path": str(source_path),
        "connection_json": json.dumps(
            {
                "driver": "s3",
                "runtime_format": "csv",
                "bucket": "analytics-data",
                "object_key": "exports/sales.csv",
                "etag": "etag-123",
                "version_id": "version-7",
            }
        ),
    }

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("ANYDATAS_DOCKER_DATABASE_NETWORK", "external-databases")
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "read_runner_result", lambda *_args: ({"columns": [], "rows": []}, ""))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path / "runs")

    DockerRunner().run(
        {"language": "sql", "script": "SELECT * FROM data;"},
        source,
        "s3-docker-run",
        {},
        {},
    )

    command = captured["command"]
    assert command[command.index("--network") + 1] == "none"
    assert any("dst=/data,readonly" in item for item in command)
    assert "ANYDATAS_SOURCE_TYPE=s3" in command
    assert not any("S3_SECRET" in item or "secret_access_key" in item for item in command)


def test_clickhouse_wrapper_runs_typed_readonly_query(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)
    project = {"language": "sql", "script": "SELECT region, revenue FROM data WHERE revenue >= $minimum;"}
    run_path, result_path, wrapper_path = runner_module.prepare_run_files(
        project,
        "clickhouse-wrapper",
        "clickhouse",
        {"minimum": 100},
    )
    package_path = run_path / "clickhouse_connect"
    package_path.mkdir()
    (package_path / "__init__.py").write_text(
        """
import json
from pathlib import Path


class Result:
    column_names = ["region", "revenue"]
    result_rows = [("East", 120)]


class Client:
    def __init__(self, options):
        self.options = options
        self.calls = []

    def query(self, query, parameters=None, settings=None):
        self.calls.append({"query": query, "parameters": parameters, "settings": settings})
        return Result()

    def close(self):
        Path("clickhouse-calls.json").write_text(
            json.dumps({"options": self.options, "calls": self.calls}),
            encoding="utf-8",
        )


def get_client(**options):
    return Client(options)
""".strip(),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "ANYDATAS_LANGUAGE": "sql",
        "ANYDATAS_SOURCE_TYPE": "clickhouse",
        "ANYDATAS_SCRIPT": str(run_path / "main.sql"),
        "ANYDATAS_DATASET": "",
        "ANYDATAS_CONNECTION": '{"driver":"clickhouse","database":"analytics","table":"daily_sales","url_environment":"ANYDATAS_USER_SECRET_SOURCE_CLICKHOUSE_WRAPPER"}',
        "ANYDATAS_PARAMETERS_JSON": '{"minimum":100}',
        "ANYDATAS_OUTPUT": str(result_path),
        "ANYDATAS_CLICKHOUSE_QUERY_TIMEOUT_SECONDS": "45",
        "ANYDATAS_USER_SECRET_SOURCE_CLICKHOUSE_WRAPPER": "clickhouses://readonly:wrapper-secret@ch.example.com:8443/warehouse",
    }

    completed = subprocess.run(
        [sys.executable, str(wrapper_path)],
        cwd=run_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert decode_json(result_path.read_text(encoding="utf-8"), {}) == {
        "columns": ["region", "revenue"],
        "rows": [["East", 120]],
        "summary": {"rows": 1, "columns": 2},
    }
    calls = json.loads((run_path / "clickhouse-calls.json").read_text(encoding="utf-8"))
    assert calls["options"] == {
        "host": "ch.example.com",
        "port": 8443,
        "username": "readonly",
        "password": "wrapper-secret",
        "database": "analytics",
        "secure": True,
        "connect_timeout": 5,
        "send_receive_timeout": 5,
    }
    assert calls["calls"] == [
        {
            "query": "SELECT region, revenue FROM data WHERE revenue >= {minimum:Int64};",
            "parameters": {"minimum": 100},
            "settings": {
                "readonly": 1,
                "max_execution_time": 45,
                "max_result_rows": 500,
                "result_overflow_mode": "break",
            },
        }
    ]


def test_docker_runner_uses_configured_network_for_clickhouse_sources(monkeypatch, tmp_path):
    captured = {}
    connection_url = "clickhouse://readonly:docker-secret@ch.example.com/warehouse"
    source = {
        "id": "clickhouse-docker-source",
        "source_type": "clickhouse",
        "path": "",
        "connection_json": '{"driver":"clickhouse","database":"analytics","table":"daily_sales","url_environment":"ANYDATAS_USER_SECRET_SOURCE_CLICKHOUSE_DOCKER_SOURCE"}',
    }

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("ANYDATAS_DOCKER_DATABASE_NETWORK", "anydatas-database")
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "read_runner_result", lambda *_args: ({"columns": [], "rows": []}, ""))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)

    DockerRunner().run(
        {"language": "sql", "script": "SELECT $minimum;"},
        source,
        "clickhouse-docker-test",
        {"minimum": 1},
        {"ANYDATAS_USER_SECRET_SOURCE_CLICKHOUSE_DOCKER_SOURCE": connection_url},
    )

    command = captured["command"]
    assert command[command.index("--network") + 1] == "anydatas-database"
    assert not any("dst=/data" in item for item in command)
    assert "ANYDATAS_CLICKHOUSE_QUERY_TIMEOUT_SECONDS=45" in command
    assert f"ANYDATAS_USER_SECRET_SOURCE_CLICKHOUSE_DOCKER_SOURCE={connection_url}" in command
    assert (tmp_path / "clickhouse-docker-test/main.sql").read_text(encoding="utf-8") == "SELECT {minimum:Int64};"


def test_docker_runner_rejects_clickhouse_sources_without_a_configured_network(monkeypatch, tmp_path):
    monkeypatch.delenv("ANYDATAS_DOCKER_DATABASE_NETWORK", raising=False)
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="ClickHouse data sources require ANYDATAS_DOCKER_DATABASE_NETWORK"):
        DockerRunner().run(
            {"language": "sql", "script": "SELECT 1;"},
            {
                "id": "clickhouse-no-network",
                "source_type": "clickhouse",
                "path": "",
                "connection_json": '{"driver":"clickhouse","database":"analytics","table":"events"}',
            },
            "clickhouse-no-network-run",
            {},
            {},
        )


def test_prepare_run_files_rewrites_and_validates_mysql_sql(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)
    project = {
        "language": "sql",
        "script": "# $not_a_parameter\nSELECT $minimum AS value, '$literal' AS literal_value;",
    }

    run_path, _result_path, _wrapper_path = runner_module.prepare_run_files(project, "mysql-rewrite", "mysql")

    assert (run_path / "main.sql").read_text(encoding="utf-8") == "# $not_a_parameter\nSELECT %(minimum)s AS value, '$literal' AS literal_value;"
    with pytest.raises(ValueError, match="executable comments"):
        runner_module.prepare_run_files(
            {"language": "sql", "script": "SELECT 1 /*!50000 FOR UPDATE */"},
            "mysql-rewrite-rejected",
            "mysql",
        )


def test_mysql_wrapper_runs_a_read_only_parameterized_query(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)
    project = {"language": "sql", "script": "SELECT region, revenue FROM data WHERE revenue >= $minimum;"}
    run_path, result_path, wrapper_path = runner_module.prepare_run_files(project, "mysql-wrapper", "mysql")
    package_path = run_path / "pymysql"
    package_path.mkdir()
    (package_path / "__init__.py").write_text(
        """
import json
from pathlib import Path


class Cursor:
    description = [("region",), ("revenue",)]

    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.calls.append({"statement": str(statement), "params": params})

    def fetchmany(self, _limit):
        return [("East", 120)]

    def fetchall(self):
        return [("East", 120)]


class Connection:
    def __init__(self, options):
        self.options = options
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        Path("pymysql-calls.json").write_text(json.dumps({"options": self.options, "calls": self.calls}), encoding="utf-8")
        return False

    def cursor(self):
        return Cursor(self.calls)


def connect(**options):
    return Connection(options)
""".strip(),
        encoding="utf-8",
    )
    connection_url = "mysql://readonly:wrapper-secret@db.example.com:3307/warehouse"
    environment = {
        **os.environ,
        "ANYDATAS_LANGUAGE": "sql",
        "ANYDATAS_SOURCE_TYPE": "mysql",
        "ANYDATAS_SCRIPT": str(run_path / "main.sql"),
        "ANYDATAS_DATASET": "",
        "ANYDATAS_CONNECTION": '{"driver":"mysql","database":"analytics","table":"daily_sales","url_environment":"ANYDATAS_USER_SECRET_SOURCE_MYSQL_WRAPPER"}',
        "ANYDATAS_PARAMETERS_JSON": '{"minimum":100}',
        "ANYDATAS_OUTPUT": str(result_path),
        "ANYDATAS_MYSQL_STATEMENT_TIMEOUT_MS": "45000",
        "ANYDATAS_USER_SECRET_SOURCE_MYSQL_WRAPPER": connection_url,
    }

    completed = subprocess.run(
        [sys.executable, str(wrapper_path)],
        cwd=run_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert decode_json(result_path.read_text(encoding="utf-8"), {}) == {
        "columns": ["region", "revenue"],
        "rows": [["East", 120]],
        "summary": {"rows": 1, "columns": 2},
    }
    calls = json.loads((run_path / "pymysql-calls.json").read_text(encoding="utf-8"))
    assert calls == {
        "options": {
            "host": "db.example.com",
            "port": 3307,
            "user": "readonly",
            "password": "wrapper-secret",
            "database": "analytics",
            "charset": "utf8mb4",
            "connect_timeout": 5,
            "read_timeout": 5,
            "write_timeout": 5,
            "autocommit": False,
        },
        "calls": [
            {"statement": "SET TRANSACTION READ ONLY", "params": None},
            {"statement": "START TRANSACTION READ ONLY", "params": None},
            {"statement": "SET SESSION MAX_EXECUTION_TIME = %s", "params": [45000]},
            {"statement": "SELECT region, revenue FROM data WHERE revenue >= %(minimum)s;", "params": {"minimum": 100}},
        ],
    }

    python_script = run_path / "main.py"
    python_result_path = run_path / "python-result.json"
    python_script.write_text("result = load_data()", encoding="utf-8")
    python_environment = {
        **environment,
        "ANYDATAS_LANGUAGE": "python",
        "ANYDATAS_SCRIPT": str(python_script),
        "ANYDATAS_OUTPUT": str(python_result_path),
    }
    python_completed = subprocess.run(
        [sys.executable, str(wrapper_path)],
        cwd=run_path,
        env=python_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert python_completed.returncode == 0, python_completed.stderr
    assert decode_json(python_result_path.read_text(encoding="utf-8"), {}) == {
        "columns": ["region", "revenue"],
        "rows": [["East", 120]],
        "summary": {"rows": 1, "columns": 2},
    }
    python_calls = json.loads((run_path / "pymysql-calls.json").read_text(encoding="utf-8"))
    assert python_calls["calls"] == [
        {"statement": "SET TRANSACTION READ ONLY", "params": None},
        {"statement": "START TRANSACTION READ ONLY", "params": None},
        {"statement": "SET SESSION MAX_EXECUTION_TIME = %s", "params": [45000]},
        {"statement": "SELECT * FROM `analytics`.`daily_sales`", "params": None},
    ]


def test_prepare_run_files_rewrites_postgres_dollar_parameters(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)
    project = {
        "language": "sql",
        "script": "SELECT $minimum AS value, '$literal' AS literal_value;",
    }

    run_path, _result_path, _wrapper_path = runner_module.prepare_run_files(project, "postgres-rewrite", "postgres")

    assert (run_path / "main.sql").read_text(encoding="utf-8") == "SELECT %(minimum)s AS value, '$literal' AS literal_value;"

    with pytest.raises(ValueError, match="exactly one"):
        runner_module.prepare_run_files(
            {"language": "sql", "script": "SELECT 1; DELETE FROM daily_sales;"},
            "postgres-rewrite-rejected",
            "postgres",
        )


def test_postgres_wrapper_runs_a_read_only_parameterized_query(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)
    project = {"language": "sql", "script": "SELECT region, revenue FROM data WHERE revenue >= $minimum;"}
    run_path, result_path, wrapper_path = runner_module.prepare_run_files(project, "postgres-wrapper", "postgres")
    package_path = run_path / "psycopg"
    package_path.mkdir()
    (package_path / "__init__.py").write_text(
        """
import json
from pathlib import Path


class Field:
    def __init__(self, name):
        self.name = name


class Cursor:
    description = [Field("region"), Field("revenue")]

    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.calls.append({"statement": str(statement), "params": params})

    def fetchmany(self, _limit):
        return [("East", 120)]

    def fetchall(self):
        return [("East", 120)]


class Connection:
    def __init__(self, url):
        self.url = url
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        Path("psycopg-calls.json").write_text(json.dumps({"url": self.url, "calls": self.calls}), encoding="utf-8")
        return False

    def cursor(self):
        return Cursor(self.calls)


def connect(url):
    return Connection(url)
""".strip(),
        encoding="utf-8",
    )
    (package_path / "sql.py").write_text(
        """
class Identifier:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return '"' + self.value.replace('"', '""') + '"'


class SQL:
    def __init__(self, value):
        self.value = value

    def format(self, *items):
        return SQL(self.value.format(*[str(item) for item in items]))

    def __str__(self):
        return self.value
""".strip(),
        encoding="utf-8",
    )
    connection_url = "postgresql://readonly:wrapper-secret@db.example.com/warehouse"
    environment = {
        **os.environ,
        "ANYDATAS_LANGUAGE": "sql",
        "ANYDATAS_SOURCE_TYPE": "postgres",
        "ANYDATAS_SCRIPT": str(run_path / "main.sql"),
        "ANYDATAS_DATASET": "",
        "ANYDATAS_CONNECTION": '{"driver":"postgres","schema":"analytics","table":"daily_sales","url_environment":"ANYDATAS_USER_SECRET_SOURCE_POSTGRES_WRAPPER"}',
        "ANYDATAS_PARAMETERS_JSON": '{"minimum":100}',
        "ANYDATAS_OUTPUT": str(result_path),
        "ANYDATAS_POSTGRES_STATEMENT_TIMEOUT_MS": "45000",
        "ANYDATAS_USER_SECRET_SOURCE_POSTGRES_WRAPPER": connection_url,
    }

    completed = subprocess.run(
        [sys.executable, str(wrapper_path)],
        cwd=run_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert decode_json(result_path.read_text(encoding="utf-8"), {}) == {
        "columns": ["region", "revenue"],
        "rows": [["East", 120]],
        "summary": {"rows": 1, "columns": 2},
    }
    calls = json.loads((run_path / "psycopg-calls.json").read_text(encoding="utf-8"))
    assert calls == {
        "url": connection_url,
        "calls": [
            {"statement": "SET TRANSACTION READ ONLY", "params": None},
            {"statement": "SET LOCAL statement_timeout TO 45000", "params": None},
            {"statement": 'SELECT region, revenue FROM data WHERE revenue >= %(minimum)s;', "params": {"minimum": 100}},
        ],
    }

    python_script = run_path / "main.py"
    python_result_path = run_path / "python-result.json"
    python_script.write_text("result = load_data()", encoding="utf-8")
    python_environment = {
        **environment,
        "ANYDATAS_LANGUAGE": "python",
        "ANYDATAS_SCRIPT": str(python_script),
        "ANYDATAS_OUTPUT": str(python_result_path),
    }
    python_completed = subprocess.run(
        [sys.executable, str(wrapper_path)],
        cwd=run_path,
        env=python_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert python_completed.returncode == 0, python_completed.stderr
    assert decode_json(python_result_path.read_text(encoding="utf-8"), {}) == {
        "columns": ["region", "revenue"],
        "rows": [["East", 120]],
        "summary": {"rows": 1, "columns": 2},
    }
    python_calls = json.loads((run_path / "psycopg-calls.json").read_text(encoding="utf-8"))
    assert python_calls == {
        "url": connection_url,
        "calls": [
            {"statement": "SET TRANSACTION READ ONLY", "params": None},
            {"statement": "SET LOCAL statement_timeout TO 45000", "params": None},
            {"statement": 'SELECT * FROM "analytics"."daily_sales"', "params": None},
        ],
    }


def test_docker_runner_uses_configured_network_for_postgres_sources(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}
    connection_url = "postgresql://readonly:docker-secret@db.example.com/warehouse"
    source = {
        "id": "postgres-docker-source",
        "source_type": "postgres",
        "path": "",
        "connection_json": '{"driver":"postgres","schema":"analytics","table":"daily_sales","url_environment":"ANYDATAS_USER_SECRET_SOURCE_POSTGRES_DOCKER_SOURCE"}',
    }

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("ANYDATAS_DOCKER_DATABASE_NETWORK", "anydatas-database")
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "read_runner_result", lambda _proc, _result_path: ({"columns": [], "rows": []}, ""))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)

    DockerRunner().run(
        {"language": "sql", "script": "SELECT $minimum;"},
        source,
        "postgres-docker-test",
        {"minimum": 1},
        {"ANYDATAS_USER_SECRET_SOURCE_POSTGRES_DOCKER_SOURCE": connection_url},
    )

    command = captured["command"]
    assert command[command.index("--network") + 1] == "anydatas-database"
    assert not any("dst=/data" in item for item in command)
    assert "ANYDATAS_DATASET=" in command
    assert f"ANYDATAS_USER_SECRET_SOURCE_POSTGRES_DOCKER_SOURCE={connection_url}" in command
    assert (tmp_path / "postgres-docker-test" / "main.sql").read_text(encoding="utf-8") == "SELECT %(minimum)s;"


def test_docker_runner_uses_configured_network_for_mysql_sources(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}
    connection_url = "mysql://readonly:docker-secret@db.example.com/warehouse"
    source = {
        "id": "mysql-docker-source",
        "source_type": "mysql",
        "path": "",
        "connection_json": '{"driver":"mysql","database":"analytics","table":"daily_sales","url_environment":"ANYDATAS_USER_SECRET_SOURCE_MYSQL_DOCKER_SOURCE"}',
    }

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("ANYDATAS_DOCKER_DATABASE_NETWORK", "anydatas-database")
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "read_runner_result", lambda _proc, _result_path: ({"columns": [], "rows": []}, ""))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)

    DockerRunner().run(
        {"language": "sql", "script": "SELECT $minimum;"},
        source,
        "mysql-docker-test",
        {"minimum": 1},
        {"ANYDATAS_USER_SECRET_SOURCE_MYSQL_DOCKER_SOURCE": connection_url},
    )

    command = captured["command"]
    assert command[command.index("--network") + 1] == "anydatas-database"
    assert not any("dst=/data" in item for item in command)
    assert "ANYDATAS_DATASET=" in command
    assert "ANYDATAS_MYSQL_STATEMENT_TIMEOUT_MS=45000" in command
    assert f"ANYDATAS_USER_SECRET_SOURCE_MYSQL_DOCKER_SOURCE={connection_url}" in command
    assert (tmp_path / "mysql-docker-test" / "main.sql").read_text(encoding="utf-8") == "SELECT %(minimum)s;"


def test_docker_runner_rejects_postgres_sources_without_a_configured_network(monkeypatch, tmp_path):
    monkeypatch.delenv("ANYDATAS_DOCKER_DATABASE_NETWORK", raising=False)
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="ANYDATAS_DOCKER_DATABASE_NETWORK"):
        DockerRunner().run(
            {"language": "sql", "script": "SELECT 1;"},
            {"source_type": "postgres", "path": "", "connection_json": "{}"},
            "postgres-network-required",
            {},
        )


def test_docker_runner_rejects_mysql_sources_without_a_configured_network(monkeypatch, tmp_path):
    monkeypatch.delenv("ANYDATAS_DOCKER_DATABASE_NETWORK", raising=False)
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="MySQL data sources require ANYDATAS_DOCKER_DATABASE_NETWORK"):
        DockerRunner().run(
            {"language": "sql", "script": "SELECT 1;"},
            {"source_type": "mysql", "path": "", "connection_json": "{}"},
            "mysql-network-required",
            {},
        )


def test_parquet_data_source_sql_project_run_succeeds(client, sample_parquet_bytes):
    source = upload_source(
        client,
        "parquet sales",
        sample_parquet_bytes,
        "sales.parquet",
        "application/octet-stream",
    )
    project = create_project(
        client,
        "parquet regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    assert source["source_type"] == "parquet"
    quality = decode_json(source["quality_json"], {})
    assert quality["row_count"] == 3
    assert quality["empty_cells"] == 0
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"] == [["East", 210], ["West", 180]]


def test_xlsx_data_source_sql_project_run_succeeds(client, sample_xlsx_bytes):
    source = upload_source(
        client,
        "xlsx sales",
        sample_xlsx_bytes,
        "sales.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    project = create_project(
        client,
        "xlsx regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    assert source["source_type"] == "xlsx"
    connection = decode_json(source["connection_json"], {})
    assert connection["sheet"] == "Sales"
    quality = decode_json(source["quality_json"], {})
    assert quality["row_count"] == 3
    assert quality["empty_cells"] == 0
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"] == [["East", 210], ["West", 180]]


def test_python_project_run_succeeds(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(
        client,
        "python revenue",
        source["id"],
        "rows = load_csv()\nresult = [{'region': row['region'], 'revenue': int(row['revenue'])} for row in rows[:2]]",
        "python",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"][0] == ["East", 120]


def test_parquet_data_source_python_project_loads_rows(client, sample_parquet_bytes):
    source = upload_source(
        client,
        "python parquet sales",
        sample_parquet_bytes,
        "sales.parquet",
        "application/octet-stream",
    )
    project = create_project(
        client,
        "parquet python revenue",
        source["id"],
        "rows = load_parquet()\nresult = [{'region': row['region'], 'revenue': row['revenue']} for row in rows[:2]]",
        "python",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"][0] == ["East", 120]


def test_xlsx_data_source_python_project_loads_rows(client, sample_xlsx_bytes):
    source = upload_source(
        client,
        "python xlsx sales",
        sample_xlsx_bytes,
        "sales.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    project = create_project(
        client,
        "xlsx python revenue",
        source["id"],
        "rows = load_xlsx()\nresult = [{'region': row['region'], 'revenue': int(row['revenue'])} for row in rows[:2]]",
        "python",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"][0] == ["East", 120]


def test_sqlite_data_source_python_project_loads_rows(client, tmp_path):
    database_path = tmp_path / "warehouse.sqlite3"
    create_sample_sqlite(database_path)
    source = connect_sqlite_source(client, "python warehouse sales", database_path)
    project = create_project(
        client,
        "sqlite python revenue",
        source["id"],
        "rows = load_data()\nresult = [{'region': row['region'], 'revenue': row['revenue']} for row in rows[:2]]",
        "python",
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    result = decode_json(run["result_json"], {})
    assert result["columns"] == ["region", "revenue"]
    assert result["rows"][0] == ["East", 120]


def test_failed_run_records_error(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "bad sql", source["id"], "SELECT missing_column FROM data;")

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "failed"
    assert "missing_column" in run["error"] or "Binder Error" in run["error"]
    detail_response = client.get(f"/runs/{run['id']}")
    assert detail_response.status_code == 200
    assert "Error Summary" in detail_response.text
    assert "missing_column" in detail_response.text or "Binder Error" in detail_response.text

    notifications_response = client.get("/api/notifications")
    assert notifications_response.status_code == 200
    notifications = notifications_response.json()
    assert notifications[0]["event_type"] == "run.failed"
    assert notifications[0]["resource_type"] == "run"
    assert notifications[0]["resource_id"] == run["id"]
    assert notifications[0]["severity"] == "error"
    assert notifications[0]["is_read"] == 0

    read_response = client.post(f"/notifications/{notifications[0]['id']}/read", follow_redirects=False)
    assert read_response.status_code == 303
    with connect() as conn:
        notification = conn.execute("SELECT * FROM notifications WHERE id = ?", (notifications[0]["id"],)).fetchone()
    assert notification["is_read"] == 1


def test_schedule_claim_creates_and_executes_run(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "scheduled", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "fast", "interval_minutes": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE project_id = ?", (now_iso(), project["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    assert claim_queued_schedule_runs() == [claimed[0]["run_id"]]
    execute_run(claimed[0]["run_id"])

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
    assert run["trigger_type"] == "schedule"
    assert run["status"] == "succeeded"


def test_skip_concurrency_policy_skips_due_schedule_with_active_run(client, sample_csv_bytes):
    source = upload_source(client, "skip policy sales", sample_csv_bytes)
    project = create_project(client, "skip policy project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "skip policy", "interval_minutes": 60, "concurrency_policy": "skip"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, schedule_id, parameters_json, started_at)
            VALUES ('skip-active-run', ?, 'canceling', 'schedule', ?, '{}', ?)
            """,
            (project["id"], schedule["id"], now_iso()),
        )
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    assert claim_due_schedules() == []

    with connect() as conn:
        updated_schedule = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
        runs = conn.execute("SELECT * FROM runs WHERE schedule_id = ?", (schedule["id"],)).fetchall()
        audit_event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'schedule.run_skipped' AND resource_id = ?",
            (schedule["id"],),
        ).fetchone()
    assert len(runs) == 1
    assert updated_schedule["last_run_at"] is None
    assert updated_schedule["next_run_at"] > now_iso()
    assert decode_json(audit_event["detail_json"], {})["concurrency_policy"] == "skip"


def test_queue_one_concurrency_policy_keeps_only_one_pending_run(client, sample_csv_bytes):
    source = upload_source(client, "queue one sales", sample_csv_bytes)
    project = create_project(client, "queue one project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "queue one", "interval_minutes": 60, "concurrency_policy": "queue_one"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, schedule_id, parameters_json, started_at)
            VALUES ('queue-one-active-run', ?, 'running', 'schedule', ?, '{}', ?)
            """,
            (project["id"], schedule["id"], now_iso()),
        )
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    with connect() as conn:
        queued_run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    assert claim_queued_schedule_runs() == []
    assert claim_due_schedules() == []

    with connect() as conn:
        runs = conn.execute("SELECT * FROM runs WHERE schedule_id = ? ORDER BY started_at", (schedule["id"],)).fetchall()
        skipped_audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'schedule.run_skipped' AND resource_id = ?",
            (schedule["id"],),
        ).fetchone()
    assert queued_run["status"] == "queued"
    assert [run["status"] for run in runs] == ["running", "queued"]
    assert decode_json(skipped_audit["detail_json"], {})["reason"] == "queued_run"

    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'canceling' WHERE id = 'queue-one-active-run'")
    assert claim_queued_schedule_runs() == []
    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'canceled' WHERE id = 'queue-one-active-run'")
    assert claim_queued_schedule_runs() == [queued_run["id"]]
    with connect() as conn:
        claimed_run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run["id"],)).fetchone()
    assert claimed_run["status"] == "running"


def test_queue_all_concurrency_policy_preserves_each_due_run_and_drains_serially(client, sample_csv_bytes):
    source = upload_source(client, "queue all sales", sample_csv_bytes)
    project = create_project(client, "queue all project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "queue all", "interval_minutes": 60, "concurrency_policy": "queue_all"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    first_due = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    second_due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, schedule_id, parameters_json, started_at)
            VALUES ('queue-all-active-run', ?, 'running', 'schedule', ?, '{}', ?)
            """,
            (project["id"], schedule["id"], first_due),
        )
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (first_due, schedule["id"]))

    first_claimed = claim_due_schedules()
    assert len(first_claimed) == 1
    with connect() as conn:
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (second_due, schedule["id"]))

    second_claimed = claim_due_schedules()
    assert len(second_claimed) == 1
    assert claim_queued_schedule_runs() == []
    with connect() as conn:
        first_run = conn.execute("SELECT * FROM runs WHERE id = ?", (first_claimed[0]["run_id"],)).fetchone()
        second_run = conn.execute("SELECT * FROM runs WHERE id = ?", (second_claimed[0]["run_id"],)).fetchone()
        queued_audits = conn.execute(
            "SELECT detail_json FROM audit_events WHERE action = 'run.queued' AND resource_id IN (?, ?) ORDER BY created_at",
            (first_run["id"], second_run["id"]),
        ).fetchall()
        conn.execute("UPDATE runs SET status = 'canceled' WHERE id = 'queue-all-active-run'")

    assert [first_run["status"], second_run["status"]] == ["queued", "queued"]
    assert [first_run["scheduled_for_at"], second_run["scheduled_for_at"]] == [first_due, second_due]
    assert [decode_json(audit["detail_json"], {})["concurrency_policy"] for audit in queued_audits] == ["queue_all", "queue_all"]
    assert claim_queued_schedule_runs() == [first_run["id"]]
    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded', finished_at = ? WHERE id = ?", (now_iso(), first_run["id"]))
    assert claim_queued_schedule_runs() == [second_run["id"]]


def test_cancel_previous_concurrency_policy_supersedes_active_runs(client, sample_csv_bytes, monkeypatch):
    source = upload_source(client, "cancel previous sales", sample_csv_bytes)
    project = create_project(client, "cancel previous project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "cancel previous", "interval_minutes": 60, "concurrency_policy": "cancel_previous"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    due_at = now_iso()
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.executemany(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, schedule_id, parameters_json, started_at)
            VALUES (?, ?, ?, 'schedule', ?, '{}', ?)
            """,
            [
                ("cancel-previous-queued", project["id"], "queued", schedule["id"], due_at),
                ("cancel-previous-running", project["id"], "running", schedule["id"], due_at),
                ("cancel-previous-canceling", project["id"], "canceling", schedule["id"], due_at),
            ],
        )
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (due_at, schedule["id"]))

    cancellation_calls: list[str] = []
    monkeypatch.setattr(main_module, "cancel_run_execution", lambda run_id: cancellation_calls.append(run_id) or True)

    claimed = claim_due_schedules()

    assert len(claimed) == 1
    assert set(cancellation_calls) == {"cancel-previous-running", "cancel-previous-canceling"}
    with connect() as conn:
        runs = {
            run["id"]: run
            for run in conn.execute("SELECT * FROM runs WHERE schedule_id = ?", (schedule["id"],)).fetchall()
        }
        superseded_event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'schedule.run_superseded' AND resource_id = ?",
            (schedule["id"],),
        ).fetchone()
    latest_run = runs[claimed[0]["run_id"]]
    assert runs["cancel-previous-queued"]["status"] == "canceled"
    assert runs["cancel-previous-running"]["status"] == "canceling"
    assert runs["cancel-previous-canceling"]["status"] == "canceling"
    assert latest_run["status"] == "queued"
    assert latest_run["scheduled_for_at"] == due_at
    assert claim_queued_schedule_runs() == []
    superseded_detail = decode_json(superseded_event["detail_json"], {})
    assert superseded_detail["concurrency_policy"] == "cancel_previous"
    assert superseded_detail["canceled_run_ids"] == ["cancel-previous-queued"]
    assert set(superseded_detail["cancel_requested_run_ids"]) == {
        "cancel-previous-running",
        "cancel-previous-canceling",
    }

    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'canceled' WHERE id IN ('cancel-previous-running', 'cancel-previous-canceling')"
        )
    assert claim_queued_schedule_runs() == [latest_run["id"]]


def test_schedule_backfill_queues_interval_occurrences_without_refreshing_reports(client, sample_csv_bytes):
    source = upload_source(client, "backfill sales", sample_csv_bytes)
    project = create_project(client, "backfill project", source["id"], "SELECT * FROM data LIMIT 1;")
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Backfill report", "description": "Keep the current snapshot"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "hourly backfill", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", schedule["id"]))

    response = client.post(
        f"/schedules/{schedule['id']}/backfill",
        data={"start_at": "2026-01-01T00:00", "end_at": "2026-01-01T03:00", "max_runs": 4},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Queued%204%20backfill%20runs" in response.headers["location"]
    with connect() as conn:
        backfill_runs = conn.execute(
            "SELECT * FROM runs WHERE schedule_id = ? AND trigger_type = 'schedule_backfill' ORDER BY scheduled_for_at",
            (schedule["id"],),
        ).fetchall()
        backfill_event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'schedule.backfill_queued' AND resource_id = ?",
            (schedule["id"],),
        ).fetchone()
    assert [run["scheduled_for_at"] for run in backfill_runs] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T01:00:00+00:00",
        "2026-01-01T02:00:00+00:00",
        "2026-01-01T03:00:00+00:00",
    ]
    assert all(decode_json(run["parameters_json"], {})["__anydatas_scheduled_for"] == run["scheduled_for_at"] for run in backfill_runs)
    assert decode_json(backfill_event["detail_json"], {})["run_count"] == 4

    claimed = claim_queued_schedule_runs()
    assert claimed == [backfill_runs[0]["id"]]
    execute_run(claimed[0])
    with connect() as conn:
        completed_run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0],)).fetchone()
        snapshot_count = conn.execute("SELECT COUNT(*) AS count FROM report_snapshots WHERE report_id = ?", (report_id,)).fetchone()
    assert completed_run["status"] == "succeeded"
    assert snapshot_count["count"] == 0


def test_schedule_backfill_rejects_ranges_that_exceed_the_requested_limit(client, sample_csv_bytes):
    source = upload_source(client, "bounded backfill sales", sample_csv_bytes)
    project = create_project(client, "bounded backfill project", source["id"], "SELECT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "bounded backfill", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", schedule["id"]))

    response = client.post(
        f"/schedules/{schedule['id']}/backfill",
        data={"start_at": "2026-01-01T00:00", "end_at": "2026-01-01T03:00", "max_runs": 3},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Backfill%20range%20produces%20more%20than%203%20runs" in response.headers["location"]
    with connect() as conn:
        backfill_count = conn.execute(
            "SELECT COUNT(*) AS count FROM runs WHERE schedule_id = ? AND trigger_type = 'schedule_backfill'",
            (schedule["id"],),
        ).fetchone()
    assert backfill_count["count"] == 0


def test_schedule_backfill_treats_minute_precision_end_as_inclusive(client, sample_csv_bytes):
    source = upload_source(client, "minute backfill sales", sample_csv_bytes)
    project = create_project(client, "minute backfill project", source["id"], "SELECT 1;")
    assert client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "minute backfill", "interval_minutes": 60},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:30+00:00", schedule["id"]))

    response = client.post(
        f"/schedules/{schedule['id']}/backfill",
        data={"start_at": "2026-01-01T00:00", "end_at": "2026-01-01T00:00", "max_runs": 1},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute(
            "SELECT * FROM runs WHERE schedule_id = ? AND trigger_type = 'schedule_backfill'",
            (schedule["id"],),
        ).fetchone()
    assert run["scheduled_for_at"] == "2026-01-01T00:00:30+00:00"


def test_failed_schedule_backfill_retries_without_refreshing_reports(client, sample_csv_bytes):
    source = upload_source(client, "backfill retry sales", sample_csv_bytes)
    project = create_project(client, "backfill retry project", source["id"], "SELECT missing_column FROM data;")
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Backfill retry report", "description": "No historical snapshots"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    assert client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "backfill retry", "interval_minutes": 60, "max_retries": 1},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", schedule["id"]))

    assert client.post(
        f"/schedules/{schedule['id']}/backfill",
        data={"start_at": "2026-01-01T00:00", "end_at": "2026-01-01T00:00", "max_runs": 1},
        follow_redirects=False,
    ).status_code == 303
    initial_run_id = claim_queued_schedule_runs()[0]
    execute_run(initial_run_id)

    with connect() as conn:
        initial_run = conn.execute("SELECT * FROM runs WHERE id = ?", (initial_run_id,)).fetchone()
        retry_run = conn.execute("SELECT * FROM runs WHERE retry_of_run_id = ?", (initial_run_id,)).fetchone()
        conn.execute("UPDATE runs SET next_attempt_at = ? WHERE id = ?", (now_iso(), retry_run["id"]))
    assert initial_run["status"] == "failed"
    assert retry_run["trigger_type"] == "schedule_backfill_retry"
    assert retry_run["scheduled_for_at"] == initial_run["scheduled_for_at"]

    assert claim_due_retries() == [retry_run["id"]]
    execute_run(retry_run["id"])
    with connect() as conn:
        snapshot_count = conn.execute("SELECT COUNT(*) AS count FROM report_snapshots WHERE report_id = ?", (report_id,)).fetchone()
    assert snapshot_count["count"] == 0


def test_scheduled_retry_waits_for_an_active_schedule_run(client, sample_csv_bytes):
    source = upload_source(client, "retry concurrency sales", sample_csv_bytes)
    project = create_project(client, "retry concurrency project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "retry concurrency", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute(
            """
            INSERT INTO runs (id, project_id, status, trigger_type, schedule_id, parameters_json, started_at)
            VALUES ('retry-concurrency-active', ?, 'canceling', 'schedule', ?, '{}', ?)
            """,
            (project["id"], schedule["id"], now_iso()),
        )
        conn.execute(
            """
            INSERT INTO runs (
                id, project_id, status, trigger_type, schedule_id, attempt, retry_of_run_id,
                next_attempt_at, parameters_json, started_at
            )
            VALUES ('retry-concurrency-pending', ?, 'queued', 'schedule_retry', ?, 2, 'retry-concurrency-active', ?, '{}', ?)
            """,
            (project["id"], schedule["id"], now_iso(), now_iso()),
        )

    assert claim_due_retries() == []
    with connect() as conn:
        conn.execute("UPDATE runs SET status = 'succeeded' WHERE id = 'retry-concurrency-active'")
    assert claim_due_retries() == ["retry-concurrency-pending"]

    with connect() as conn:
        retry_run = conn.execute("SELECT * FROM runs WHERE id = 'retry-concurrency-pending'").fetchone()
    assert retry_run["status"] == "running"


def test_failed_scheduled_run_retries_then_notifies_after_final_attempt(client, sample_csv_bytes):
    source = upload_source(client, "retry sales", sample_csv_bytes)
    project = create_project(
        client,
        "retry project",
        source["id"],
        "SELECT missing_column FROM data WHERE region = $region;",
        parameters_json='{"region": "East"}',
    )
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Retry report", "description": "Wait for final failure"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "retry schedule",
            "interval_minutes": 60,
            "max_retries": 1,
            "retry_delay_minutes": 1,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    execute_run(claimed[0]["run_id"])

    with connect() as conn:
        initial_run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
        retry_run = conn.execute("SELECT * FROM runs WHERE retry_of_run_id = ?", (initial_run["id"],)).fetchone()
        notification_count = conn.execute("SELECT COUNT(*) AS count FROM notifications").fetchone()["count"]
        intermediate_snapshot_count = conn.execute(
            "SELECT COUNT(*) AS count FROM report_snapshots WHERE report_id = ?",
            (report_id,),
        ).fetchone()["count"]
    assert initial_run["status"] == "failed"
    assert initial_run["schedule_id"] == schedule["id"]
    assert initial_run["attempt"] == 1
    assert retry_run is not None
    assert retry_run["status"] == "queued"
    assert retry_run["trigger_type"] == "schedule_retry"
    assert retry_run["schedule_id"] == schedule["id"]
    assert retry_run["attempt"] == 2
    assert decode_json(retry_run["parameters_json"], {}) == {"region": "East"}
    assert retry_run["next_attempt_at"] > initial_run["finished_at"]
    assert notification_count == 0
    assert intermediate_snapshot_count == 0

    with connect() as conn:
        conn.execute("UPDATE runs SET next_attempt_at = ? WHERE id = ?", (now_iso(), retry_run["id"]))
    claimed_retries = claim_due_retries()
    assert claimed_retries == [retry_run["id"]]
    execute_run(retry_run["id"])

    with connect() as conn:
        final_retry = conn.execute("SELECT * FROM runs WHERE id = ?", (retry_run["id"],)).fetchone()
        retries = conn.execute("SELECT * FROM runs WHERE retry_of_run_id = ?", (retry_run["id"],)).fetchall()
        notification = conn.execute("SELECT * FROM notifications WHERE resource_id = ?", (retry_run["id"],)).fetchone()
        final_snapshot = conn.execute(
            "SELECT * FROM report_snapshots WHERE report_id = ? AND run_id = ?",
            (report_id, retry_run["id"]),
        ).fetchone()
    assert final_retry["status"] == "failed"
    assert retries == []
    assert notification is not None
    assert notification["event_type"] == "run.failed"
    assert final_snapshot is not None
    assert final_snapshot["status"] == "failed"


def test_scheduled_retries_use_exponential_delay(client, sample_csv_bytes):
    source = upload_source(client, "backoff sales", sample_csv_bytes)
    project = create_project(client, "backoff project", source["id"], "SELECT missing_column FROM data;")
    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "backoff schedule",
            "interval_minutes": 60,
            "max_retries": 2,
            "retry_delay_minutes": 2,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    initial_run_id = claim_due_schedules()[0]["run_id"]
    execute_run(initial_run_id)
    with connect() as conn:
        first_retry = conn.execute("SELECT * FROM runs WHERE retry_of_run_id = ?", (initial_run_id,)).fetchone()
        conn.execute("UPDATE runs SET next_attempt_at = ? WHERE id = ?", (now_iso(), first_retry["id"]))

    assert claim_due_retries() == [first_retry["id"]]
    execute_run(first_retry["id"])
    with connect() as conn:
        completed_first_retry = conn.execute("SELECT * FROM runs WHERE id = ?", (first_retry["id"],)).fetchone()
        second_retry = conn.execute("SELECT * FROM runs WHERE retry_of_run_id = ?", (first_retry["id"],)).fetchone()
    assert second_retry["attempt"] == 3
    assert datetime.fromisoformat(second_retry["next_attempt_at"]) - datetime.fromisoformat(completed_first_retry["finished_at"]) == timedelta(minutes=4)


def test_schedule_retries_validate_bounds(client, sample_csv_bytes):
    source = upload_source(client, "retry validation sales", sample_csv_bytes)
    project = create_project(client, "retry validation project", source["id"], "SELECT 1;")

    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "invalid retries",
            "interval_minutes": 60,
            "max_retries": 11,
            "retry_delay_minutes": 1,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Retries must be between 0 and 10." in unquote(response.headers["location"])

    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "invalid retry delay",
            "interval_minutes": 60,
            "max_retries": 1,
            "retry_delay_minutes": 0,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Retry delay must be between 1 and 1440 minutes." in unquote(response.headers["location"])
    with connect() as conn:
        schedule_count = conn.execute("SELECT COUNT(*) AS count FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
    assert schedule_count["count"] == 0


def test_schedule_rejects_unknown_concurrency_policy(client, sample_csv_bytes):
    source = upload_source(client, "concurrency validation sales", sample_csv_bytes)
    project = create_project(client, "concurrency validation project", source["id"], "SELECT 1;")

    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "invalid concurrency",
            "interval_minutes": 60,
            "concurrency_policy": "allow_parallel",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Concurrency%20policy%20must%20be%20skip,%20queue_one,%20queue_all,%20or%20cancel_previous" in response.headers["location"]
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
    assert schedule is None


def test_next_cron_run_respects_timezone():
    next_run = next_cron_run("0 9 * * 1-5", datetime(2026, 7, 6, 23, 30, tzinfo=timezone.utc), "Asia/Shanghai")

    assert next_run == "2026-07-07T01:00:00+00:00"


def test_schedule_backfill_occurrences_respect_cron_timezone():
    schedule = {
        "schedule_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "interval_minutes": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    occurrences = schedule_backfill_occurrences(
        schedule,
        datetime(2026, 7, 6, 23, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 7, 1, 30, tzinfo=timezone.utc),
        5,
    )

    assert occurrences == ["2026-07-07T01:00:00+00:00"]


def test_cron_schedule_claim_creates_and_executes_run(client, sample_csv_bytes):
    source = upload_source(client, "cron sales", sample_csv_bytes)
    project = create_project(client, "cron scheduled", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "weekday refresh",
            "schedule_type": "cron",
            "cron_expression": "*/5 * * * *",
            "timezone_name": "UTC",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    due_at = now_iso()
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        assert schedule["schedule_type"] == "cron"
        assert schedule["cron_expression"] == "*/5 * * * *"
        assert schedule["timezone"] == "UTC"
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (due_at, schedule["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    execute_run(claimed[0]["run_id"])

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
        updated_schedule = conn.execute("SELECT * FROM schedules WHERE id = ?", (claimed[0]["schedule_id"],)).fetchone()
    assert run["trigger_type"] == "schedule"
    assert run["status"] == "succeeded"
    assert datetime.fromisoformat(updated_schedule["next_run_at"]) > datetime.fromisoformat(due_at)


def test_schedule_run_now_executes_manual_schedule_trigger(client, sample_csv_bytes):
    source = upload_source(client, "manual schedule sales", sample_csv_bytes)
    project = create_project(client, "manual scheduled", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "manual run", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()

    response = client.post(f"/schedules/{schedule['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/runs/")
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1", (project["id"],)).fetchone()
        updated_schedule = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
    assert run["trigger_type"] == "schedule_manual"
    assert run["schedule_id"] == schedule["id"]
    assert run["status"] == "succeeded"
    assert updated_schedule["last_run_at"] is not None


def test_due_schedule_updates_linked_report_snapshot(client, sample_csv_bytes):
    source = upload_source(client, "scheduled report sales", sample_csv_bytes)
    project = create_project(client, "scheduled report project", source["id"], "SELECT 'old' AS marker;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Scheduled report", "description": "Auto refresh"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_path = report_response.headers["location"]
    report_id = report_path.rsplit("/", 1)[-1]

    update_response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "scheduled report project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'scheduled' AS marker;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    schedule_response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "report refresh", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert schedule_response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    execute_run(claimed[0]["run_id"])

    with connect() as conn:
        scheduled_run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
        snapshot = conn.execute(
            "SELECT * FROM report_snapshots WHERE report_id = ? AND run_id = ?",
            (report_id, claimed[0]["run_id"]),
        ).fetchone()
        audit_event = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'report.snapshot_updated' AND resource_id = ?",
            (report_id,),
        ).fetchone()
        notification = conn.execute(
            """
            SELECT *
            FROM notifications
            WHERE event_type = 'report.refresh_succeeded' AND resource_id = ?
            """,
            (report_id,),
        ).fetchone()
    assert scheduled_run["status"] == "succeeded"
    assert scheduled_run["trigger_type"] == "schedule"
    assert snapshot["status"] == "succeeded"
    assert decode_json(snapshot["result_json"], {})["rows"] == [["scheduled"]]
    assert audit_event is not None
    assert notification["recipient_user_id"] == DEFAULT_USER_ID
    assert "scheduled" in client.get(report_path).text


def test_final_failed_schedule_records_failed_report_snapshot(client, sample_csv_bytes):
    source = upload_source(client, "failed scheduled report sales", sample_csv_bytes)
    project = create_project(client, "failed scheduled report project", source["id"], "SELECT 'old' AS marker;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Failed scheduled report", "description": "Auto refresh"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_path = report_response.headers["location"]
    report_id = report_path.rsplit("/", 1)[-1]

    update_response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "failed scheduled report project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT missing_column FROM data;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    schedule_response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "failed report refresh", "interval_minutes": 60, "max_retries": 0},
        follow_redirects=False,
    )
    assert schedule_response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    execute_run(claimed[0]["run_id"])

    with connect() as conn:
        snapshot = conn.execute(
            "SELECT * FROM report_snapshots WHERE report_id = ? AND run_id = ?",
            (report_id, claimed[0]["run_id"]),
        ).fetchone()
    assert snapshot["status"] == "failed"
    assert "missing_column" in snapshot["error"]
    report_html = client.get(report_path).text
    assert "old" in report_html
    assert "missing_column" in report_html


def test_invalid_cron_schedule_redirects_without_creating_schedule(client, sample_csv_bytes):
    source = upload_source(client, "invalid cron sales", sample_csv_bytes)
    project = create_project(client, "invalid cron scheduled", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "bad cron",
            "schedule_type": "cron",
            "cron_expression": "60 * * * *",
            "timezone_name": "UTC",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Cron field value must be between 0 and 59." in unquote(response.headers["location"])
    with connect() as conn:
        schedule_count = conn.execute("SELECT COUNT(*) AS count FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
    assert schedule_count["count"] == 0


def test_run_is_bound_to_immutable_project_version(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "versioned", source["id"], "SELECT 'old' AS marker;")
    queued_run_id = create_run(project["id"], "manual")

    response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "versioned",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'new' AS marker;",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    execute_run(queued_run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()
        versions = conn.execute(
            "SELECT * FROM project_versions WHERE project_id = ? ORDER BY version_number ASC",
            (project["id"],),
        ).fetchall()
    assert len(versions) == 2
    assert run["project_version_id"] == versions[0]["id"]
    result = decode_json(run["result_json"], {})
    assert result["rows"] == [["old"]]


def test_sql_project_parameters_are_bound_and_snapshotted(client, sample_csv_bytes):
    source = upload_source(client, "parameterized sales", sample_csv_bytes)
    original_parameters = '{"region": "East", "minimum_revenue": 100, "unused": "ignored"}'
    project = create_project(
        client,
        "parameterized SQL",
        source["id"],
        """-- $unused should not be bound from a SQL comment.
SELECT '$unused' AS literal, region, revenue
FROM data
WHERE region = $region AND revenue >= $minimum_revenue
ORDER BY revenue DESC;
""",
        parameters_json=original_parameters,
    )
    queued_run_id = create_run(project["id"], "manual")

    response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "parameterized SQL",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT $region AS selected_region;",
            "parameters_json": '{"region": "West"}',
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    execute_run(queued_run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()
        versions = conn.execute(
            "SELECT * FROM project_versions WHERE project_id = ? ORDER BY version_number ASC",
            (project["id"],),
        ).fetchall()
    assert run["status"] == "succeeded"
    assert decode_json(run["parameters_json"], {}) == {"region": "East", "minimum_revenue": 100, "unused": "ignored"}
    assert decode_json(versions[0]["parameters_json"], {}) == {"region": "East", "minimum_revenue": 100, "unused": "ignored"}
    assert decode_json(versions[1]["parameters_json"], {}) == {"region": "West"}
    result = decode_json(run["result_json"], {})
    assert result["rows"] == [["$unused", "East", 120]]

    response = client.get(f"/api/runs/{queued_run_id}")
    assert response.status_code == 200
    assert response.json()["parameters"] == {"region": "East", "minimum_revenue": 100, "unused": "ignored"}


def test_runtime_profile_is_versioned_and_runs_use_the_published_profile(client, monkeypatch, sample_csv_bytes):
    monkeypatch.setenv(
        "ANYDATAS_RUNTIME_PROFILES_JSON",
        json.dumps(
            {
                "science": {
                    "label": "Data Science",
                    "image": "registry.example.com/anydatas/science:2026-07",
                }
            }
        ),
    )
    source = upload_source(client, "runtime profile sales", sample_csv_bytes)
    response = client.post(
        "/projects",
        data={
            "name": "profiled analysis",
            "language": "python",
            "runtime_profile": "science",
            "data_source_id": source["id"],
            "script": "result = load_data()",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'profiled analysis'").fetchone()
        version_one = conn.execute(
            "SELECT * FROM project_versions WHERE project_id = ? AND version_number = 1",
            (project["id"],),
        ).fetchone()
    assert project["runtime_profile"] == "science"
    assert version_one["runtime_profile"] == "science"

    assert client.post(
        f"/projects/{project['id']}",
        data={
            "name": "profiled analysis",
            "language": "python",
            "runtime_profile": "standard",
            "data_source_id": source["id"],
            "script": "result = load_data()[:1]",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
        version_two = conn.execute(
            "SELECT * FROM project_versions WHERE project_id = ? AND version_number = 2",
            (project["id"],),
        ).fetchone()
    assert updated_project["runtime_profile"] == "standard"
    assert version_two["runtime_profile"] == "standard"
    assert updated_project["published_version_id"] == version_one["id"]

    captured_profiles = []

    class FakeRunner:
        def run(self, runnable, _source, _run_id, _parameters, _secret_values):
            captured_profiles.append(runnable["runtime_profile"])
            return {"columns": [], "rows": []}, ""

    monkeypatch.setattr(runner_module, "get_runner", lambda: FakeRunner())
    first_run_id = create_run(project["id"], "manual")
    execute_run(first_run_id)
    assert captured_profiles == ["science"]

    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    second_run_id = create_run(project["id"], "manual")
    execute_run(second_run_id)
    assert captured_profiles == ["science", "standard"]
    with connect() as conn:
        first_audit = conn.execute(
            "SELECT detail_json FROM audit_events WHERE action = 'run.queued' AND resource_id = ?",
            (first_run_id,),
        ).fetchone()
        second_audit = conn.execute(
            "SELECT detail_json FROM audit_events WHERE action = 'run.queued' AND resource_id = ?",
            (second_run_id,),
        ).fetchone()
    assert decode_json(first_audit["detail_json"], {})["runtime_profile"] == "science"
    assert decode_json(second_audit["detail_json"], {})["runtime_profile"] == "standard"


def test_project_rejects_unconfigured_runtime_profiles(client, sample_csv_bytes):
    source = upload_source(client, "invalid runtime profile sales", sample_csv_bytes)
    response = client.post(
        "/projects",
        data={
            "name": "untrusted runtime",
            "language": "python",
            "runtime_profile": "attacker-image",
            "data_source_id": source["id"],
            "script": "result = []",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Select%20an%20available%20runtime%20profile" in response.headers["location"]
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects WHERE name = 'untrusted runtime'").fetchone()[0] == 0


def test_python_project_parameters_are_available_as_params(client, sample_csv_bytes):
    source = upload_source(client, "python parameters sales", sample_csv_bytes)
    project = create_project(
        client,
        "parameterized Python",
        source["id"],
        'result = {"region": params["region"], "limit": params["limit"]}',
        language="python",
        parameters_json='{"region": "West", "limit": 2}',
    )

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
    assert run["status"] == "succeeded"
    assert decode_json(run["parameters_json"], {}) == {"region": "West", "limit": 2}
    assert decode_json(run["result_json"], {})["rows"] == [["West", 2]]


def test_secret_references_are_snapshotted_in_runs_and_redacted(client, sample_csv_bytes, monkeypatch):
    secret_value = "super-secret-token"
    monkeypatch.setenv("ANYDATAS_SECRET_WAREHOUSE_PASSWORD", secret_value)
    monkeypatch.setenv("ANYDATAS_SECRET_UNBOUND", "must-not-reach-runner")
    monkeypatch.setenv("ANYDATAS_SMTP_PASSWORD", "smtp-password-must-not-reach-runner")
    monkeypatch.setenv("ANYDATAS_METRICS_TOKEN", "metrics-token-must-not-reach-runner")
    source = upload_source(client, "secret sales", sample_csv_bytes)
    project = create_project(
        client,
        "secret project",
        source["id"],
        """import os
secret = os.environ["ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD"]
assert os.environ.get("ANYDATAS_SECRET_UNBOUND") is None
assert os.environ.get("ANYDATAS_SMTP_PASSWORD") is None
assert os.environ.get("ANYDATAS_METRICS_TOKEN") is None
print(secret)
result = [{"configured": True, "secret": secret}]
""",
        language="python",
    )
    reference_response = client.post(
        "/secrets",
        data={
            "name": "warehouse-password",
            "environment_variable": "ANYDATAS_SECRET_WAREHOUSE_PASSWORD",
            "description": "Read-only password",
        },
        follow_redirects=False,
    )
    assert reference_response.status_code == 303
    with connect() as conn:
        reference = conn.execute("SELECT * FROM secret_references WHERE name = 'warehouse-password'").fetchone()

    bind_response = client.post(
        f"/projects/{project['id']}/secrets",
        data={
            "secret_id": reference["id"],
            "environment_name": "ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD",
        },
        follow_redirects=False,
    )
    assert bind_response.status_code == 303
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    queued_run_id = create_run(project["id"], "manual")

    unbind_response = client.post(
        f"/projects/{project['id']}/secrets/{reference['id']}/delete",
        follow_redirects=False,
    )
    assert unbind_response.status_code == 303
    execute_run(queued_run_id)

    with connect() as conn:
        queued_run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()
        versions = conn.execute(
            "SELECT * FROM project_versions WHERE project_id = ? ORDER BY version_number",
            (project["id"],),
        ).fetchall()
        bindings = conn.execute(
            "SELECT * FROM project_secret_bindings WHERE project_id = ?",
            (project["id"],),
        ).fetchall()
        resolved_audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'run.secrets_resolved' AND resource_id = ?",
            (queued_run_id,),
        ).fetchone()

    assert queued_run["status"] == "succeeded"
    assert secret_value not in queued_run["logs"]
    assert REDACTED_VALUE in queued_run["logs"]
    assert secret_value not in queued_run["result_json"]
    assert REDACTED_VALUE in queued_run["result_json"]
    assert decode_json(queued_run["secret_bindings_json"], []) == [
        {"secret_id": reference["id"], "environment_name": "ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD"}
    ]
    assert decode_json(versions[1]["secret_bindings_json"], []) == decode_json(queued_run["secret_bindings_json"], [])
    assert decode_json(versions[-1]["secret_bindings_json"], []) == []
    assert bindings == []
    assert resolved_audit is not None
    assert secret_value not in resolved_audit["detail_json"]


def test_docker_runner_passes_only_bound_secret_values(monkeypatch, tmp_path):
    source_path = tmp_path / "sales.csv"
    source_path.write_text("region,revenue\nEast,120\n", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "read_runner_result", lambda _proc, _result_path: ({"columns": [], "rows": []}, ""))

    DockerRunner().run(
        {"language": "sql", "script": "SELECT 1;"},
        {"source_type": "file", "path": str(source_path), "connection_json": "{}"},
        "docker-secret-test",
        {},
        {"ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD": "docker-secret"},
    )

    assert "ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD=docker-secret" in captured["command"]


def test_secret_reference_deletion_preserves_active_and_published_snapshots(client, sample_csv_bytes):
    source = upload_source(client, "secret deletion sales", sample_csv_bytes)
    project = create_project(client, "secret deletion project", source["id"], "SELECT * FROM data LIMIT 1;")
    reference_response = client.post(
        "/secrets",
        data={
            "name": "deletion-lifecycle",
            "environment_variable": "ANYDATAS_SECRET_DELETION_LIFECYCLE_TEST",
            "description": "Protect published snapshots",
        },
        follow_redirects=False,
    )
    assert reference_response.status_code == 303
    with connect() as conn:
        reference = conn.execute("SELECT * FROM secret_references WHERE name = 'deletion-lifecycle'").fetchone()

    assert client.post(
        f"/projects/{project['id']}/secrets",
        data={
            "secret_id": reference["id"],
            "environment_name": "ANYDATAS_USER_SECRET_DELETION_LIFECYCLE_TEST",
        },
        follow_redirects=False,
    ).status_code == 303
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    bound_delete = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert bound_delete.status_code == 303
    assert "Unbind%20this%20secret%20reference" in bound_delete.headers["location"]

    queued_run_id = create_run(project["id"], "manual")
    assert client.post(
        f"/projects/{project['id']}/secrets/{reference['id']}/delete",
        follow_redirects=False,
    ).status_code == 303
    pending_delete = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert pending_delete.status_code == 303
    assert "Wait%20for%20or%20cancel%20active%20runs" in pending_delete.headers["location"]

    execute_run(queued_run_id)
    published_delete = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert published_delete.status_code == 303
    assert "Publish%20a%20newer%20project%20version" in published_delete.headers["location"]
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    deleted_reference = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)
    assert deleted_reference.status_code == 303
    with connect() as conn:
        surviving_reference = conn.execute("SELECT * FROM secret_references WHERE id = ?", (reference["id"],)).fetchone()
    assert surviving_reference is None


def test_secret_reference_requires_runtime_value(client, sample_csv_bytes, monkeypatch):
    monkeypatch.delenv("ANYDATAS_SECRET_MISSING_RUNTIME_VALUE", raising=False)
    source = upload_source(client, "missing secret sales", sample_csv_bytes)
    project = create_project(client, "missing secret project", source["id"], "SELECT * FROM data LIMIT 1;")
    reference_response = client.post(
        "/secrets",
        data={
            "name": "missing-runtime-value",
            "environment_variable": "ANYDATAS_SECRET_MISSING_RUNTIME_VALUE",
            "description": "Must be configured by deployment",
        },
        follow_redirects=False,
    )
    assert reference_response.status_code == 303
    with connect() as conn:
        reference = conn.execute("SELECT * FROM secret_references WHERE name = 'missing-runtime-value'").fetchone()

    assert client.post(
        f"/projects/{project['id']}/secrets",
        data={
            "secret_id": reference["id"],
            "environment_name": "ANYDATAS_USER_SECRET_MISSING_RUNTIME_VALUE",
        },
        follow_redirects=False,
    ).status_code == 303
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    queued_run_id = create_run(project["id"], "manual")
    execute_run(queued_run_id)

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (queued_run_id,)).fetchone()

    assert run["status"] == "failed"
    assert "missing-runtime-value" in run["error"]
    assert "ANYDATAS_SECRET_MISSING_RUNTIME_VALUE" not in run["error"]


def test_docker_runner_passes_parameters_to_runtime(monkeypatch, tmp_path):
    source_path = UPLOAD_DIR / "sales.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("region,revenue\nEast,120\n", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def fake_run(command, **_kwargs):
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, "/srv/anydatas/var\n", "")
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module, "is_dockerized", lambda: True)
    monkeypatch.delenv("ANYDATAS_DOCKER_HOST_DATA_DIR", raising=False)
    monkeypatch.setenv("HOSTNAME", "anydatas-app")
    monkeypatch.setattr(
        runner_module,
        "read_runner_result",
        lambda _proc, _result_path: ({"columns": ["region"], "rows": [["East"]]}, ""),
    )

    result, logs = DockerRunner().run(
        {"language": "sql", "script": "SELECT $region AS region;"},
        {"source_type": "file", "path": str(source_path), "connection_json": "{}"},
        "docker-parameter-test",
        {"region": "East"},
    )

    assert result["rows"] == [["East"]]
    assert logs == ""
    assert "ANYDATAS_PARAMETERS_JSON={\"region\": \"East\"}" in captured["command"]
    assert "--read-only" in captured["command"]
    assert "--user" in captured["command"]
    assert "65532:65532" in captured["command"]
    assert "--tmpfs" in captured["command"]
    assert "/tmp:rw,noexec,nosuid,size=64m" in captured["command"]
    assert "--network" in captured["command"]
    assert "none" in captured["command"]
    assert "--name" in captured["command"]
    assert "anydatas-run-docker-parameter-test" in captured["command"]
    assert "type=bind,src=/srv/anydatas/var/runs/docker-parameter-test,dst=/work" in captured["command"]
    assert "type=bind,src=/srv/anydatas/var/uploads,dst=/data,readonly" in captured["command"]


def test_docker_runner_removes_container_after_timeout(monkeypatch, tmp_path):
    source_path = tmp_path / "sales.csv"
    source_path.write_text("region,revenue\nEast,120\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 45)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))

    with pytest.raises(subprocess.TimeoutExpired):
        DockerRunner().run(
            {"language": "sql", "script": "SELECT 1;"},
            {"source_type": "file", "path": str(source_path), "connection_json": "{}"},
            "timeout-test",
            {},
        )

    assert ["/usr/bin/docker", "rm", "--force", "anydatas-run-timeout-test"] in commands


def test_docker_runner_cancels_the_named_container(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    assert DockerRunner().cancel("cancel test") is True
    assert commands == [["/usr/bin/docker", "rm", "--force", "anydatas-run-cancel-test"]]


def test_docker_runner_falls_back_when_mount_inspection_times_out(monkeypatch):
    def timeout_inspect(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.delenv("ANYDATAS_DOCKER_HOST_DATA_DIR", raising=False)
    monkeypatch.setenv("HOSTNAME", "anydatas-app")
    monkeypatch.setattr(runner_module, "is_dockerized", lambda: True)
    monkeypatch.setattr(runner_module.subprocess, "run", timeout_inspect)

    assert DockerRunner.host_data_dir("/usr/bin/docker") is None


def test_runner_rejects_symlinked_result_file(tmp_path):
    target = tmp_path / "other-result.json"
    target.write_text('{"columns": ["value"], "rows": [[1]]}', encoding="utf-8")
    result_path = tmp_path / "result.json"
    result_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="regular file"):
        runner_module.read_runner_result(subprocess.CompletedProcess(["runner"], 0, "", ""), result_path)


def test_project_rejects_invalid_parameter_objects(client, sample_csv_bytes):
    source = upload_source(client, "invalid parameters sales", sample_csv_bytes)

    response = client.post(
        "/projects",
        data={
            "name": "bad parameters",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 1;",
            "parameters_json": "[]",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Parameters must be a JSON object." in unquote(response.headers["location"])
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'bad parameters'").fetchone()
    assert project is None

    response = client.post(
        "/projects",
        data={
            "name": "bad parameter name",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 1;",
            "parameters_json": '{"bad-name": 1}',
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Parameter names must start with a letter or underscore" in unquote(response.headers["location"])

    response = client.post(
        "/projects",
        data={
            "name": "non-standard parameter value",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 1;",
            "parameters_json": '{"limit": NaN}',
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Parameters must be a valid JSON object." in unquote(response.headers["location"])


def test_runs_use_published_version_until_latest_is_published(client, sample_csv_bytes):
    source = upload_source(client, "publish sales", sample_csv_bytes)
    project = create_project(client, "publish flow", source["id"], "SELECT 'published' AS marker;")

    response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "publish flow",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'draft' AS marker;",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert response.status_code == 303
    with connect() as conn:
        first_run = conn.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1", (project["id"],)).fetchone()
        versions = conn.execute("SELECT * FROM project_versions WHERE project_id = ? ORDER BY version_number ASC", (project["id"],)).fetchall()
    assert first_run["project_version_id"] == versions[0]["id"]
    first_result = decode_json(first_run["result_json"], {})
    assert first_result["rows"] == [["published"]]

    response = client.post(f"/projects/{project['id']}/publish", follow_redirects=False)
    assert response.status_code == 303
    assert "Published%20project%20version%20v2" in response.headers["location"]

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert response.status_code == 303
    with connect() as conn:
        second_run = conn.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1", (project["id"],)).fetchone()
        project_after_publish = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
    assert project_after_publish["published_version_id"] == versions[1]["id"]
    assert second_run["project_version_id"] == versions[1]["id"]
    second_result = decode_json(second_run["result_json"], {})
    assert second_result["rows"] == [["draft"]]


def test_scheduled_run_uses_published_version_parameters(client, sample_csv_bytes):
    source = upload_source(client, "scheduled parameter sales", sample_csv_bytes)
    project = create_project(
        client,
        "scheduled parameters",
        source["id"],
        "SELECT $region AS region;",
        parameters_json='{"region": "East"}',
    )
    response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "scheduled parameters",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT $region AS region;",
            "parameters_json": '{"region": "West"}',
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "parameter schedule", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        versions = conn.execute("SELECT * FROM project_versions WHERE project_id = ? ORDER BY version_number ASC", (project["id"],)).fetchall()
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (now_iso(), schedule["id"]))

    claimed = claim_due_schedules()
    assert len(claimed) == 1
    execute_run(claimed[0]["run_id"])

    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
    assert run["project_version_id"] == versions[0]["id"]
    assert decode_json(run["parameters_json"], {}) == {"region": "East"}
    assert decode_json(run["result_json"], {})["rows"] == [["East"]]


def test_upload_rejects_non_csv(client):
    response = client.post(
        "/data-sources",
        data={"name": "bad"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "MVP%20currently%20accepts%20CSV,%20XLSX,%20or%20Parquet%20files" in response.headers["location"]


def test_upload_rejects_files_over_size_limit(client, sample_csv_bytes, monkeypatch):
    monkeypatch.setenv("ANYDATAS_MAX_UPLOAD_BYTES", "12")

    response = client.post(
        "/data-sources",
        data={"name": "too large"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "File exceeds upload limit" in unquote(response.headers["location"])
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'too large'").fetchone()
    assert source is None
    assert list(UPLOAD_DIR.iterdir()) == []


def test_sqlite_connection_rejects_missing_table(client, tmp_path):
    database_path = tmp_path / "warehouse.sqlite3"
    create_sample_sqlite(database_path)

    response = client.post(
        "/data-sources/sqlite",
        data={"name": "bad warehouse", "database_path": str(database_path), "table_name": "missing_sales"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "SQLite%20connection%20failed" in response.headers["location"]
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'bad warehouse'").fetchone()
    assert source is None


def test_audit_events_are_recorded_for_core_actions(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "audited", source["id"], "SELECT * FROM data LIMIT 1;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Audit report", "description": "Latest"},
        follow_redirects=False,
    )

    response = client.get("/api/audit-events")

    assert response.status_code == 200
    actions = {event["action"] for event in response.json()}
    assert "data_source.created" in actions
    assert "project.created" in actions
    assert "run.queued" in actions
    assert "run.succeeded" in actions
    assert "report.created" in actions


def test_report_creation_rejects_unknown_visibility(client, sample_csv_bytes):
    source = upload_source(client, "invalid visibility sales", sample_csv_bytes)
    project = create_project(client, "invalid visibility project", source["id"], "SELECT * FROM data LIMIT 1;")

    response = client.post(
        "/reports",
        data={
            "project_id": project["id"],
            "title": "Invalid visibility report",
            "description": "Should not be created",
            "visibility": "organization",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Report%20visibility%20must%20be%20workspace%20or%20private" in response.headers["location"]
    with connect() as conn:
        report = conn.execute("SELECT * FROM reports WHERE title = 'Invalid visibility report'").fetchone()
    assert report is None


def test_init_db_migrates_legacy_schedule_constraint_without_retargeting_runs(tmp_path, monkeypatch):
    legacy_db_path = tmp_path / "legacy-schedule.sqlite3"
    with sqlite3.connect(legacy_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'interval',
                interval_minutes INTEGER NOT NULL,
                cron_expression TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'UTC',
                max_retries INTEGER NOT NULL DEFAULT 0,
                retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
                concurrency_policy TEXT NOT NULL DEFAULT 'skip' CHECK(concurrency_policy IN ('skip', 'queue_one')),
                is_active INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                next_run_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL)")
        conn.execute(
            """
            INSERT INTO schedules (id, project_id, name, interval_minutes, next_run_at, created_at)
            VALUES ('legacy-schedule', 'legacy-project', 'Legacy schedule', 60, '2026-01-01T01:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute("INSERT INTO runs (id, schedule_id) VALUES ('legacy-run', 'legacy-schedule')")

    monkeypatch.setattr(db_module, "DB_PATH", legacy_db_path)
    monkeypatch.setattr(db_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db_module, "RUN_DIR", tmp_path / "runs")
    db_module.init_db()

    with db_module.connect() as conn:
        conn.execute("UPDATE schedules SET concurrency_policy = 'cancel_previous' WHERE id = 'legacy-schedule'")
        cancel_previous_schedule = conn.execute("SELECT * FROM schedules WHERE id = 'legacy-schedule'").fetchone()
        conn.execute("UPDATE schedules SET concurrency_policy = 'queue_all' WHERE id = 'legacy-schedule'")
        schedule = conn.execute("SELECT * FROM schedules WHERE id = 'legacy-schedule'").fetchone()
        run = conn.execute("SELECT * FROM runs WHERE id = 'legacy-run'").fetchone()
        run_foreign_keys = conn.execute("PRAGMA foreign_key_list(runs)").fetchall()
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}

    assert cancel_previous_schedule["concurrency_policy"] == "cancel_previous"
    assert schedule["concurrency_policy"] == "queue_all"
    assert run["schedule_id"] == "legacy-schedule"
    assert any(foreign_key["from"] == "schedule_id" and foreign_key["table"] == "schedules" for foreign_key in run_foreign_keys)
    assert "scheduled_for_at" in run_columns


def test_init_db_migrates_legacy_report_widget_constraint_for_pie_and_scatter_charts(tmp_path, monkeypatch):
    legacy_db_path = tmp_path / "legacy-report-widgets.sqlite3"
    with sqlite3.connect(legacy_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE report_widgets (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT 'demo-workspace',
                created_by_user_id TEXT,
                kind TEXT NOT NULL CHECK(kind IN ('metric', 'table', 'bar', 'line', 'markdown')),
                title TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO report_widgets (id, report_id, kind, title, config_json, position, created_at)
            VALUES ('legacy-widget', 'legacy-report', 'bar', 'Legacy chart', '{}', 0, '2026-01-01T00:00:00+00:00')
            """
        )

    monkeypatch.setattr(db_module, "DB_PATH", legacy_db_path)
    monkeypatch.setattr(db_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db_module, "RUN_DIR", tmp_path / "runs")
    db_module.init_db()

    with db_module.connect() as conn:
        conn.execute("UPDATE report_widgets SET kind = 'pie' WHERE id = 'legacy-widget'")
        conn.execute("UPDATE report_widgets SET kind = 'scatter' WHERE id = 'legacy-widget'")
        widget = conn.execute("SELECT * FROM report_widgets WHERE id = 'legacy-widget'").fetchone()
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}

    assert widget["kind"] == "scatter"
    assert "report_filters" in tables
    assert "service_accounts" in tables
    assert "password_reset_tokens" in tables


def test_init_db_migrates_legacy_reports_with_visibility_columns(tmp_path, monkeypatch):
    legacy_db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE reports (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO reports (id, project_id, title, description, created_at, updated_at)
            VALUES ('legacy-report', 'legacy-project', 'Legacy report', '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )

    monkeypatch.setattr(db_module, "DB_PATH", legacy_db_path)
    monkeypatch.setattr(db_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db_module, "RUN_DIR", tmp_path / "runs")
    db_module.init_db()

    with db_module.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
        notification_columns = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        schedule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
        project_version_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_versions)").fetchall()}
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        report = conn.execute("SELECT * FROM reports WHERE id = 'legacy-report'").fetchone()
        widgets = conn.execute(
            "SELECT kind, title FROM report_widgets WHERE report_id = ? ORDER BY position",
            ("legacy-report",),
        ).fetchall()

    assert {"created_by_user_id", "visibility", "widgets_initialized"}.issubset(columns)
    assert "concurrency_policy" in schedule_columns
    assert "report_access_grants" in tables
    assert "report_subscriptions" in tables
    assert "report_subscription_channels" in tables
    assert "report_widgets" in tables
    assert "report_filters" in tables
    assert "secret_references" in tables
    assert "project_secret_bindings" in tables
    assert "notification_channels" in tables
    assert "notification_deliveries" in tables
    assert "recipient_user_id" in notification_columns
    assert "secret_bindings_json" in project_version_columns
    assert "secret_bindings_json" in run_columns
    assert report["created_by_user_id"] is None
    assert report["visibility"] == "workspace"
    assert report["workspace_id"] == db_module.DEFAULT_WORKSPACE_ID
    assert report["widgets_initialized"] == 1
    assert [(widget["kind"], widget["title"]) for widget in widgets] == [
        ("metric", "Rows"),
        ("metric", "Columns"),
        ("bar", "Comparison"),
        ("table", "Result Table"),
    ]


def test_init_db_migrates_legacy_api_tokens_to_full_scope(tmp_path, monkeypatch):
    legacy_db_path = tmp_path / "legacy-token.sqlite3"
    with sqlite3.connect(legacy_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE api_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO api_tokens (
                id, token_hash, user_id, workspace_id, name, created_at, expires_at
            )
            VALUES ('legacy-token', 'legacy-hash', 'legacy-user', 'legacy-workspace', 'Legacy', '2026-01-01', '2027-01-01')
            """
        )

    monkeypatch.setattr(db_module, "DB_PATH", legacy_db_path)
    monkeypatch.setattr(db_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db_module, "RUN_DIR", tmp_path / "runs")
    db_module.init_db()

    with db_module.connect() as conn:
        token = conn.execute("SELECT * FROM api_tokens WHERE id = 'legacy-token'").fetchone()

    assert token["scope"] == "full"


def test_init_db_backfills_report_creator_subscriptions(tmp_path, monkeypatch):
    legacy_db_path = tmp_path / "legacy-report-subscription.sqlite3"
    with sqlite3.connect(legacy_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE reports (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                created_by_user_id TEXT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO reports (
                id, workspace_id, project_id, created_by_user_id, title, description, created_at, updated_at
            )
            VALUES (
                'legacy-subscribed-report', 'demo-workspace', 'legacy-project', 'demo-user',
                'Legacy subscribed report', '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            )
            """
        )

    monkeypatch.setattr(db_module, "DB_PATH", legacy_db_path)
    monkeypatch.setattr(db_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db_module, "RUN_DIR", tmp_path / "runs")
    db_module.init_db()

    with db_module.connect() as conn:
        subscription = conn.execute(
            "SELECT * FROM report_subscriptions WHERE report_id = 'legacy-subscribed-report'"
        ).fetchone()
    assert subscription["user_id"] == db_module.DEFAULT_USER_ID


def test_init_db_backfills_legacy_data_source_schema_metadata(tmp_path, monkeypatch):
    legacy_db_path = tmp_path / "legacy-source.sqlite3"
    with sqlite3.connect(legacy_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE data_sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                row_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO data_sources (id, name, filename, path, columns_json, preview_json, row_count, created_at)
            VALUES ('legacy-source', 'Legacy source', 'legacy.csv', '/tmp/legacy.csv', ?, ?, 1, '2026-01-01T00:00:00+00:00')
            """,
            (
                '["day", "revenue", "active"]',
                '[{"day": "2026-01-01", "revenue": "120", "active": "true"}]',
            ),
        )

    monkeypatch.setattr(db_module, "DB_PATH", legacy_db_path)
    monkeypatch.setattr(db_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(db_module, "RUN_DIR", tmp_path / "runs")
    db_module.init_db()

    with db_module.connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE id = 'legacy-source'").fetchone()
    metadata = db_module.decode_json(source["column_metadata_json"], {})
    assert source["classification"] == "internal"
    assert metadata == {
        "day": {"type": "date", "description": "", "classification": "none", "masking": "none"},
        "revenue": {"type": "integer", "description": "", "classification": "none", "masking": "none"},
        "active": {"type": "boolean", "description": "", "classification": "none", "masking": "none"},
    }


def test_report_uses_snapshot_until_refreshed(client, sample_csv_bytes):
    source = upload_source(client, "snapshot sales", sample_csv_bytes)
    project = create_project(client, "snapshot project", source["id"], "SELECT 'old' AS marker;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Snapshot report", "description": "Pinned"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_path = response.headers["location"]
    with connect() as conn:
        report = conn.execute("SELECT * FROM reports WHERE title = 'Snapshot report'").fetchone()
        snapshots = conn.execute("SELECT * FROM report_snapshots WHERE report_id = ?", (report["id"],)).fetchall()
    assert len(snapshots) == 1
    assert snapshots[0]["status"] == "succeeded"

    client.post(
        f"/projects/{project['id']}",
        data={
            "name": "snapshot project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'new' AS marker;",
        },
        follow_redirects=False,
    )
    client.post(f"/projects/{project['id']}/publish", follow_redirects=False)
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    stale_response = client.get(report_path)
    assert stale_response.status_code == 200
    assert "<td>old</td>" in stale_response.text
    assert "<td>new</td>" not in stale_response.text

    refresh_response = client.post(f"/reports/{report['id']}/refresh", follow_redirects=False)
    assert refresh_response.status_code == 303
    refreshed_response = client.get(report_path)
    assert refreshed_response.status_code == 200
    assert "<td>new</td>" in refreshed_response.text
    with connect() as conn:
        snapshots = conn.execute("SELECT * FROM report_snapshots WHERE report_id = ? ORDER BY created_at ASC", (report["id"],)).fetchall()
    assert len(snapshots) == 2
    assert snapshots[-1]["status"] == "succeeded"


def test_report_subscribers_receive_targeted_refresh_notifications(client, sample_csv_bytes):
    source = upload_source(client, "subscriber sales", sample_csv_bytes)
    project = create_project(client, "subscriber project", source["id"], "SELECT 'initial' AS marker;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Subscriber report", "description": "Targeted updates"},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    for channel_name, destination in (
        ("subscriber selected email", "selected@example.com"),
        ("subscriber unselected email", "unselected@example.com"),
    ):
        assert client.post(
            "/notification-channels",
            data={
                "name": channel_name,
                "channel_type": "email",
                "destination": destination,
                "event_types": ["report.refresh_succeeded", "report.refresh_failed"],
                "max_retries": 1,
            },
            follow_redirects=False,
        ).status_code == 303
    with connect() as conn:
        selected_channel = conn.execute(
            "SELECT * FROM notification_channels WHERE name = 'subscriber selected email'"
        ).fetchone()

    member_response = client.post(
        "/workspace/members",
        data={"email": "subscriber@example.com", "name": "Subscriber", "role": "viewer"},
        follow_redirects=False,
    )
    assert member_response.status_code == 303
    with connect() as conn:
        subscriber = conn.execute("SELECT * FROM users WHERE email = 'subscriber@example.com'").fetchone()

    client.cookies.set("anydatas_user_id", subscriber["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    subscribe_response = client.post(
        f"/reports/{report_id}/subscriptions",
        data={"external_channel_ids": selected_channel["id"]},
        follow_redirects=False,
    )
    assert subscribe_response.status_code == 303

    client.cookies.set("anydatas_user_id", DEFAULT_USER_ID)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    client.post(
        f"/projects/{project['id']}",
        data={
            "name": "subscriber project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'updated' AS marker;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    assert client.post(f"/reports/{report_id}/refresh", follow_redirects=False).status_code == 303

    with connect() as conn:
        succeeded_notifications = conn.execute(
            """
            SELECT *
            FROM notifications
            WHERE event_type = 'report.refresh_succeeded' AND resource_id = ?
            ORDER BY recipient_user_id
            """,
            (report_id,),
        ).fetchall()
    assert {notification["recipient_user_id"] for notification in succeeded_notifications} == {DEFAULT_USER_ID, subscriber["id"]}
    owner_notification = next(
        notification for notification in succeeded_notifications if notification["recipient_user_id"] == DEFAULT_USER_ID
    )
    subscriber_notification = next(
        notification for notification in succeeded_notifications if notification["recipient_user_id"] == subscriber["id"]
    )
    with connect() as conn:
        subscriber_deliveries = conn.execute(
            """
            SELECT delivery.channel_id
            FROM notification_deliveries delivery
            WHERE delivery.notification_id = ?
            """,
            (subscriber_notification["id"],),
        ).fetchall()
        owner_delivery_count = conn.execute(
            "SELECT COUNT(*) AS count FROM notification_deliveries WHERE notification_id = ?",
            (owner_notification["id"],),
        ).fetchone()["count"]
    assert [delivery["channel_id"] for delivery in subscriber_deliveries] == [selected_channel["id"]]
    assert owner_delivery_count == 0

    client.cookies.set("anydatas_user_id", subscriber["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    assert all(notification["recipient_user_id"] != DEFAULT_USER_ID for notification in client.get("/api/notifications").json())
    assert client.post(f"/notifications/{owner_notification['id']}/read", follow_redirects=False).status_code == 404
    assert client.post(f"/notifications/{subscriber_notification['id']}/read", follow_redirects=False).status_code == 303
    assert client.post(f"/reports/{report_id}/subscriptions/delete", follow_redirects=False).status_code == 303

    client.cookies.set("anydatas_user_id", DEFAULT_USER_ID)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    client.post(
        f"/projects/{project['id']}",
        data={
            "name": "subscriber project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT missing_column FROM data;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    assert client.post(f"/reports/{report_id}/refresh", follow_redirects=False).status_code == 303

    with connect() as conn:
        failed_notifications = conn.execute(
            "SELECT * FROM notifications WHERE event_type = 'report.refresh_failed' AND resource_id = ?",
            (report_id,),
        ).fetchall()
        subscription_actions = conn.execute(
            """
            SELECT action
            FROM audit_events
            WHERE resource_type = 'report' AND resource_id = ?
            """,
            (report_id,),
        ).fetchall()
    assert [notification["recipient_user_id"] for notification in failed_notifications] == [DEFAULT_USER_ID]
    assert {action["action"] for action in subscription_actions}.issuperset({"report.subscribed", "report.unsubscribed"})


def test_failed_report_refresh_keeps_previous_successful_snapshot(client, sample_csv_bytes):
    source = upload_source(client, "failed refresh sales", sample_csv_bytes)
    project = create_project(client, "failed refresh project", source["id"], "SELECT 'stable' AS marker;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Failed refresh report", "description": "Pinned"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_path = response.headers["location"]
    with connect() as conn:
        report = conn.execute("SELECT * FROM reports WHERE title = 'Failed refresh report'").fetchone()

    client.post(
        f"/projects/{project['id']}",
        data={
            "name": "failed refresh project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT missing_column FROM data;",
        },
        follow_redirects=False,
    )
    client.post(f"/projects/{project['id']}/publish", follow_redirects=False)

    refresh_response = client.post(f"/reports/{report['id']}/refresh", follow_redirects=False)
    assert refresh_response.status_code == 303
    response = client.get(report_path)
    assert response.status_code == 200
    assert "stable" in response.text
    assert "Refresh Status" in response.text
    assert "failed" in response.text
    assert "missing_column" in response.text or "Binder Error" in response.text
    with connect() as conn:
        latest_snapshot = conn.execute(
            "SELECT * FROM report_snapshots WHERE report_id = ? ORDER BY created_at DESC LIMIT 1",
            (report["id"],),
        ).fetchone()
        notification = conn.execute(
            "SELECT * FROM notifications WHERE resource_type = 'report' AND resource_id = ?",
            (report["id"],),
        ).fetchone()
    assert latest_snapshot["status"] == "failed"
    assert notification["event_type"] == "report.refresh_failed"
    assert notification["severity"] == "error"
