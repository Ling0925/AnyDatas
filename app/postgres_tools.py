from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import psycopg

from .quality_tools import build_quality_summary
from .sql_tools import mask_sql_literals_and_comments, rewrite_dollar_parameters


POSTGRES_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
POSTGRES_INSPECTION_TIMEOUT_MS = 5_000
POSTGRES_READ_ONLY_START_PATTERN = re.compile(r"^\s*(?:SELECT|WITH|EXPLAIN|VALUES)\b", re.IGNORECASE)
POSTGRES_WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(?:ALTER|ANALYZE|CALL|COPY|CREATE|DELETE|DISCARD|DO|DROP|GRANT|INSERT|LISTEN|LOCK|MERGE|NOTIFY|RESET|REVOKE|SET|TRUNCATE|UNLISTEN|UPDATE|VACUUM)\b|\bINTO\b",
    re.IGNORECASE,
)
POSTGRES_LOCKING_QUERY_PATTERN = re.compile(r"\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)


def parse_postgres_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not POSTGRES_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(f"PostgreSQL {label} must start with a letter or underscore and use letters, numbers, underscores, or $.")
    return normalized


def parse_postgres_connection_url(value: str) -> str:
    connection_url = value.strip()
    parsed = urlparse(connection_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("PostgreSQL secret values must use a postgres:// or postgresql:// connection URL.")
    return connection_url


def quote_postgres_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    return str(value)


def validate_postgres_read_only_sql(sql: str) -> None:
    masked_sql = mask_sql_literals_and_comments(sql)
    statements = [statement for statement in masked_sql.split(";") if statement.strip()]
    if len(statements) != 1:
        raise ValueError("PostgreSQL projects must contain exactly one read-only query.")
    statement = statements[0]
    if not POSTGRES_READ_ONLY_START_PATTERN.match(statement):
        raise ValueError("PostgreSQL projects must start with SELECT, WITH, EXPLAIN, or VALUES.")
    if POSTGRES_WRITE_KEYWORD_PATTERN.search(statement) or POSTGRES_LOCKING_QUERY_PATTERN.search(statement):
        raise ValueError("PostgreSQL projects cannot contain write, DDL, session-control, or locking statements.")


def inspect_postgres_table(
    connection_url: str,
    schema_name: str,
    table_name: str,
    preview_limit: int = 50,
) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any]]:
    schema = parse_postgres_identifier(schema_name, "schema")
    table = parse_postgres_identifier(table_name, "table")
    table_sql = f"{quote_postgres_identifier(schema)}.{quote_postgres_identifier(table)}"
    with psycopg.connect(connection_url, connect_timeout=5, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SET statement_timeout TO {POSTGRES_INSPECTION_TIMEOUT_MS}")
            cursor.execute(f"SELECT * FROM {table_sql} LIMIT %s", (preview_limit,))
            columns = [field.name for field in (cursor.description or [])]
            preview = [
                {column: json_safe_value(value) for column, value in zip(columns, row)}
                for row in cursor.fetchall()
            ]
            cursor.execute(f"SELECT COUNT(*) FROM {table_sql}")
            count_row = cursor.fetchone()
            row_count = int(count_row[0] if count_row else 0)
    quality = build_quality_summary(columns, preview)
    quality["sampled_rows"] = quality["row_count"]
    quality["row_count"] = row_count
    return columns, preview, row_count, quality
