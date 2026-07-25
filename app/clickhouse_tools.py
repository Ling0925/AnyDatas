from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from .quality_tools import build_quality_summary
from .sql_tools import mask_sql_literals_and_comments


CLICKHOUSE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,126}$")
CLICKHOUSE_READ_ONLY_START_PATTERN = re.compile(r"^\s*(?:SELECT|WITH|EXPLAIN)\b", re.IGNORECASE)
CLICKHOUSE_WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(?:ALTER|ATTACH|CHECK|CREATE|DELETE|DETACH|DROP|INSERT|KILL|MOVE|OPTIMIZE|RENAME|REPLACE|SET|SYSTEM|TRUNCATE|UNDROP|UPDATE|USE)\b|\bINTO\b",
    re.IGNORECASE,
)
CLICKHOUSE_QUERY_SETTINGS = {
    "readonly": 1,
    "max_execution_time": 45,
    "max_result_rows": 500,
    "result_overflow_mode": "break",
}


def parse_clickhouse_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not CLICKHOUSE_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"ClickHouse {label} must start with a letter or underscore and use letters, numbers, underscores, or $."
        )
    return normalized


def parse_clickhouse_connection_url(value: str) -> str:
    connection_url = value.strip()
    parsed = urlparse(connection_url)
    if parsed.scheme not in {"clickhouse", "clickhouses"} or not parsed.hostname:
        raise ValueError("ClickHouse secret values must use a clickhouse:// or clickhouses:// connection URL.")
    if not parsed.username:
        raise ValueError("ClickHouse connection URLs must include a username.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("ClickHouse connection URLs must use a valid port.") from exc
    return connection_url


def quote_clickhouse_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def clickhouse_client_options(connection_url: str, database_name: str) -> dict[str, Any]:
    parsed = urlparse(parse_clickhouse_connection_url(connection_url))
    database = parse_clickhouse_identifier(database_name, "database")
    secure = parsed.scheme == "clickhouses"
    return {
        "host": parsed.hostname,
        "port": parsed.port or (8443 if secure else 8123),
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "secure": secure,
        "connect_timeout": 5,
        "send_receive_timeout": 5,
    }


def validate_clickhouse_read_only_sql(sql: str) -> None:
    masked_sql = mask_sql_literals_and_comments(sql)
    statements = [statement for statement in masked_sql.split(";") if statement.strip()]
    if len(statements) != 1:
        raise ValueError("ClickHouse projects must contain exactly one read-only query.")
    statement = statements[0]
    if not CLICKHOUSE_READ_ONLY_START_PATTERN.match(statement):
        raise ValueError("ClickHouse projects must start with SELECT, WITH, or EXPLAIN.")
    if CLICKHOUSE_WRITE_KEYWORD_PATTERN.search(statement):
        raise ValueError("ClickHouse projects cannot contain write, DDL, session-control, or system statements.")


def clickhouse_parameter_type(value: Any) -> str:
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int64"
    if isinstance(value, float):
        return "Float64"
    if isinstance(value, str):
        return "String"
    raise ValueError("ClickHouse parameters support only string, integer, number, and boolean values.")


def rewrite_clickhouse_parameters(sql: str, parameters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result: list[str] = []
    bound: dict[str, Any] = {}
    index = 0
    while index < len(sql):
        character = sql[index]
        if character in {"'", '"'}:
            quote = character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    index += 1
                    if index < len(sql) and sql[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
            result.append(sql[start:index])
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            end = len(sql) if newline < 0 else newline
            result.append(sql[index:end])
            index = end
            continue
        if sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            end = len(sql) if close < 0 else close + 2
            result.append(sql[index:end])
            index = end
            continue
        if character == "$":
            start = index + 1
            if start < len(sql) and (sql[start].isalpha() or sql[start] == "_"):
                end = start + 1
                while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                    end += 1
                name = sql[start:end]
                if name not in parameters:
                    raise ValueError(f"ClickHouse parameter is missing: {name}")
                bound[name] = parameters[name]
                result.append(f"{{{name}:{clickhouse_parameter_type(parameters[name])}}}")
                index = end
                continue
        result.append(character)
        index += 1
    return "".join(result), bound


def json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    return str(value)


def inspect_clickhouse_table(
    connection_url: str,
    database_name: str,
    table_name: str,
    preview_limit: int = 50,
) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any]]:
    import clickhouse_connect

    database = parse_clickhouse_identifier(database_name, "database")
    table = parse_clickhouse_identifier(table_name, "table")
    table_sql = f"{quote_clickhouse_identifier(database)}.{quote_clickhouse_identifier(table)}"
    client = clickhouse_connect.get_client(**clickhouse_client_options(connection_url, database))
    try:
        preview_result = client.query(
            f"SELECT * FROM {table_sql} LIMIT {int(preview_limit)}",
            settings={**CLICKHOUSE_QUERY_SETTINGS, "max_execution_time": 5},
        )
        columns = [str(column) for column in preview_result.column_names]
        preview = [
            {column: json_safe_value(value) for column, value in zip(columns, row)}
            for row in preview_result.result_rows
        ]
        count_result = client.query(
            f"SELECT count() AS row_count FROM {table_sql}",
            settings={**CLICKHOUSE_QUERY_SETTINGS, "max_execution_time": 5, "max_result_rows": 1},
        )
        row_count = int(count_result.result_rows[0][0] if count_result.result_rows else 0)
    finally:
        client.close()
    quality = build_quality_summary(columns, preview)
    quality["sampled_rows"] = quality["row_count"]
    quality["row_count"] = row_count
    return columns, preview, row_count, quality
