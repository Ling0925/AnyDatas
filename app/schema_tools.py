from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

from .data_masking import FIELD_CLASSIFICATIONS, MASKING_POLICIES


LOGICAL_TYPES = ("text", "integer", "number", "boolean", "date", "datetime")
INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")


def infer_value_type(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "empty"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "text"
        return "integer" if value.is_integer() else "number"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, date):
        return "date"

    text = str(value).strip()
    if text.lower() in {"true", "false"}:
        return "boolean"
    if INTEGER_PATTERN.fullmatch(text):
        return "integer"
    try:
        number = float(text)
        if math.isfinite(number):
            return "number"
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
        if "T" in text or " " in text:
            return "datetime"
    except ValueError:
        pass
    try:
        date.fromisoformat(text)
        return "date"
    except ValueError:
        return "text"


def infer_column_type(values: list[Any]) -> str:
    observed = {infer_value_type(value) for value in values}
    observed.discard("empty")
    if not observed:
        return "text"
    if observed <= {"integer"}:
        return "integer"
    if observed <= {"integer", "number"}:
        return "number"
    if observed <= {"date"}:
        return "date"
    if observed <= {"date", "datetime"}:
        return "datetime"
    if observed <= {"boolean"}:
        return "boolean"
    return "text"


def build_column_metadata(
    columns: list[str],
    preview: list[dict[str, Any]],
    existing: dict[str, Any] | None = None,
) -> dict[str, dict[str, str]]:
    existing = existing if isinstance(existing, dict) else {}
    metadata: dict[str, dict[str, str]] = {}
    for column in columns:
        previous = existing.get(column)
        previous = previous if isinstance(previous, dict) else {}
        declared_type = previous.get("type")
        values = [row.get(column) for row in preview if isinstance(row, dict)]
        column_type = declared_type if declared_type in LOGICAL_TYPES else infer_column_type(values)
        description = previous.get("description")
        classification = previous.get("classification", "none")
        masking = previous.get("masking", "none")
        metadata[column] = {
            "type": column_type,
            "description": str(description) if description is not None else "",
            "classification": classification if classification in FIELD_CLASSIFICATIONS else "none",
            "masking": masking if masking in MASKING_POLICIES else "none",
        }
    return metadata
