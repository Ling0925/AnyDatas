from __future__ import annotations

import os
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import duckdb
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_DIR = ROOT / ".test-var"

os.environ["ANYDATAS_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["ANYDATAS_DISABLE_SCHEDULER"] = "1"
os.environ["ANYDATAS_RUNNER"] = "local"

from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_data_dir():
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
    init_db()
    yield
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def sample_csv_bytes() -> bytes:
    return (
        b"date,revenue,region\n"
        b"2026-07-01,120,East\n"
        b"2026-07-02,180,West\n"
        b"2026-07-03,90,East\n"
    )


@pytest.fixture()
def sample_parquet_bytes(tmp_path) -> bytes:
    path = tmp_path / "sales.parquet"
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


def build_xlsx_cell(reference: str, value, cell_type: str = "inlineStr") -> str:
    if cell_type == "n":
        return f'<c r="{reference}"><v>{value}</v></c>'
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


@pytest.fixture()
def sample_xlsx_bytes() -> bytes:
    rows = [
        [
            build_xlsx_cell("A1", "date"),
            build_xlsx_cell("B1", "revenue"),
            build_xlsx_cell("C1", "region"),
        ],
        [
            build_xlsx_cell("A2", "2026-07-01"),
            build_xlsx_cell("B2", 120, "n"),
            build_xlsx_cell("C2", "East"),
        ],
        [
            build_xlsx_cell("A3", "2026-07-02"),
            build_xlsx_cell("B3", 180, "n"),
            build_xlsx_cell("C3", "West"),
        ],
        [
            build_xlsx_cell("A4", "2026-07-03"),
            build_xlsx_cell("B4", 90, "n"),
            build_xlsx_cell("C4", "East"),
        ],
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


def latest_row(table: str, where: str = "1 = 1", params: tuple = ()):
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
