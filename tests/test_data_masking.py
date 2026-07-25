from __future__ import annotations

from app.data_masking import REDACTED_FIELD_VALUE, apply_export_masking, mask_value


def test_export_masking_supports_redact_partial_and_hash():
    result = {
        "columns": ["email", "account", "amount"],
        "rows": [["ada@example.com", "ABCDEFGH", 120]],
        "summary": {"rows": 1, "columns": 3},
    }
    metadata = {
        "email": {"masking": "hash"},
        "account": {"masking": "partial"},
        "amount": {"masking": "redact"},
    }

    masked, columns = apply_export_masking(result, metadata, allow_raw=False)

    assert columns == ["email", "account", "amount"]
    assert masked["rows"] == [[mask_value("ada@example.com", "hash"), "AB****GH", REDACTED_FIELD_VALUE]]
    assert masked["summary"] == result["summary"]
    assert apply_export_masking(result, metadata, allow_raw=True) == (result, [])
