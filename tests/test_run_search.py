import pytest

from app.run_search import RunSearchFilters, log_excerpt


def test_run_search_filters_normalize_values_and_dates():
    filters = RunSearchFilters.parse(
        query="  timeout  ",
        status="FAILED",
        trigger_type="schedule_retry",
        project_id=" project-1 ",
        started_from="2026-07-01",
        started_to="2026-07-02",
    )

    assert filters.query == "timeout"
    assert filters.status == "failed"
    assert filters.project_id == "project-1"
    assert filters.started_from == "2026-07-01T00:00:00+00:00"
    assert filters.started_to == "2026-07-02T23:59:59+00:00"


def test_run_search_rejects_unknown_status_and_selects_matching_excerpt():
    with pytest.raises(ValueError, match="Unknown run status"):
        RunSearchFilters.parse(status="lost")

    assert log_excerpt("loaded rows\nrequest timeout for warehouse\ncleanup", "", "TIMEOUT") == (
        "request timeout for warehouse"
    )
