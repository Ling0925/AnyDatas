from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import mysql_tools
from app.sql_tools import rewrite_dollar_parameters


def test_mysql_validation_rewrites_parameters_and_rejects_unsafe_queries():
    sql = "SELECT $minimum, '$literal', $tag$keep $tag$, # $comment\n $maximum"

    assert rewrite_dollar_parameters(sql, hash_line_comments=True) == "SELECT %(minimum)s, '$literal', $tag$keep $tag$, # $comment\n %(maximum)s"
    mysql_tools.validate_mysql_read_only_sql("# DELETE FROM data\nSELECT * FROM sales WHERE id = $id")
    with pytest.raises(ValueError, match="exactly one"):
        mysql_tools.validate_mysql_read_only_sql("SELECT 1; SELECT 2")
    with pytest.raises(ValueError, match="write"):
        mysql_tools.validate_mysql_read_only_sql("WITH changed AS (DELETE FROM sales RETURNING id) SELECT * FROM changed")
    with pytest.raises(ValueError, match="locking"):
        mysql_tools.validate_mysql_read_only_sql("SELECT * FROM sales LOCK IN SHARE MODE")
    with pytest.raises(ValueError, match="executable comments"):
        mysql_tools.validate_mysql_read_only_sql("SELECT 1 /*!50000 FOR UPDATE */")
    mysql_tools.validate_mysql_read_only_sql("SELECT '/*!50000 FOR UPDATE */' AS literal_value")


def test_mysql_connection_url_and_identifier_validation():
    options = mysql_tools.mysql_connection_options(
        "mysql+pymysql://analyst:pass%20word@db.example.test:3307/ignored",
        "warehouse",
    )

    assert options["host"] == "db.example.test"
    assert options["port"] == 3307
    assert options["user"] == "analyst"
    assert options["password"] == "pass word"
    assert options["database"] == "warehouse"
    assert options["autocommit"] is False
    with pytest.raises(ValueError, match="mysql://"):
        mysql_tools.parse_mysql_connection_url("postgres://analyst@db.example.test/data")
    with pytest.raises(ValueError, match="username"):
        mysql_tools.parse_mysql_connection_url("mysql://db.example.test/data")
    with pytest.raises(ValueError, match="database"):
        mysql_tools.parse_mysql_identifier("warehouse-prod", "database")


def test_mysql_table_inspection_uses_read_only_transaction_and_safe_preview(monkeypatch):
    class FakeCursor:
        description = [("id",), ("seen_at",)]

        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            self.calls.append((statement, params))

        def fetchall(self):
            return [{"id": 1, "seen_at": datetime(2026, 7, 11, tzinfo=timezone.utc)}]

        def fetchone(self):
            return {"row_count": 7}

    class FakeConnection:
        def __init__(self):
            self.cursor_instance = FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self.cursor_instance

    fake_connection = FakeConnection()
    captured_options = {}

    def fake_connect(**options):
        captured_options.update(options)
        return fake_connection

    monkeypatch.setattr(mysql_tools.pymysql, "connect", fake_connect)

    columns, preview, row_count, quality = mysql_tools.inspect_mysql_table(
        "mysql://analyst:password@db.example.test:3306/warehouse",
        "warehouse",
        "daily_sales",
    )

    assert captured_options["database"] == "warehouse"
    assert captured_options["connect_timeout"] == 5
    assert columns == ["id", "seen_at"]
    assert preview == [{"id": 1, "seen_at": "2026-07-11 00:00:00+00:00"}]
    assert row_count == 7
    assert quality["row_count"] == 7
    assert quality["sampled_rows"] == 1
    assert fake_connection.cursor_instance.calls == [
        ("SET SESSION MAX_EXECUTION_TIME = %s", (5000,)),
        ("SET TRANSACTION READ ONLY", None),
        ("START TRANSACTION READ ONLY", None),
        ("SELECT * FROM `warehouse`.`daily_sales` LIMIT %s", (50,)),
        ("SELECT COUNT(*) AS row_count FROM `warehouse`.`daily_sales`", None),
    ]
