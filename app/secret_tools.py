from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable


SECRET_REFERENCE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SECRET_SOURCE_ENVIRONMENT_PATTERN = re.compile(r"^ANYDATAS_SECRET_[A-Z][A-Z0-9_]*$")
SECRET_TARGET_ENVIRONMENT_PATTERN = re.compile(r"^ANYDATAS_USER_SECRET_[A-Z][A-Z0-9_]*$")
SECRET_SOURCE_ENVIRONMENT_PREFIX = "ANYDATAS_SECRET_"
SECRET_TARGET_ENVIRONMENT_PREFIX = "ANYDATAS_USER_SECRET_"
CONTROL_PLANE_ENVIRONMENT_PREFIXES = ("ANYDATAS_SMTP_",)
CONTROL_PLANE_SECRET_ENVIRONMENT_NAMES = {"ANYDATAS_METRICS_TOKEN", "ANYDATAS_METRICS_TOKEN_FILE"}
REDACTED_VALUE = "[REDACTED]"


def parse_secret_reference(name: str, environment_variable: str, description: str) -> tuple[str, str, str]:
    normalized_name = name.strip().lower()
    normalized_environment_variable = environment_variable.strip().upper()
    normalized_description = description.strip()
    if not SECRET_REFERENCE_NAME_PATTERN.fullmatch(normalized_name):
        raise ValueError("Secret reference names must use lowercase letters, numbers, hyphens, or underscores.")
    if not SECRET_SOURCE_ENVIRONMENT_PATTERN.fullmatch(normalized_environment_variable):
        raise ValueError("Secret source variables must start with ANYDATAS_SECRET_ and use uppercase letters, numbers, or underscores.")
    if len(normalized_description) > 500:
        raise ValueError("Secret descriptions must be 500 characters or fewer.")
    return normalized_name, normalized_environment_variable, normalized_description


def parse_secret_target_environment_name(environment_name: str) -> str:
    normalized_name = environment_name.strip().upper()
    if not SECRET_TARGET_ENVIRONMENT_PATTERN.fullmatch(normalized_name):
        raise ValueError("Secret environment names must start with ANYDATAS_USER_SECRET_ and use uppercase letters, numbers, or underscores.")
    if normalized_name.startswith("ANYDATAS_USER_SECRET_SOURCE_"):
        raise ValueError("Secret environment names beginning with ANYDATAS_USER_SECRET_SOURCE_ are reserved for data source connections.")
    return normalized_name


def parse_secret_bindings(raw_bindings: str | None) -> list[dict[str, str]]:
    try:
        bindings = json.loads(raw_bindings or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Secret binding snapshot is invalid.") from exc
    if not isinstance(bindings, list):
        raise ValueError("Secret binding snapshot is invalid.")
    normalized_bindings = []
    seen_secret_ids: set[str] = set()
    seen_environment_names: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError("Secret binding snapshot is invalid.")
        secret_id = binding.get("secret_id")
        environment_name = binding.get("environment_name")
        if not isinstance(secret_id, str) or not secret_id:
            raise ValueError("Secret binding snapshot is invalid.")
        if not isinstance(environment_name, str):
            raise ValueError("Secret binding snapshot is invalid.")
        normalized_environment_name = parse_secret_target_environment_name(environment_name)
        if secret_id in seen_secret_ids or normalized_environment_name in seen_environment_names:
            raise ValueError("Secret binding snapshot contains duplicate bindings.")
        seen_secret_ids.add(secret_id)
        seen_environment_names.add(normalized_environment_name)
        normalized_bindings.append({"secret_id": secret_id, "environment_name": normalized_environment_name})
    return normalized_bindings


def data_source_secret_environment_name(source_id: str) -> str:
    return f"ANYDATAS_USER_SECRET_SOURCE_{source_id.upper()}"


def resolve_secret_reference_value(conn, workspace_id: str, secret_id: str) -> tuple[str, dict[str, str]]:
    reference = conn.execute(
        """
        SELECT id, name, environment_variable
        FROM secret_references
        WHERE workspace_id = ? AND id = ?
        """,
        (workspace_id, secret_id),
    ).fetchone()
    if reference is None:
        raise RuntimeError("A secret reference is no longer available.")
    value = os.getenv(reference["environment_variable"])
    if value is None:
        raise RuntimeError(f"Secret reference '{reference['name']}' is not configured in the runtime environment.")
    return value, {
        "secret_id": reference["id"],
        "secret_name": reference["name"],
        "source_environment_variable": reference["environment_variable"],
    }


def resolve_secret_values(conn, workspace_id: str, bindings: list[dict[str, str]]) -> tuple[dict[str, str], list[dict[str, str]]]:
    if not bindings:
        return {}, []
    secret_values: dict[str, str] = {}
    resolved_references = []
    for binding in bindings:
        value, reference = resolve_secret_reference_value(conn, workspace_id, binding["secret_id"])
        secret_values[binding["environment_name"]] = value
        resolved_references.append(
            {
                "secret_id": reference["secret_id"],
                "secret_name": reference["secret_name"],
                "environment_name": binding["environment_name"],
            }
        )
    return secret_values, resolved_references


def redact_text(value: str | None, secret_values: Iterable[str]) -> str:
    text = value or ""
    for secret_value in sorted({item for item in secret_values if item}, key=len, reverse=True):
        text = text.replace(secret_value, REDACTED_VALUE)
    return text


def redact_result(value: Any, secret_values: Iterable[str]) -> Any:
    values = {item for item in secret_values if item}
    if isinstance(value, str):
        return redact_text(value, values)
    if isinstance(value, list):
        return [redact_result(item, values) for item in value]
    if isinstance(value, dict):
        return {key: redact_result(item, values) for key, item in value.items()}
    if values and str(value) in values:
        return REDACTED_VALUE
    return value


def remove_unbound_secret_sources(environment: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environment.items()
        if not key.startswith((SECRET_SOURCE_ENVIRONMENT_PREFIX, SECRET_TARGET_ENVIRONMENT_PREFIX, *CONTROL_PLANE_ENVIRONMENT_PREFIXES))
        and key not in CONTROL_PLANE_SECRET_ENVIRONMENT_NAMES
    }
