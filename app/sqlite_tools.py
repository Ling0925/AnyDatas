from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .quality_tools import build_quality_summary


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def inspect_sqlite_table(path: Path, table_name: str, preview_limit: int = 50) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name = ?
            """,
            (table_name,),
        ).fetchone()
        if table is None:
            raise ValueError(f"Table or view not found: {table_name}")

        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()]
        rows = conn.execute(f"SELECT * FROM {quote_identifier(table_name)} LIMIT ?", (preview_limit,)).fetchall()
        preview = [{column: row[column] for column in columns} for row in rows]
        all_rows = [
            {column: row[column] for column in columns}
            for row in conn.execute(f"SELECT * FROM {quote_identifier(table_name)}").fetchall()
        ]
        row_count = conn.execute(f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table_name)}").fetchone()["row_count"]
    return columns, preview, int(row_count), build_quality_summary(columns, all_rows)
