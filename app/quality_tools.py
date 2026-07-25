from __future__ import annotations

from typing import Any, Iterable


def is_empty_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def normalize_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_quality_summary(columns: list[str], rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    stats = {
        column: {
            "name": column,
            "empty": 0,
            "filled": 0,
            "unique_values": set(),
            "sample_values": [],
        }
        for column in columns
    }
    seen_rows: set[tuple[Any, ...]] = set()
    duplicate_rows = 0
    row_count = 0

    for row in rows:
        row_count += 1
        fingerprint = tuple(normalize_value(row.get(column)) for column in columns)
        if fingerprint in seen_rows:
            duplicate_rows += 1
        else:
            seen_rows.add(fingerprint)

        for column in columns:
            value = row.get(column)
            column_stats = stats[column]
            if is_empty_value(value):
                column_stats["empty"] += 1
                continue
            column_stats["filled"] += 1
            normalized = normalize_value(value)
            column_stats["unique_values"].add(normalized)
            if len(column_stats["sample_values"]) < 5 and normalized not in column_stats["sample_values"]:
                column_stats["sample_values"].append(normalized)

    total_cells = row_count * len(columns)
    empty_cells = sum(column_stats["empty"] for column_stats in stats.values())
    filled_cells = total_cells - empty_cells
    column_summaries = []
    for column in columns:
        column_stats = stats[column]
        column_summaries.append(
            {
                "name": column,
                "empty": column_stats["empty"],
                "filled": column_stats["filled"],
                "unique": len(column_stats["unique_values"]),
                "sample_values": column_stats["sample_values"],
            }
        )

    return {
        "row_count": row_count,
        "column_count": len(columns),
        "empty_cells": empty_cells,
        "filled_cells": filled_cells,
        "duplicate_rows": duplicate_rows,
        "completeness": 0 if total_cells == 0 else round((filled_cells / total_cells) * 100, 2),
        "columns": column_summaries,
    }
