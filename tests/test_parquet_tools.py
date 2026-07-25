from __future__ import annotations

from app.parquet_tools import inspect_parquet


def test_inspect_parquet_reads_columns_preview_count_and_quality(tmp_path, sample_parquet_bytes):
    path = tmp_path / "sales.parquet"
    path.write_bytes(sample_parquet_bytes)

    columns, preview, row_count, quality = inspect_parquet(path, preview_limit=1)

    assert columns == ["date", "revenue", "region"]
    assert preview == [{"date": "2026-07-01", "revenue": 120, "region": "East"}]
    assert row_count == 3
    assert quality["row_count"] == 3
    assert quality["column_count"] == 3
    assert quality["empty_cells"] == 0
    assert quality["duplicate_rows"] == 0
