from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from .quality_tools import build_quality_summary


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def serialize_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def row_to_dict(columns: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    return {column: serialize_cell(row[index]) for index, column in enumerate(columns)}


def inspect_parquet(path: Path, preview_limit: int = 50) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Parquet file not found: {path}")
    parquet_ref = quote_literal(str(path))
    with duckdb.connect(database=":memory:") as conn:
        preview_relation = conn.execute(f"SELECT * FROM read_parquet({parquet_ref}) LIMIT ?", (preview_limit,))
        columns = [item[0] for item in (preview_relation.description or [])]
        preview = [row_to_dict(columns, row) for row in preview_relation.fetchall()]
        rows_relation = conn.execute(f"SELECT * FROM read_parquet({parquet_ref})")
        rows = [row_to_dict(columns, row) for row in rows_relation.fetchall()]
        row_count = conn.execute(f"SELECT COUNT(*) AS row_count FROM read_parquet({parquet_ref})").fetchone()[0]
    return columns, preview, int(row_count), build_quality_summary(columns, rows)
