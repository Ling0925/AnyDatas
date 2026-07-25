from __future__ import annotations

from app.xlsx_tools import inspect_xlsx, read_xlsx_rows, render_xlsx


def test_inspect_xlsx_reads_first_sheet_preview_count_and_quality(tmp_path, sample_xlsx_bytes):
    path = tmp_path / "sales.xlsx"
    path.write_bytes(sample_xlsx_bytes)

    columns, preview, row_count, quality, sheet_name, rows = inspect_xlsx(path, preview_limit=1)

    assert sheet_name == "Sales"
    assert columns == ["date", "revenue", "region"]
    assert preview == [{"date": "2026-07-01", "revenue": 120, "region": "East"}]
    assert rows[-1] == {"date": "2026-07-03", "revenue": 90, "region": "East"}
    assert row_count == 3
    assert quality["row_count"] == 3
    assert quality["column_count"] == 3
    assert quality["empty_cells"] == 0
    assert quality["duplicate_rows"] == 0


def test_render_xlsx_round_trips_values_and_keeps_formula_like_text_inert(tmp_path):
    workbook = render_xlsx(
        ["region", "revenue", "active", "note"],
        [
            ["East", 210, True, "=HYPERLINK(\"https://example.com\")"],
            ["West", 180.5, False, "+SUM(1,2)"],
            ["Emoji \U0001f4ca", 10**400, True, "control\x00removed"],
        ],
        sheet_name='\x00Report "QA"',
    )
    path = tmp_path / "report.xlsx"
    path.write_bytes(workbook)

    columns, rows, sheet_name = read_xlsx_rows(path)

    assert workbook.startswith(b"PK")
    assert columns == ["region", "revenue", "active", "note"]
    assert rows == [
        {"region": "East", "revenue": 210, "active": True, "note": '=HYPERLINK("https://example.com")'},
        {"region": "West", "revenue": 180.5, "active": False, "note": "+SUM(1,2)"},
        {"region": "Emoji \U0001f4ca", "revenue": str(10**400), "active": True, "note": "controlremoved"},
    ]
    assert sheet_name == 'Report "QA"'
