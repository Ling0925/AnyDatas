from __future__ import annotations

import os
import sys
import tempfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SMOKE_DATA_DIR = tempfile.TemporaryDirectory(prefix="anydatas-smoke-")
os.environ["ANYDATAS_DATA_DIR"] = SMOKE_DATA_DIR.name
os.environ["ANYDATAS_DISABLE_SCHEDULER"] = "1"

from app.db import connect, decode_json
from app.main import app, claim_due_schedules
from app.runner import execute_run, now_iso


def build_sample_parquet_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "sales.parquet"
        safe_path = str(path).replace("'", "''")
        duckdb.sql(
            f"""
            COPY (
                SELECT '2026-07-01' AS date, 120 AS revenue, 'East' AS region
                UNION ALL
                SELECT '2026-07-02' AS date, 180 AS revenue, 'West' AS region
                UNION ALL
                SELECT '2026-07-03' AS date, 90 AS revenue, 'East' AS region
            )
            TO '{safe_path}' (FORMAT PARQUET)
            """
        )
        return path.read_bytes()


def xlsx_cell(reference: str, value, cell_type: str = "inlineStr") -> str:
    if cell_type == "n":
        return f'<c r="{reference}"><v>{value}</v></c>'
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def build_sample_xlsx_bytes() -> bytes:
    rows = [
        [xlsx_cell("A1", "date"), xlsx_cell("B1", "revenue"), xlsx_cell("C1", "region")],
        [xlsx_cell("A2", "2026-07-01"), xlsx_cell("B2", 120, "n"), xlsx_cell("C2", "East")],
        [xlsx_cell("A3", "2026-07-02"), xlsx_cell("B3", 180, "n"), xlsx_cell("C3", "West")],
        [xlsx_cell("A4", "2026-07-03"), xlsx_cell("B4", 90, "n"), xlsx_cell("C4", "East")],
    ]
    sheet_rows = "\n".join(f'<row r="{index + 1}">{"".join(row)}</row>' for index, row in enumerate(rows))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as workbook:
        workbook.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>')
        workbook.writestr(
            "xl/workbook.xml",
            """
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets>
            </workbook>
            """,
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            """
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
            </Relationships>
            """,
        )
        workbook.writestr(
            "xl/worksheets/sheet1.xml",
            f"""
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>{sheet_rows}</sheetData>
            </worksheet>
            """,
        )
    return buffer.getvalue()


