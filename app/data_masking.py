from __future__ import annotations

import hashlib
from typing import Any


FIELD_CLASSIFICATIONS = ("none", "pii", "financial", "customer", "sensitive")
MASKING_POLICIES = ("none", "redact", "partial", "hash")
REDACTED_FIELD_VALUE = "[REDACTED]"


def mask_value(value: Any, policy: str) -> Any:
    if value is None or policy == "none":
        return value
    text = str(value)
    if not text:
        return value
    if policy == "redact":
        return REDACTED_FIELD_VALUE
    if policy == "hash":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"sha256:{digest}"
    if policy == "partial":
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"
    raise ValueError("Unsupported field masking policy.")


def apply_export_masking(
    result: dict[str, Any],
    column_metadata: dict[str, Any],
    allow_raw: bool,
) -> tuple[dict[str, Any], list[str]]:
    if allow_raw:
        return result, []
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if not isinstance(columns, list) or not isinstance(rows, list):
        return result, []
    policies = []
    masked_columns = []
    for column in columns:
        metadata = column_metadata.get(str(column), {}) if isinstance(column_metadata, dict) else {}
        policy = metadata.get("masking", "none") if isinstance(metadata, dict) else "none"
        if policy not in MASKING_POLICIES:
            policy = "none"
        policies.append(policy)
        if policy != "none":
            masked_columns.append(str(column))
    if not masked_columns:
        return result, []
    masked_rows = []
    for row in rows:
        values = list(row) if isinstance(row, (list, tuple)) else [row]
        masked_rows.append(
            [mask_value(value, policies[index]) if index < len(policies) else value for index, value in enumerate(values)]
        )
    return {**result, "rows": masked_rows}, masked_columns
