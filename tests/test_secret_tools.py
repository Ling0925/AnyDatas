from __future__ import annotations

import pytest

from app.secret_tools import (
    REDACTED_VALUE,
    data_source_secret_environment_name,
    parse_secret_bindings,
    parse_secret_reference,
    parse_secret_target_environment_name,
    redact_result,
    redact_text,
    remove_unbound_secret_sources,
)


def test_secret_reference_validation_and_redaction_helpers():
    assert parse_secret_reference(
        "warehouse-password",
        "ANYDATAS_SECRET_WAREHOUSE_PASSWORD",
        "Read-only password",
    ) == (
        "warehouse-password",
        "ANYDATAS_SECRET_WAREHOUSE_PASSWORD",
        "Read-only password",
    )
    assert parse_secret_target_environment_name("anydatas_user_secret_warehouse_password") == "ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD"
    assert redact_text("token=secret-value", ["secret-value"]) == f"token={REDACTED_VALUE}"
    assert redact_result({"rows": [["secret-value"]]}, ["secret-value"]) == {"rows": [[REDACTED_VALUE]]}
    assert remove_unbound_secret_sources(
        {
            "ANYDATAS_SECRET_WAREHOUSE_PASSWORD": "source-value",
            "ANYDATAS_USER_SECRET_WAREHOUSE_PASSWORD": "stale-value",
            "ANYDATAS_SMTP_PASSWORD": "smtp-password",
            "ANYDATAS_METRICS_TOKEN": "metrics-token",
            "ANYDATAS_METRICS_TOKEN_FILE": "/run/secrets/metrics-token",
            "PATH": "/usr/bin",
        }
    ) == {"PATH": "/usr/bin"}


def test_secret_binding_validation_rejects_unsafe_environment_names_and_duplicates():
    with pytest.raises(ValueError, match="ANYDATAS_USER_SECRET_"):
        parse_secret_target_environment_name("PATH")
    with pytest.raises(ValueError, match="reserved"):
        parse_secret_target_environment_name("ANYDATAS_USER_SECRET_SOURCE_DATABASE")
    with pytest.raises(ValueError, match="lowercase"):
        parse_secret_reference("Warehouse Password", "ANYDATAS_SECRET_PASSWORD", "")
    with pytest.raises(ValueError, match="duplicate"):
        parse_secret_bindings(
            '[{"secret_id": "one", "environment_name": "ANYDATAS_USER_SECRET_ONE"}, '
            '{"secret_id": "one", "environment_name": "ANYDATAS_USER_SECRET_TWO"}]'
        )
    assert data_source_secret_environment_name("ab12cd") == "ANYDATAS_USER_SECRET_SOURCE_AB12CD"