def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    source_name = f"smoke_sales_{suffix}"
    parquet_source_name = f"smoke_parquet_sales_{suffix}"
    xlsx_source_name = f"smoke_xlsx_sales_{suffix}"
    project_name = f"smoke_sql_{suffix}"
    parameter_project_name = f"smoke_parameter_sql_{suffix}"
    parquet_project_name = f"smoke_parquet_sql_{suffix}"
    xlsx_project_name = f"smoke_xlsx_sql_{suffix}"
    failed_project_name = f"smoke_failed_sql_{suffix}"
    report_title = f"Smoke Report {suffix}"
    csv_bytes = (
        b"date,revenue,region\n"
        b"2026-07-01,120,East\n"
        b"2026-07-02,180,West\n"
        b"2026-07-03,90,East\n"
    )
    parquet_bytes = build_sample_parquet_bytes()
    xlsx_bytes = build_sample_xlsx_bytes()

    with TestClient(app) as client:
        response = client.get("/api/workspace/quota")
        assert response.status_code == 200, response.text
        quota_payload = response.json()
        quota_resources = {
            "data_sources",
            "projects",
            "schedules",
            "reports",
            "concurrent_runs",
            "storage_bytes",
        }
        assert set(quota_payload["limits"]) == quota_resources
        assert set(quota_payload["usage"]) == quota_resources
        assert all(value >= 0 for value in quota_payload["usage"].values())

        response = client.post(
            "/data-sources",
            data={"name": source_name},
            files={"file": ("sales.csv", csv_bytes, "text/csv")},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text

        with connect() as conn:
            source = conn.execute("SELECT * FROM data_sources WHERE name = ?", (source_name,)).fetchone()
        assert source is not None, "source missing"
        quality = decode_json(source["quality_json"], {})
        assert quality["completeness"] == 100.0, quality
        assert quality["duplicate_rows"] == 0, quality

        sql = "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;"
        response = client.post(
            "/projects",
            data={"name": project_name, "language": "sql", "data_source_id": source["id"], "script": sql},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text

        with connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE name = ?", (project_name,)).fetchone()
        assert project is not None, "project missing"

        response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
        assert response.status_code == 303, response.text
        run_path = response.headers["location"]
        assert run_path.startswith("/runs/"), run_path

        with connect() as conn:
            run = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1",
                (project["id"],),
            ).fetchone()
        assert run is not None and run["status"] == "succeeded", dict(run) if run else None
        result = decode_json(run["result_json"], {})
        assert result["columns"] == ["region", "revenue"], result
        assert result["rows"][0] == ["East", 210], result

        response = client.post(
            "/projects",
            data={
                "name": parameter_project_name,
                "language": "sql",
                "data_source_id": source["id"],
                "script": "SELECT $region AS selected_region;",
                "parameters_json": '{"region": "East"}',
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            parameter_project = conn.execute("SELECT * FROM projects WHERE name = ?", (parameter_project_name,)).fetchone()
        response = client.post(f"/projects/{parameter_project['id']}/run", follow_redirects=False)
        assert response.status_code == 303, response.text
        with connect() as conn:
            parameter_run = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1",
                (parameter_project["id"],),
            ).fetchone()
        assert parameter_run is not None and parameter_run["status"] == "succeeded", dict(parameter_run) if parameter_run else None
        assert decode_json(parameter_run["parameters_json"], {}) == {"region": "East"}
        assert decode_json(parameter_run["result_json"], {})["rows"] == [["East"]]

        response = client.post(
            "/data-sources",
            data={"name": parquet_source_name},
            files={"file": ("sales.parquet", parquet_bytes, "application/octet-stream")},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            parquet_source = conn.execute("SELECT * FROM data_sources WHERE name = ?", (parquet_source_name,)).fetchone()
        assert parquet_source is not None and parquet_source["source_type"] == "parquet", dict(parquet_source) if parquet_source else None
        response = client.post(
            "/projects",
            data={"name": parquet_project_name, "language": "sql", "data_source_id": parquet_source["id"], "script": sql},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            parquet_project = conn.execute("SELECT * FROM projects WHERE name = ?", (parquet_project_name,)).fetchone()
        response = client.post(f"/projects/{parquet_project['id']}/run", follow_redirects=False)
        assert response.status_code == 303, response.text
        with connect() as conn:
            parquet_run = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1",
                (parquet_project["id"],),
            ).fetchone()
        assert parquet_run is not None and parquet_run["status"] == "succeeded", dict(parquet_run) if parquet_run else None
        parquet_result = decode_json(parquet_run["result_json"], {})
        assert parquet_result["rows"][0] == ["East", 210], parquet_result

        response = client.post(
            "/data-sources",
            data={"name": xlsx_source_name},
            files={"file": ("sales.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            xlsx_source = conn.execute("SELECT * FROM data_sources WHERE name = ?", (xlsx_source_name,)).fetchone()
        assert xlsx_source is not None and xlsx_source["source_type"] == "xlsx", dict(xlsx_source) if xlsx_source else None
        response = client.post(
            "/projects",
            data={"name": xlsx_project_name, "language": "sql", "data_source_id": xlsx_source["id"], "script": sql},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            xlsx_project = conn.execute("SELECT * FROM projects WHERE name = ?", (xlsx_project_name,)).fetchone()
        response = client.post(f"/projects/{xlsx_project['id']}/run", follow_redirects=False)
        assert response.status_code == 303, response.text
        with connect() as conn:
            xlsx_run = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1",
                (xlsx_project["id"],),
            ).fetchone()
        assert xlsx_run is not None and xlsx_run["status"] == "succeeded", dict(xlsx_run) if xlsx_run else None
        xlsx_result = decode_json(xlsx_run["result_json"], {})
        assert xlsx_result["rows"][0] == ["East", 210], xlsx_result

        response = client.get(run_path)
        assert response.status_code == 200, response.text
        assert "Run Details" in response.text
        response = client.get(f"/runs/{run['id']}/result.csv")
        assert response.status_code == 200, response.text
        assert "East,210" in response.text

        response = client.post(
            "/reports",
            data={"project_id": project["id"], "title": report_title, "description": "Latest"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        report_path = response.headers["location"]
        response = client.get(report_path)
        assert response.status_code == 200, response.text
        assert report_title in response.text
        assert "West" in response.text
        response = client.get(f"{report_path}/snapshot.csv")
        assert response.status_code == 200, response.text
        assert "East,210" in response.text
        response = client.get(f"{report_path}/snapshot.json")
        assert response.status_code == 200, response.text
        assert response.json()["rows"][0] == ["East", 210], response.json()
        response = client.get(f"{report_path}/snapshot.xlsx")
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"PK")
        response = client.get(f"{report_path}/snapshot.png")
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        response = client.get(f"{report_path}/snapshot.pdf")
        assert response.status_code == 200, response.text
        assert response.content.startswith(b"%PDF-")

        response = client.post(
            "/schedules",
            data={"project_id": project["id"], "name": f"schedule_{suffix}", "interval_minutes": 1},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()
        assert schedule is not None, "schedule missing"

        response = client.post(f"/schedules/{schedule['id']}/run", follow_redirects=False)
        assert response.status_code == 303, response.text
        manual_schedule_run_id = response.headers["location"].rsplit("/", 1)[-1]
        with connect() as conn:
            manual_schedule_run = conn.execute("SELECT * FROM runs WHERE id = ?", (manual_schedule_run_id,)).fetchone()
            schedule = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule["id"],)).fetchone()
            report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_path.rsplit("/", 1)[-1],)).fetchone()
            scheduled_snapshot = conn.execute(
                "SELECT * FROM report_snapshots WHERE report_id = ? ORDER BY created_at DESC LIMIT 1",
                (report["id"],),
            ).fetchone()
        assert manual_schedule_run is not None and manual_schedule_run["status"] == "succeeded", dict(manual_schedule_run)
        assert manual_schedule_run["trigger_type"] == "schedule_manual", dict(manual_schedule_run)
        assert schedule["last_run_at"] is not None, dict(schedule)
        assert scheduled_snapshot is not None and scheduled_snapshot["run_id"] == manual_schedule_run_id, dict(scheduled_snapshot)

        with connect() as conn:
            conn.execute(
                "UPDATE schedules SET next_run_at = ? WHERE project_id = ?",
                (now_iso(), project["id"]),
            )
        claimed = claim_due_schedules()
        assert claimed, "schedule was not claimed"
        execute_run(claimed[0]["run_id"])
        with connect() as conn:
            scheduled_run = conn.execute("SELECT * FROM runs WHERE id = ?", (claimed[0]["run_id"],)).fetchone()
        assert scheduled_run is not None and scheduled_run["status"] == "succeeded", dict(scheduled_run)

        response = client.post(
            "/projects",
            data={
                "name": failed_project_name,
                "language": "sql",
                "data_source_id": source["id"],
                "script": "SELECT missing_column FROM data;",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with connect() as conn:
            failed_project = conn.execute("SELECT * FROM projects WHERE name = ?", (failed_project_name,)).fetchone()
        response = client.post(f"/projects/{failed_project['id']}/run", follow_redirects=False)
        assert response.status_code == 303, response.text
        response = client.get("/api/notifications")
        assert response.status_code == 200, response.text
        notifications = response.json()
        assert notifications and notifications[0]["event_type"] == "run.failed", notifications
        response = client.get("/")
        assert "Run failed" in response.text

    print("smoke test passed")


if __name__ == "__main__":
    main()
