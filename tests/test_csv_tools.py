from __future__ import annotations

from app.csv_tools import inspect_csv


def test_inspect_csv_reads_columns_preview_and_count(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("date,revenue\n2026-07-01,120\n2026-07-02,180\n", encoding="utf-8")

    columns, preview, row_count, quality = inspect_csv(path, preview_limit=1)

    assert columns == ["date", "revenue"]
    assert preview == [{"date": "2026-07-01", "revenue": "120"}]
    assert row_count == 2
    assert quality["row_count"] == 2
    assert quality["column_count"] == 2
    assert quality["empty_cells"] == 0
    assert quality["duplicate_rows"] == 0


def test_inspect_csv_builds_quality_summary(tmp_path):
    path = tmp_path / "messy.csv"
    path.write_text(
        "region,revenue\n"
        "East,120\n"
        "West,\n"
        "East,120\n",
        encoding="utf-8",
    )

    _columns, _preview, row_count, quality = inspect_csv(path)

    assert row_count == 3
    assert quality["empty_cells"] == 1
    assert quality["duplicate_rows"] == 1
    assert quality["completeness"] == 83.33
    revenue = next(column for column in quality["columns"] if column["name"] == "revenue")
    assert revenue["empty"] == 1
    assert revenue["unique"] == 1
