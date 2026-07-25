from __future__ import annotations

import json

from app.auth import SESSION_COOKIE_NAME, create_session
from app.db import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID
from app.db import connect, record_notification
from app.runner import create_run, now_iso


def upload_source(client, name: str, file_bytes: bytes, filename: str = "sales.csv", content_type: str = "text/csv"):
    response = client.post(
        "/data-sources",
        data={"name": name},
        files={"file": (filename, file_bytes, content_type)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM data_sources WHERE name = ?", (name,)).fetchone()


def create_project(client, name: str, source_id: str, script: str, language: str = "sql", parameters_json: str = "{}"):
    response = client.post(
        "/projects",
        data={"name": name, "language": language, "data_source_id": source_id, "script": script, "parameters_json": parameters_json},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def test_workspace_frontend_renders_core_sections(client):
    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "<h1>AnyDatas</h1>" in html
    assert '<body class="workspace-page">' in html
    assert 'class="app-sidebar"' in html
    assert 'id="workspace-nav"' in html
    assert 'id="overview"' in html
    assert "Workspace overview" in html
    assert '<html lang="en">' in html
    assert '<script src="/static/i18n.js" defer></script>' in html
    assert '<script src="/static/workspace.js" defer></script>' in html
    assert "Demo Workspace" in html
    assert "Workspace Members" in html
    assert 'action="/workspace/members"' in html
    assert 'id="notifications"' in html
    assert "Notifications" in html
    assert 'id="delivery-channels"' in html
    assert "Delivery Channels" in html
    assert 'action="/notification-channels"' in html
    assert 'name="channel_type"' in html
    assert '<option value="slack">Slack</option>' in html
    assert '<option value="teams">Microsoft Teams</option>' in html
    assert 'name="event_types"' in html
    assert 'name="max_retries"' in html
    assert 'id="data"' in html
    assert 'id="projects"' in html
    assert '<script src="/static/code-editor.js" defer></script>' in html
    assert 'name="script" spellcheck="false" data-code-editor data-language="sql"' in html
    assert 'id="runs"' in html
    assert 'id="reports"' in html
    assert 'id="audit"' in html
    assert "Upload File" in html
    assert 'name="classification"' in html
    assert "Restricted" in html
    assert "Max 500 MB" in html
    assert ".parquet" in html
    assert ".xlsx" in html
    assert "Connect SQLite" in html
    assert 'action="/data-sources/sqlite"' in html
    assert "Database path" in html
    assert "Table or view" in html
    assert 'name="schedule_type"' in html
    assert "Cron" in html
    assert 'name="cron_expression"' in html
    assert 'name="timezone_name"' in html
    assert 'name="retry_delay_minutes"' in html
    assert 'name="concurrency_policy"' in html
    assert "Queue one" in html
    assert 'value="queue_all">Queue all' in html
    assert 'value="cancel_previous">Cancel previous' in html
    assert "Analysis Projects" in html
    assert "Audit" in html
    assert 'id="secrets"' in html
    assert 'action="/secrets"' in html
    assert 'name="environment_variable"' in html

    editor_asset = client.get("/static/code-editor.js")
    assert editor_asset.status_code == 200
    assert 'textarea[data-code-editor]' in editor_asset.text
    assert "new IntersectionObserver" in editor_asset.text
    assert 'form.requestSubmit()' in editor_asset.text
    assert 'formatButton.textContent = "Format"' in editor_asset.text

    workspace_asset = client.get("/static/workspace.js")
    assert workspace_asset.status_code == 200
    assert 'body.classList.contains("workspace-page")' in workspace_asset.text
    assert 'candidate.hidden = candidate !== section' in workspace_asset.text

    i18n_asset = client.get("/static/i18n.js")
    assert i18n_asset.status_code == 200
    assert 'const STORAGE_KEY = "anydatas.locale"' in i18n_asset.text
    assert '"Workspace overview": "工作区概览"' in i18n_asset.text
    assert 'localStorage.setItem(STORAGE_KEY, normalized)' in i18n_asset.text
    assert 'className = "language-switcher"' in i18n_asset.text


def test_auth_frontend_uses_dedicated_shell(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert '<body class="auth-body">' in response.text
    assert 'class="workspace auth-page"' in response.text
    assert '<script src="/static/i18n.js" defer></script>' in response.text


def test_data_source_schema_renders_field_governance_controls(client, sample_csv_bytes):
    source = upload_source(client, "governance UI sales", sample_csv_bytes)

    response = client.get(f"/data-sources/{source['id']}")

    assert response.status_code == 200
    assert '<body class="detail-page">' in response.text
    assert 'name="field_classifications"' in response.text
    assert 'name="masking_policies"' in response.text
    assert "Export Masking" in response.text
    assert '<option value="pii"' in response.text
    assert '<option value="redact"' in response.text


def test_data_source_frontend_renders_impact_analysis(client, sample_csv_bytes):
    source = upload_source(client, "impact UI sales", sample_csv_bytes)
    project = create_project(client, "impact UI project", source["id"], "SELECT * FROM data LIMIT 1;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Impact UI report", "description": ""},
        follow_redirects=False,
    )
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]

    response = client.get(f"/data-sources/{source['id']}")

    assert response.status_code == 200
    assert 'id="source-impact"' in response.text
    assert "Impact Analysis" in response.text
    assert "Historical Runs" in response.text
    assert "impact UI project" in response.text
    assert f'href="/reports/{report_id}"' in response.text


def test_run_search_frontend_renders_filters_and_log_excerpt(client, sample_csv_bytes):
    source = upload_source(client, "run search UI sales", sample_csv_bytes)
    project = create_project(client, "run search UI project", source["id"], "SELECT 1 AS value;")
    run_id = create_run(project["id"], "manual")
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'failed', logs = 'worker started\nvisible search marker' WHERE id = ?",
            (run_id,),
        )

    response = client.get("/runs", params={"q": "search marker", "status": "failed"})

    assert response.status_code == 200
    assert "Run Search" in response.text
    assert 'name="q"' in response.text
    assert 'name="status"' in response.text
    assert 'name="trigger_type"' in response.text
    assert 'name="project_id"' in response.text
    assert "visible search marker" in response.text
    assert f'href="/runs/{run_id}"' in response.text


def test_password_account_frontend_defaults_api_tokens_to_read_scope(client, monkeypatch):
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        session_token = create_session(conn, DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID)
    client.cookies.set(SESSION_COOKIE_NAME, session_token)

    response = client.get("/")

    assert response.status_code == 200
    assert 'name="username" type="email" value="demo@anydatas.local" autocomplete="username" hidden' in response.text
    assert 'name="scope"' in response.text
    assert '<option value="read" selected>Read only</option>' in response.text
    assert '<option value="full">Full access</option>' in response.text
    assert 'id="service-accounts"' in response.text
    assert '<a href="#service-accounts">Automation</a>' in response.text
    assert 'action="/service-accounts"' in response.text
    assert "Create Service Account" in response.text
    assert 'name="role"' in response.text
    assert "No service accounts." in response.text
    assert f'action="/workspace/members/{DEFAULT_USER_ID}/password-reset"' in response.text
    assert "Reset Password" in response.text


def test_registration_frontend_renders_only_when_signup_is_enabled(client, monkeypatch):
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_ALLOW_SIGNUP", "1")
    client.cookies.clear()

    login_response = client.get("/login")
    registration_response = client.get("/register")

    assert login_response.status_code == 200
    assert '<a class="button-link secondary" href="/register">Create Account</a>' in login_response.text
    assert registration_response.status_code == 200
    assert "Create Account" in registration_response.text
    assert 'name="name" autocomplete="name"' in registration_response.text
    assert 'name="email" type="email" autocomplete="username"' in registration_response.text
    assert registration_response.text.count('autocomplete="new-password"') == 2


def test_workspace_frontend_renders_postgres_connection_form_when_a_secret_reference_exists(client):
    assert client.post(
        "/secrets",
        data={
            "name": "warehouse-url",
            "environment_variable": "ANYDATAS_SECRET_WAREHOUSE_URL",
            "description": "PostgreSQL URL",
        },
        follow_redirects=False,
    ).status_code == 303

    response = client.get("/")

    assert response.status_code == 200
    assert "Connect PostgreSQL" in response.text
    assert 'action="/data-sources/postgres"' in response.text
    assert 'name="secret_id"' in response.text
    assert 'name="schema_name"' in response.text
    assert "Connect MySQL" in response.text
    assert 'action="/data-sources/mysql"' in response.text
    assert 'name="database_name"' in response.text
    assert "Connect ClickHouse" in response.text
    assert 'action="/data-sources/clickhouse"' in response.text
    assert "Import S3 / MinIO" in response.text
    assert 'action="/data-sources/s3"' in response.text
    assert 'name="object_key"' in response.text


def test_workspace_frontend_renders_quota_usage_and_controls(client):
    response = client.post(
        "/workspace/quotas",
        data={
            "max_data_sources": 3,
            "max_projects": 4,
            "max_schedules": 5,
            "max_reports": 6,
            "max_concurrent_runs": 7,
            "max_storage_mb": 512,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="quotas"' in html
    assert "Workspace Limits" in html
    assert "Resource Usage" in html
    assert 'action="/workspace/quotas"' in html
    assert 'name="max_data_sources"' in html
    assert 'name="max_concurrent_runs"' in html
    assert 'name="max_storage_mb"' in html
    assert "Data Source Storage" in html
    assert 'value="3"' in html
    assert 'value="4"' in html
    assert 'value="5"' in html
    assert 'value="6"' in html
    assert 'value="7"' in html
    assert 'value="512"' in html
    assert "Save Limits" in html


def test_workspace_frontend_renders_admin_runtime_usage(client, monkeypatch):
    monkeypatch.setenv("ANYDATAS_RUNNER_COST_PER_HOUR_CNY", "2.5")

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="run-usage"' in html
    assert "Runtime Usage" in html
    assert "Last 24 Hours" in html
    assert "Retained History" in html
    assert "Compute Hours" in html
    assert "¥2.50 per compute hour" in html

    monkeypatch.setenv("ANYDATAS_RUNNER_COST_PER_HOUR_CNY", "not-a-number")
    assert client.get("/").status_code == 200


def test_workspace_frontend_renders_project_actions(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "regional revenue", source["id"], "SELECT * FROM data LIMIT 1;")

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "regional revenue" in html
    assert f'action="/projects/{project["id"]}/run"' in html
    assert f'action="/projects/{project["id"]}/publish"' in html
    assert "Latest v1" in html
    assert "Published v1" in html
    assert "Save Version" in html
    assert "Publish" in html
    assert 'action="/reports"' in html
    assert "Run" in html
    assert "Report" in html


def test_workspace_frontend_renders_operator_runtime_profiles(client, monkeypatch, sample_csv_bytes):
    monkeypatch.setenv(
        "ANYDATAS_RUNTIME_PROFILES_JSON",
        json.dumps(
            {
                "science": {
                    "label": "Data Science",
                    "image": "registry.example.com/anydatas/science:2026-07",
                }
            }
        ),
    )
    source = upload_source(client, "runtime UI sales", sample_csv_bytes)
    project = create_project(client, "runtime UI project", source["id"], "result = []", language="python")
    response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "runtime UI project",
            "language": "python",
            "runtime_profile": "science",
            "data_source_id": source["id"],
            "script": "result = []",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    html = client.get("/").text

    assert 'name="runtime_profile"' in html
    assert "Data Science" in html
    assert 'value="science" selected' in html
    assert "science runtime" in html


def test_project_and_report_frontends_render_authorized_lineage(client, sample_csv_bytes):
    source = upload_source(client, "lineage sales", sample_csv_bytes)
    project = create_project(client, "lineage project", source["id"], "SELECT * FROM data LIMIT 1;")
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Lineage report", "description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_id = response.headers["location"].rsplit("/", 1)[-1]

    workspace_response = client.get("/")
    report_response = client.get(f"/reports/{report_id}")

    assert workspace_response.status_code == 200
    assert f'id="project-{project["id"]}"' in workspace_response.text
    assert "Linked Reports" in workspace_response.text
    assert f'href="/reports/{report_id}"' in workspace_response.text
    assert "Lineage report" in workspace_response.text
    assert report_response.status_code == 200
    assert 'id="report-lineage"' in report_response.text
    assert f'href="/#project-{project["id"]}"' in report_response.text
    assert "Data Source" in report_response.text
    assert "lineage sales" in report_response.text
    assert "Snapshot Run" in report_response.text


def test_data_source_frontend_links_to_schema_editor(client, sample_csv_bytes):
    source = upload_source(client, "schema link sales", sample_csv_bytes)

    workspace_response = client.get("/")
    detail_response = client.get(f"/data-sources/{source['id']}")

    assert f'href="/data-sources/{source["id"]}"' in workspace_response.text
    assert detail_response.status_code == 200
    assert "Schema" in detail_response.text
    assert f'action="/data-sources/{source["id"]}/schema"' in detail_response.text
    assert f'action="/data-sources/{source["id"]}/classification"' in detail_response.text
    assert "Classification" in detail_response.text
    assert "Save Schema" in detail_response.text
    assert "Preview" in detail_response.text


def test_data_source_frontend_renders_private_access_controls_for_a_manager(client, sample_csv_bytes):
    source = upload_source(client, "private source controls", sample_csv_bytes)
    assert client.post(
        "/workspace/members",
        data={"email": "source-member@example.com", "name": "Source Member", "role": "analyst"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/data-sources/{source['id']}/visibility",
        data={"visibility": "private"},
        follow_redirects=False,
    ).status_code == 303

    detail_response = client.get(f"/data-sources/{source['id']}")
    workspace_response = client.get("/")

    assert detail_response.status_code == 200
    assert "Access" in detail_response.text
    assert f'action="/data-sources/{source["id"]}/visibility"' in detail_response.text
    assert f'action="/data-sources/{source["id"]}/grants"' in detail_response.text
    assert 'name="permission"' in detail_response.text
    assert "<th>Access</th>" in workspace_response.text


def test_workspace_and_run_detail_render_project_parameters(client, sample_csv_bytes):
    source = upload_source(client, "parameter sales", sample_csv_bytes)
    project = create_project(
        client,
        "parameter project",
        source["id"],
        "SELECT $region AS region;",
        parameters_json='{"region": "East", "minimum_revenue": 100}',
    )

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'name="parameters_json"' in html
    assert "Parameters (JSON; SQL uses $name)" in html
    assert "2 params" in html
    assert "minimum_revenue" in html

    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert response.status_code == 303
    detail_response = client.get(response.headers["location"])
    assert detail_response.status_code == 200
    assert "Parameters" in detail_response.text
    assert "2 values" in detail_response.text
    assert "minimum_revenue" in detail_response.text
    assert "Attempt" in detail_response.text


def test_run_frontend_renders_independent_result_and_log_pagination(client, sample_csv_bytes):
    source = upload_source(client, "pagination sales", sample_csv_bytes)
    project = create_project(client, "pagination project", source["id"], "SELECT * FROM data LIMIT 1;")
    run_id = create_run(project["id"], "manual")
    result = {"columns": ["value"], "rows": [[f"frontend-row-{index:03d}"] for index in range(101)]}
    logs = "\n".join(f"frontend-log-{index:03d}" for index in range(201))
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'succeeded', result_json = ?, logs = ?, finished_at = ? WHERE id = ?",
            (json.dumps(result), logs, now_iso(), run_id),
        )

    first_page = client.get(f"/runs/{run_id}")
    assert first_page.status_code == 200
    assert "1-100 of 101 rows" in first_page.text
    assert "1-200 of 201 log lines" in first_page.text
    assert f'href="/runs/{run_id}?result_page=2&amp;log_page=1#result-artifacts"' in first_page.text
    assert f'href="/runs/{run_id}?result_page=1&amp;log_page=2#execution-logs"' in first_page.text

    second_page = client.get(f"/runs/{run_id}?result_page=2&log_page=2")
    assert "101-101 of 101 rows" in second_page.text
    assert "201-201 of 201 log lines" in second_page.text
    assert "frontend-row-100" in second_page.text
    assert "frontend-log-200" in second_page.text


def test_run_frontend_renders_cancel_control_for_queued_run(client, sample_csv_bytes):
    source = upload_source(client, "cancel control sales", sample_csv_bytes)
    project = create_project(client, "cancel control project", source["id"], "SELECT * FROM data LIMIT 1;")
    run_id = create_run(project["id"], "manual")

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert f'action="/runs/{run_id}/cancel"' in response.text
    assert "Cancel Run" in response.text

    cancel_response = client.post(f"/runs/{run_id}/cancel", follow_redirects=False)
    assert cancel_response.status_code == 303
    canceled_response = client.get(cancel_response.headers["location"])
    assert "Cancellation" in canceled_response.text
    assert "canceled" in canceled_response.text


def test_workspace_frontend_renders_schedule_rules_and_actions(client, sample_csv_bytes):
    source = upload_source(client, "scheduled sales", sample_csv_bytes)
    project = create_project(client, "scheduled project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/schedules",
        data={
            "project_id": project["id"],
            "name": "hourly refresh",
            "interval_minutes": 60,
            "max_retries": 2,
            "retry_delay_minutes": 7,
            "concurrency_policy": "queue_one",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "hourly refresh" in html
    assert "Every 60 min" in html
    assert "2 x / 7 min" in html
    assert "queue_one" in html
    assert f'action="/schedules/{schedule["id"]}/run"' in html
    assert f'href="/schedules/{schedule["id"]}/backfill"' in html
    assert "Run Now" in html
    assert "Pause" in html


def test_schedule_backfill_page_renders_range_controls(client, sample_csv_bytes):
    source = upload_source(client, "backfill form sales", sample_csv_bytes)
    project = create_project(client, "backfill form project", source["id"], "SELECT * FROM data LIMIT 1;")
    assert client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "backfill form", "interval_minutes": 60},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()

    response = client.get(f"/schedules/{schedule['id']}/backfill")

    assert response.status_code == 200
    assert f'action="/schedules/{schedule["id"]}/backfill"' in response.text
    assert 'name="start_at" type="datetime-local"' in response.text
    assert 'name="end_at" type="datetime-local"' in response.text
    assert 'name="max_runs" type="number"' in response.text
    assert "Queue Backfill" in response.text


def test_workspace_frontend_renders_failure_notifications(client, sample_csv_bytes):
    source = upload_source(client, "notified sales", sample_csv_bytes)
    project = create_project(client, "notified failure", source["id"], "SELECT missing_column FROM data;")
    response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "1 unread" in html
    assert "Run failed: notified failure" in html
    assert f'href="/runs/{run["id"]}"' in html
    assert "Mark Read" in html


def test_workspace_frontend_renders_failed_delivery_retry_action(client):
    assert client.post(
        "/notification-channels",
        data={
            "name": "frontend retry email",
            "channel_type": "email",
            "destination": "ops@example.com",
            "secret_id": "",
            "event_types": "run.failed",
            "max_retries": 0,
        },
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        record_notification(
            conn,
            "demo-workspace",
            "run.failed",
            "Run failed: frontend retry",
            "The source query failed.",
            "error",
            "run",
            "frontend-run",
        )
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        conn.execute("UPDATE notification_deliveries SET status = 'failed', attempt = 1 WHERE id = ?", (delivery["id"],))

    response = client.get("/")

    assert response.status_code == 200
    assert "frontend retry email" in response.text
    assert "failed" in response.text
    assert "Retry" in response.text
    assert f'action="/notification-deliveries/{delivery["id"]}/requeue"' in response.text


def test_workspace_frontend_marks_unpublished_latest_version(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "version marker", source["id"], "SELECT 'published' AS marker;")
    response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "version marker",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'draft' AS marker;",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "Latest v2" in html
    assert "Published v1" in html


def test_workspace_frontend_renders_data_quality_summary(client):
    upload_source(
        client,
        "messy quality sales",
        b"region,revenue\nEast,120\nWest,\nEast,120\n",
    )

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "messy quality sales" in html
    assert "Quality" in html
    assert "83.33%" in html
    assert "1 empty / 1 duplicate" in html


def test_workspace_frontend_renders_parquet_data_source(client, sample_parquet_bytes):
    upload_source(
        client,
        "parquet sales",
        sample_parquet_bytes,
        "sales.parquet",
        "application/octet-stream",
    )

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "parquet sales" in html
    assert "parquet" in html
    assert "100.0%" in html


def test_workspace_frontend_renders_xlsx_data_source(client, sample_xlsx_bytes):
    upload_source(
        client,
        "xlsx sales",
        sample_xlsx_bytes,
        "sales.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "xlsx sales" in html
    assert "xlsx" in html
    assert "100.0%" in html


def test_run_detail_frontend_renders_artifacts_and_logs(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(
        client,
        "regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )
    run_response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert run_response.status_code == 303

    workspace_response = client.get("/")
    assert workspace_response.status_code == 200
    assert f'href="{run_response.headers["location"]}"' in workspace_response.text
    assert "Details" in workspace_response.text

    detail_response = client.get(run_response.headers["location"])
    assert detail_response.status_code == 200
    html = detail_response.text
    assert "Run Details" in html
    assert "Result Artifacts" in html
    assert "Execution Logs" in html
    assert "Download CSV" in html
    assert "Download JSON" in html
    assert "regional revenue" in html
    assert "East" in html
    assert "West" in html


def test_report_frontend_renders_snapshot_table_and_chart(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(
        client,
        "regional revenue",
        source["id"],
        "SELECT region, SUM(revenue) AS revenue FROM data GROUP BY region ORDER BY revenue DESC;",
    )
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Revenue report", "description": "Latest"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.post(
        "/notification-channels",
        data={
            "name": "report delivery email",
            "channel_type": "email",
            "destination": "reports@example.com",
            "event_types": ["report.refresh_succeeded"],
            "max_retries": 1,
        },
        follow_redirects=False,
    ).status_code == 303

    report_response = client.get(response.headers["location"])

    assert report_response.status_code == 200
    html = report_response.text
    assert "Revenue report" in html
    assert '<body class="detail-page report-page">' in html
    assert 'class="report-toolbar"' in html
    assert 'class="toolbar-menu export-menu"' in html
    assert "Latest Snapshot" in html
    assert "Refresh Snapshot" in html
    assert "Unsubscribe" in html
    assert "Save Delivery" in html
    assert 'name="external_channel_ids"' in html
    assert "report delivery email (email)" in html
    assert f'action="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/subscriptions/delete"' in html
    assert "Refresh Status" in html
    assert f'href="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/snapshot.csv"' in html
    assert f'href="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/snapshot.json"' in html
    assert f'href="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/snapshot.xlsx"' in html
    assert f'href="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/snapshot.png"' in html
    assert f'href="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/snapshot.pdf"' in html
    assert "Download CSV" in html
    assert "Download JSON" in html
    assert 'name="widget_width"' in html
    assert '/layout" method="post"' in html
    assert 'id="report-widget-list"' in html
    assert f'data-reorder-url="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/widgets/reorder"' in html
    assert 'draggable="true" data-widget-id=' in html
    assert 'id="widget-order-status" role="status" aria-live="polite"' in html
    assert 'list.addEventListener("dragstart"' in html
    assert 'method: "POST"' in html
    assert 'class="report-layout-grid"' in html
    assert 'width-quarter' in html
    assert 'width-half' in html
    assert 'width-full' in html
    assert "Result Table" in html
    assert "bar-chart" in html
    assert "Components" in html
    assert "Pie Chart" in html
    assert f'action="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/widgets"' in html
    assert 'name="kind"' in html
    assert 'name="markdown_text"' in html
    assert "Filters" in html
    assert f'action="/reports/{response.headers["location"].rsplit("/", 1)[-1]}/filters"' in html
    assert 'name="column_name"' in html
    assert 'name="filter_type"' in html
    assert "Run Details" in html
    assert "East" in html
    assert "West" in html


def test_report_frontend_renders_scatter_and_table_highlight_controls(client):
    source = upload_source(client, "scatter frontend sales", b"day,revenue\n1,120\n2,80\n3,180\n")
    project = create_project(client, "scatter frontend project", source["id"], "SELECT day, revenue FROM data ORDER BY day;")
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Scatter frontend report", "description": ""},
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    assert client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "scatter", "title": "Revenue distribution", "x_column": "day", "value_column": "revenue"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post(
        f"/reports/{report_id}/widgets",
        data={
            "kind": "table",
            "title": "Revenue threshold",
            "table_highlight_column": "revenue",
            "table_highlight_rule": "below",
            "table_highlight_threshold": 100,
        },
        follow_redirects=False,
    ).status_code == 303

    response = client.get(f"/reports/{report_id}")

    assert response.status_code == 200
    html = response.text
    assert "Scatter Chart" in html
    assert 'name="x_column"' in html
    assert 'name="table_highlight_column"' in html
    assert 'name="table_highlight_rule"' in html
    assert 'name="table_highlight_threshold"' in html
    assert "Revenue distribution" in html
    assert 'class="scatter-chart"' in html
    assert "Revenue threshold" in html
    assert 'class="table-cell-bad"' in html


def test_report_frontend_renders_the_latest_scheduled_snapshot(client, sample_csv_bytes):
    source = upload_source(client, "scheduled snapshot sales", sample_csv_bytes)
    project = create_project(client, "scheduled snapshot project", source["id"], "SELECT 'before' AS marker;")
    client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Scheduled snapshot report", "description": "Latest"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_path = response.headers["location"]
    update_response = client.post(
        f"/projects/{project['id']}",
        data={
            "name": "scheduled snapshot project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT 'scheduled' AS marker;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert update_response.status_code == 303
    assert client.post(f"/projects/{project['id']}/publish", follow_redirects=False).status_code == 303
    schedule_response = client.post(
        "/schedules",
        data={"project_id": project["id"], "name": "scheduled snapshot", "interval_minutes": 60},
        follow_redirects=False,
    )
    assert schedule_response.status_code == 303
    with connect() as conn:
        schedule = conn.execute("SELECT * FROM schedules WHERE project_id = ?", (project["id"],)).fetchone()

    assert client.post(f"/schedules/{schedule['id']}/run", follow_redirects=False).status_code == 303
    report_response = client.get(report_path)
    assert report_response.status_code == 200
    assert "scheduled" in report_response.text


def test_report_frontend_renders_visibility_controls(client, sample_csv_bytes):
    member_response = client.post(
        "/workspace/members",
        data={"email": "report-reader@example.com", "name": "Report Reader", "role": "viewer"},
        follow_redirects=False,
    )
    assert member_response.status_code == 303
    source = upload_source(client, "private report sales", sample_csv_bytes)
    project = create_project(client, "private report project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/reports",
        data={
            "project_id": project["id"],
            "title": "Private report",
            "description": "Restricted",
            "visibility": "private",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    report_id = response.headers["location"].rsplit("/", 1)[-1]

    workspace_response = client.get("/")
    assert workspace_response.status_code == 200
    workspace_html = workspace_response.text
    assert 'name="visibility"' in workspace_html
    assert "Private report" in workspace_html
    assert "private" in workspace_html
    assert f'action="/reports/{report_id}/visibility"' in workspace_html
    assert 'aria-label="Report visibility"' in workspace_html

    report_response = client.get(response.headers["location"])
    assert report_response.status_code == 200
    report_html = report_response.text
    assert "private" in report_html
    assert "Private Access" in report_html
    assert f'action="/reports/{report_id}/grants"' in report_html
    assert 'name="user_id"' in report_html
    assert "Report Reader" in report_html
    assert "Grant Access" in report_html


def test_report_frontend_without_successful_run_shows_empty_state(client, sample_csv_bytes):
    source = upload_source(client, "sales", sample_csv_bytes)
    project = create_project(client, "empty report project", source["id"], "SELECT * FROM data LIMIT 1;")
    response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Empty report", "description": "No run yet"},
        follow_redirects=False,
    )

    report_response = client.get(response.headers["location"])

    assert report_response.status_code == 200
    assert "No successful snapshot" in report_response.text
    assert "Refresh Snapshot" in report_response.text
    assert "Refresh the report after a successful project run to publish a snapshot" in report_response.text
