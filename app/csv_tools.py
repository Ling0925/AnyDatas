from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .quality_tools import build_quality_summary


def inspect_csv(path: Path, preview_limit: int = 50) -> tuple[list[str], list[dict[str, str]], int, dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        preview: list[dict[str, str]] = []
        rows: list[dict[str, str]] = []
        row_count = 0
        for row in reader:
            row_count += 1
            normalized_row = {column: row.get(column, "") for column in columns}
            rows.append(normalized_row)
            if len(preview) < preview_limit:
                preview.append(normalized_row)
    return columns, preview, row_count, build_quality_summary(columns, rows)
