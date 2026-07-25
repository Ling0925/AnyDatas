from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.clickhouse_tools import (
    clickhouse_client_options,
    inspect_clickhouse_table,
    parse_clickhouse_connection_url,
    rewrite_clickhouse_parameters,
    validate_clickhouse_read_only_sql,
)


def test_clickhouse_connection_and_read_only_validation():
    assert clickhouse_client_options(
        "clickhouses://reader:p%40ss@ch.example.com/ignored",
        "analytics",
    ) == {
        "host": "ch.example.com",
        "port": 8443,
        "username": "reader",
        "password": "p@ss",
        "database": "analytics",
        "secure": True,
        "connect_timeout": 5,
        "send_receive_timeout": 5,
    }
    with pytest.raises(ValueError, match="clickhouse://"):
        parse_clickhouse_connection_url("https://ch.example.com")
    with pytest.raises(ValueError, match="username"):
        parse_clickhouse_connection_url("clickhouse://ch.example.com")
    validate_clickhouse_read_only_sql("WITH totals AS (SELECT 1) SELECT * FROM totals")
    with pytest.raises(ValueError, match="exactly one"):
        validate_clickhouse_read_only_sql("SELECT 1; SELECT 2")
    with pytest.raises(ValueError, match="cannot contain"):
        validate_clickhouse_read_only_sql("SELECT * FROM data INTO OUTFILE 'result.csv'")
    with pytest.raises(ValueError, match="must start"):
        validate_clickhouse_read_only_sql("SYSTEM FLUSH LOGS")


def test_clickhouse_parameters_are_typed_without_rewriting_literals_or_comments():
    sql, parameters = rewrite_clickhouse_parameters(
        "SELECT '$minimum' AS literal, value FROM data WHERE value >= $minimum AND active = $active -- $ignored",
        {"minimum": 12, "active": True},
    )

    assert sql == (
        "SELECT '$minimum' AS literal, value FROM data "
        "WHERE value >= {minimum:Int64} AND active = {active:Bool} -- $ignored"
    )
    assert parameters == {"minimum": 12, "active": True}
    with pytest.raises(ValueError, match="missing"):
        rewrite_clickhouse_parameters("SELECT $missing", {})
    with pytest.raises(ValueError, match="support only"):
        rewrite_clickhouse_parameters("SELECT $items", {"items": [1, 2]})


def test_inspect_clickhouse_table_uses_readonly_settings_and_closes_client(monkeypatch):
    calls = []

    class Result:
        def __init__(self, columns, rows):
            self.column_names = columns
            self.result_rows = rows

    class Client:
        closed = False

        def query(self, query, settings):
            calls.append((query, settings))
            if query.startswith("SELECT count"):
                return Result(["row_count"], [(250,)])
            return Result(["region", "revenue"], [("East", 120), ("West", 180)])

        def close(self):
            self.closed = True

    client = Client()
    fake_module = SimpleNamespace(get_client=lambda **options: calls.append(("options", options)) or client)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_module)

    columns, preview, row_count, quality = inspect_clickhouse_table(
        "clickhouse://reader:secret@ch.example.com:8123/warehouse",
        "analytics",
        "daily_sales",
    )

    assert columns == ["region", "revenue"]
    assert preview == [{"region": "East", "revenue": 120}, {"region": "West", "revenue": 180}]
    assert row_count == 250
    assert quality["sampled_rows"] == 2
    assert quality["row_count"] == 250
    assert client.closed is True
    assert calls[0][0] == "options"
    assert calls[1][0] == "SELECT * FROM `analytics`.`daily_sales` LIMIT 50"
    assert calls[1][1]["readonly"] == 1
