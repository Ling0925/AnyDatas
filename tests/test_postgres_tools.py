from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import postgres_tools


def test_rewrite_dollar_parameters_preserves_literals_comments_and_dollar_quotes():
    sql = """SELECT $minimum AS value, '$literal', \"$identifier\" -- $comment
/* $block */ WHERE label = $label AND body = $$ $body $$"""

    assert postgres_tools.rewrite_dollar_parameters(sql) == """SELECT %(minimum)s AS value, '$literal', \"$identifier\" -- $comment
/* $block */ WHERE label = %(label)s AND body = $$ $body $$"""


def test_validate_postgres_read_only_sql_allows_one_safe_query_and_rejects_mutations():
    postgres_tools.validate_postgres_read_only_sql("WITH totals AS (SELECT SUM(revenue) AS value FROM sales) SELECT value FROM totals;")
    postgres_tools.validate_postgres_read_only_sql("SELECT 'DELETE FROM sales' AS explanation")
    postgres_tools.validate_postgres_read_only_sql("SELECT $delete AS safe_parameter")

    with pytest.raises(ValueError, match="exactly one"):
        postgres_tools.validate_postgres_read_only_sql("SELECT 1; SELECT 2;")
    with pytest.raises(ValueError, match="write"):
        postgres_tools.validate_postgres_read_only_sql("WITH removed AS (DELETE FROM sales RETURNING *) SELECT * FROM removed;")
    with pytest.raises(ValueError, match="write"):
        postgres_tools.validate_postgres_read_only_sql("SELECT * FROM sales FOR UPDATE")


def test_parse_postgres_connection_and_identifiers_reject_unsafe_values():
    assert postgres_tools.parse_postgres_connection_url("postgresql://readonly:password@db.example.com:5432/warehouse") == "postgresql://readonly:password@db.example.com:5432/warehouse"
    assert postgres_tools.parse_postgres_identifier("analytics_2026", "schema") == "analytics_2026"
    with pytest.raises(ValueError, match="connection URL"):
        postgres_tools.parse_postgres_connection_url("sqlite:///tmp/warehouse.sqlite3")
    with pytest.raises(ValueError, match="PostgreSQL table"):
        postgres_tools.parse_postgres_identifier("sales; DROP TABLE users", "table")


def test_inspect_postgres_table_uses_quoted_identifiers_and_json_safe_preview(monkeypatch):
    executed: list[tuple[str, tuple | None]] = []

    class FakeCursor:
        description = [SimpleNamespace(name="region"), SimpleNamespace(name="revenue"), SimpleNamespace(name="closed_on")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            executed.append((statement, params))

        def fetchall(self):
            return [("East", Decimal("120.50"), date(2026, 7, 11)), ("West", Decimal("180"), None)]

        def fetchone(self):
            return (2,)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return FakeCursor()

    def fake_connect(connection_url, **kwargs):
        assert connection_url == "postgresql://readonly:password@db.example.com/warehouse"
        assert kwargs == {"connect_timeout": 5, "autocommit": True}
        return FakeConnection()

    monkeypatch.setattr(postgres_tools.psycopg, "connect", fake_connect)

    columns, preview, row_count, quality = postgres_tools.inspect_postgres_table(
        "postgresql://readonly:password@db.example.com/warehouse",
        "analytics",
        "daily_sales",
    )

    assert columns == ["region", "revenue", "closed_on"]
    assert preview == [
        {"region": "East", "revenue": "120.50", "closed_on": "2026-07-11"},
        {"region": "West", "revenue": "180", "closed_on": None},
    ]
    assert row_count == 2
    assert quality["row_count"] == 2
    assert quality["sampled_rows"] == 2
    assert executed == [
        ("SET statement_timeout TO 5000", None),
        ('SELECT * FROM "analytics"."daily_sales" LIMIT %s', (50,)),
        ('SELECT COUNT(*) FROM "analytics"."daily_sales"', None),
    ]
