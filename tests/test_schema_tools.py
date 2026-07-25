from __future__ import annotations

from app.schema_tools import build_column_metadata, infer_column_type


def test_schema_inference_detects_common_logical_types():
    assert infer_column_type(["1", "2", "-3"]) == "integer"
    assert infer_column_type(["1", "2.5", "-3"]) == "number"
    assert infer_column_type(["true", "false"]) == "boolean"
    assert infer_column_type(["2026-07-01", "2026-07-02"]) == "date"
    assert infer_column_type(["2026-07-01T09:00:00", "2026-07-02T10:00:00"]) == "datetime"
    assert infer_column_type(["East", "West"]) == "text"


def test_schema_metadata_preserves_declared_type_and_description():
    metadata = build_column_metadata(
        ["revenue", "region"],
        [{"revenue": "120", "region": "East"}],
        {"revenue": {"type": "number", "description": "Revenue in USD"}},
    )

    assert metadata["revenue"] == {
        "type": "number",
        "description": "Revenue in USD",
        "classification": "none",
        "masking": "none",
    }
    assert metadata["region"] == {
        "type": "text",
        "description": "",
        "classification": "none",
        "masking": "none",
    }


def test_schema_metadata_ignores_malformed_preview_rows():
    metadata = build_column_metadata(
        ["active"],
        [{"active": "true"}, "not-a-row"],  # type: ignore[list-item]
    )

    assert metadata["active"] == {
        "type": "boolean",
        "description": "",
        "classification": "none",
        "masking": "none",
    }
