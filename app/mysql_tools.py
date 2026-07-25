from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

import pymysql

from .quality_tools import build_quality_summary
from .sql_tools import mask_sql_literals_and_comments


MYSQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
MYSQL_INSPECTION_TIMEOUT_MS = 5_000
MYSQL_READ_ONLY_START_PATTERN = re.compile(r"^\s*(?:SELECT|WITH|EXPLAIN|VALUES)\b", re.IGNORECASE)
MYSQL_WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(?:ALTER|ANALYZE|BEGIN|BINLOG|CALL|CHANGE|CHECK|COMMIT|CREATE|DEALLOCATE|DELETE|DESCRIBE|DO|DROP|EXECUTE|FLUSH|GRANT|HANDLER|INSERT|INSTALL|KILL|LOAD|LOCK|OPTIMIZE|PREPARE|PURGE|RENAME|REPAIR|REPLACE|RESET|REVOKE|ROLLBACK|SET|SHOW|SIGNAL|START|TRUNCATE|UNINSTALL|UNLOCK|UPDATE|USE|XA)\b|\bINTO\b",
    re.IGNORECASE,
)
MYSQL_LOCKING_QUERY_PATTERN = re.compile(r"\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b|\bLOCK\s+IN\s+SHARE\s+MODE\b", re.IGNORECASE)


def parse_mysql_identifier(value: str, label: str) -> str:
    normalized = value.strip()
    if not MYSQL_IDENTIFIER_PATTERN.fullmatch(normalized):
        raise ValueError(f"MySQL {label} must start with a letter or underscore and use letters, numbers, underscores, or $.")
    return normalized


def parse_mysql_connection_url(value: str) -> str:
    connection_url = value.strip()
    parsed = urlparse(connection_url)
    if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname:
        raise ValueError("MySQL secret values must use a mysql:// or mysql+pymysql:// connection URL.")
    if not parsed.username:
        raise ValueError("MySQL connection URLs must include a username.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("MySQL connection URLs must use a valid port.") from exc
    return connection_url


def quote_mysql_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def mysql_connection_options(
    connection_url: str,
    database_name: str,
    connect_timeout: int = 5,
    read_timeout: int = 5,
    write_timeout: int = 5,
    cursorclass: Any = None,
) -> dict[str, Any]:
    parsed_url = parse_mysql_connection_url(connection_url)
    parsed = urlparse(parsed_url)
    database = parse_mysql_identifier(database_name, "database")
    options: dict[str, Any] = {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "charset": "utf8mb4",
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
        "write_timeout": write_timeout,
        "autocommit": False,
    }
    if cursorclass is not None:
        options["cursorclass"] = cursorclass
    return options


def json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    return str(value)


def contains_mysql_executable_comment(sql: str) -> bool:
    index = 0
    while index < len(sql):
        character = sql[index]
        if character in {"'", '"'}:
            quote = character
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    index += 1
                    if index < len(sql) and sql[index] == quote:
                        index += 1
                        continue
                    break
                index += 1
            continue
        if sql.startswith("--", index) or character == "#":
            newline = sql.find("\n", index + 1)
            index = len(sql) if newline < 0 else newline
            continue
        if sql.startswith("/*!", index) or sql.startswith("/*M!", index):
            return True
        if sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = len(sql) if close < 0 else close + 2
            continue
        index += 1
    return False


def validate_mysql_read_only_sql(sql: str) -> None:
    if contains_mysql_executable_comment(sql):
        raise ValueError("MySQL projects cannot contain executable comments.")
    masked_sql = mask_sql_literals_and_comments(sql, hash_line_comments=True)
    statements = [statement for statement in masked_sql.split(";") if statement.strip()]
    if len(statements) != 1:
        raise ValueError("MySQL projects must contain exactly one read-only query.")
    statement = statements[0]
    if not MYSQL_READ_ONLY_START_PATTERN.match(statement):
        raise ValueError("MySQL projects must start with SELECT, WITH, EXPLAIN, or VALUES.")
    if MYSQL_WRITE_KEYWORD_PATTERN.search(statement) or MYSQL_LOCKING_QUERY_PATTERN.search(statement):
        raise ValueError("MySQL projects cannot contain write, DDL, session-control, or locking statements.")


def inspect_mysql_table(
    connection_url: str,
    database_name: str,
    table_name: str,
    preview_limit: int = 50,
) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any]]:
    database = parse_mysql_identifier(database_name, "database")
    table = parse_mysql_identifier(table_name, "table")
    table_sql = f"{quote_mysql_identifier(database)}.{quote_mysql_identifier(table)}"
    connection_options = mysql_connection_options(
        connection_url,
        database,
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    with pymysql.connect(**connection_options) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (MYSQL_INSPECTION_TIMEOUT_MS,))
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(f"SELECT * FROM {table_sql} LIMIT %s", (preview_limit,))
            columns = [field[0] for field in (cursor.description or [])]
            preview = [
                {str(column): json_safe_value(value) for column, value in row.items()}
                for row in cursor.fetchall()
            ]
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM {table_sql}")
            count_row = cursor.fetchone() or {}
            row_count = int(count_row.get("row_count", 0))
    quality = build_quality_summary(columns, preview)
    quality["sampled_rows"] = quality["row_count"]
    quality["row_count"] = row_count
    return columns, preview, row_count, quality
